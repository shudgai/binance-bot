import json
import os
import requests
import time
import logging
import asyncio
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Any, Optional
from dotenv import load_dotenv

load_dotenv()

# --- 配置 ---
AI_API_KEY = os.getenv("OPENAI_API_KEY")
AI_MODEL = "gpt-4o" # 推薦使用 gpt-4o 或 claude-3-5-sonnet
TRADE_HISTORY_FILE = os.path.join(os.path.dirname(__file__), "..", "data", "trade_history.json")
BOT_SYMBOLS_FILE = os.path.join(os.path.dirname(__file__), "..", "data", "bot_symbols.json")

# --- 安全閥門：硬性限制 ---
SAFETY_LIMITS = {
    "leverage": (1, 10),
    "sl_atr_multiplier": (0.5, 5.0),
    "tp_atr_multiplier": (1.0, 10.0),
    "add_entry_pct": (0.05, 0.7),
    "volume_threshold_factor": (0.5, 3.0),
    "min_flip_time": (60, 3600)
}

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("AI_Manager")

class AIManager:
    def __init__(self):
        self.history_path = TRADE_HISTORY_FILE
        self.config_path = BOT_SYMBOLS_FILE

    def _get_recent_memories(self, limit: int = 50) -> List[Dict]:
        """讀取最近的交易經驗並過濾出重點摘要。"""
        if not os.path.exists(self.history_path):
            return []
        try:
            with open(self.history_path, "r", encoding="utf-8") as f:
                history = json.load(f)
                if not isinstance(history, list): return []
                # 取最後 N 筆記錄
                return history[-limit:]
        except Exception as e:
            logger.error(f"讀取歷史紀錄失敗: {e}")
            return []

    def _fetch_ai_diagnosis(self, memories: List[Dict]) -> Optional[Dict]:
        """發送數據給 AI 並獲取診斷結果。"""
        if not memories:
            return None

        # 讀取當前設定檔參數作為對照上下文
        current_configs = {}
        if os.path.exists(self.config_path):
            try:
                with open(self.config_path, "r", encoding="utf-8") as f:
                    config_data = json.load(f)
                    current_configs = config_data.get("profiles", {})
            except Exception as e:
                logger.error(f"AI 診斷讀取當前配置失敗: {e}")

        # 將摘要轉化為 AI 友好的文字描述
        # Prepare request payload
        req_payload = {
            "model": AI_MODEL,
            "messages": [
                {"role": "system", "content": "You are a helpful trading assistant."},
                {"role": "user", "content": prompt}
            ],
            "response_format": {"type": "json_object"}
        }

        # Ensure directory for raw responses exists
        raw_dir = Path(os.path.join(os.path.dirname(__file__), '..', 'data', 'ai_raw_responses')).resolve()
        raw_dir.mkdir(parents=True, exist_ok=True)

        max_attempts = 3
        backoff_base = 1.0
        for attempt in range(1, max_attempts + 1):
            try:
                response = requests.post(
                    "https://api.openai.com/v1/chat/completions",
                    headers={"Authorization": f"Bearer {AI_API_KEY}", "Content-Type": "application/json"},
                    data=json.dumps(req_payload),
                    timeout=30
                )
            except requests.RequestException as e:
                logger.warning(f"AI API 請求失敗 (attempt {attempt}/{max_attempts}): {e}")
                if attempt < max_attempts:
                    time.sleep(backoff_base * (2 ** (attempt - 1)))
                    continue
                logger.error(f"AI API 請求最終失敗: {e}")
                return None

            # Save raw response if non-200 or content issues
            timestamp = datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')
            raw_file = raw_dir / f"response_{timestamp}_attempt{attempt}_status{getattr(response,'status_code', 'na')}.txt"
            try:
                body_text = response.text
            except Exception:
                body_text = '<unreadable body>'

            # If not OK, persist and possibly retry
            if getattr(response, 'status_code', None) != 200:
                raw_file.write_text(f"STATUS: {getattr(response,'status_code', None)}\n\nHEADERS:\n{response.headers}\n\nBODY:\n{body_text}\n\nREQUEST_PAYLOAD:\n{json.dumps(req_payload, ensure_ascii=False, indent=2)}", encoding='utf-8')
                logger.error(f"AI API 非 200 回應 (status={getattr(response,'status_code',None)}), 已保存 raw response: {raw_file}")
                if attempt < max_attempts:
                    time.sleep(backoff_base * (2 ** (attempt - 1)))
                    continue
                return None

            # Try parse JSON
            try:
                res_json = response.json()
            except Exception as e:
                raw_file.write_text(f"JSON_PARSE_ERROR: {e}\n\nBODY:\n{body_text}\n\nREQUEST_PAYLOAD:\n{json.dumps(req_payload, ensure_ascii=False, indent=2)}", encoding='utf-8')
                logger.error(f"解析 AI 回傳 JSON 失敗 (attempt {attempt}): {e}，raw saved: {raw_file}")
                if attempt < max_attempts:
                    time.sleep(backoff_base * (2 ** (attempt - 1)))
                    continue
                return None

            # Validate structure
            choices = res_json.get("choices") if isinstance(res_json, dict) else None
            if not choices or not isinstance(choices, list):
                raw_file.write_text(f"MISSING_CHOICES\n\nRESPONSE:\n{json.dumps(res_json, ensure_ascii=False, indent=2)}\n\nREQUEST_PAYLOAD:\n{json.dumps(req_payload, ensure_ascii=False, indent=2)}", encoding='utf-8')
                logger.error(f"AI API 回傳格式缺少 choices 或格式錯誤 (attempt {attempt}), raw saved: {raw_file}")
                if attempt < max_attempts:
                    time.sleep(backoff_base * (2 ** (attempt - 1)))
                    continue
                return None

            message = choices[0].get("message") if isinstance(choices[0], dict) else None
            if not message:
                raw_file.write_text(f"MISSING_MESSAGE\n\nCHOICES:\n{json.dumps(choices, ensure_ascii=False, indent=2)}\n\nREQUEST_PAYLOAD:\n{json.dumps(req_payload, ensure_ascii=False, indent=2)}", encoding='utf-8')
                logger.error(f"AI API 回傳缺少 message 欄位 (attempt {attempt}), raw saved: {raw_file}")
                if attempt < max_attempts:
                    time.sleep(backoff_base * (2 ** (attempt - 1)))
                    continue
                return None

            content_text = message.get("content")
            if not content_text:
                raw_file.write_text(f"EMPTY_CONTENT\n\nMESSAGE:\n{json.dumps(message, ensure_ascii=False, indent=2)}\n\nREQUEST_PAYLOAD:\n{json.dumps(req_payload, ensure_ascii=False, indent=2)}", encoding='utf-8')
                logger.error(f"AI API 回傳 message.content 為空 (attempt {attempt}), raw saved: {raw_file}")
                if attempt < max_attempts:
                    time.sleep(backoff_base * (2 ** (attempt - 1)))
                    continue
                return None

            try:
                content = json.loads(content_text)
            except Exception as e:
                raw_file.write_text(f"CONTENT_JSON_PARSE_ERROR: {e}\n\nCONTENT_TEXT:\n{content_text[:2000]}\n\nREQUEST_PAYLOAD:\n{json.dumps(req_payload, ensure_ascii=False, indent=2)}", encoding='utf-8')
                logger.error(f"解析 AI 回傳 JSON 失敗 (attempt {attempt}): {e}；原始回傳片段已存: {raw_file}")
                if attempt < max_attempts:
                    time.sleep(backoff_base * (2 ** (attempt - 1)))
                    continue
                return None

            return content.get("diagnoses", [])

    def validate_suggestion(self, symbol: str, suggestion: Dict) -> Optional[Dict]:
        """安全閥門：檢查 AI 給出的建議是否在安全範圍內。"""
        new_params = suggestion.get("suggested_params", {})
        if not new_params:
            return None

        validated_params = {}
        for key, value in new_params.items():
            if key in SAFETY_LIMITS:
                min_val, max_val = SAFETY_LIMITS[key]
                if min_val <= value <= max_val:
                    validated_params[key] = value
                else:
                    logger.warning(f"⚠️ [安全閥門] AI 給出的 {key} ({value}) 超出安全範圍 [{min_val}-{max_val}]，已拒絕修改。")
            else:
                # 如果是未定義的參數，允許通過但記錄
                validated_params[key] = value
        
        return validated_params

    def apply_ai_updates(self, diagnoses: List[Dict]):
        """將通過驗證的建議寫入配置檔案。"""
        if not diagnoses:
            return

        try:
            with open(self.config_path, "r", encoding="utf-8") as f:
                config = json.load(f)

            updated_count = 0
            for diag in diagnoses:
                sym = diag["symbol"]
                if sym in config.get("profiles", {}):
                    validated = self.validate_suggestion(sym, diag)
                    if validated:
                        # 更新 profiles 中的數據
                        config["profiles"][sym].update(validated)
                        logger.info(f"✅ [AI 優化] 已更新 {sym} 參數: {validated}")
                        updated_count += 1
            
            if updated_count > 0:
                with open(self.config_path, "w", encoding="utf-8") as f:
                    json.dump(config, f, indent=4, ensure_ascii=False)
                logger.info(f"🚀 [AI 進化] 已成功寫入 {updated_count} 項更新至 {self.config_path}")

        except Exception as e:
            logger.error(f"更新配置檔案失敗: {e}")

    async def run_ai_diagnosis_cycle(self):
        """主診斷循環：每隔一段時間自動執行一次分析。"""
        logger.info("🤖 [AI 大腦] 啟動診斷週期...")
        memories = self._get_recent_memories(limit=50)
        
        # 使用 asyncio.to_thread 避免 requests.post 阻塞主執行緒
        diagnoses = await asyncio.to_thread(self._fetch_ai_diagnosis, memories)
        
        if diagnoses:
            for diag in diagnoses:
                # 只有信心分數高於 0.7 的建議才執行更新
                if diag.get("confidence_score", 0) >= 0.7:
                    self.apply_ai_updates([diag])
                else:
                    logger.info(f"ℹ️ [AI 建議] {diag['symbol']} 診斷信心低 ({diag.get('confidence_score', 0)})，跳過自動更新。")
        
        logger.info("🤖 [AI 大腦] 診斷週期結束。")

# 實例化
ai_engine = AIManager()
