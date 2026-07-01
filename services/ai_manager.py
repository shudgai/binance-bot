import json
import os
import requests
import time
import logging
import asyncio
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
        context_text = ""
        for m in memories:
            context_text += f"- {m.get('ai_summary', '無摘要')}\n"

        prompt = f"""
You are a Quantitative Trading Expert specializing in "ATR-based Dynamic Position Sizing with Pyramiding".
Analyze the following recent trade summaries and their CURRENT parameters, and identify issues in friction, stop-loss sensitivity, or pyramiding efficiency.

Current Parameters of Symbols:
{json.dumps(current_configs, indent=2, ensure_ascii=False)}

Recent Trade Summaries:
{context_text}

Task:
1. Identify any recurring problems (e.g., High Friction, Frequent Stop-outs, Poor Pyramiding) by comparing the trade outcomes with the current parameters of those symbols.
2. Provide specific parameter adjustment suggestions for the symbols involved. Make sure you don't adjust parameters in the wrong direction (e.g. do not reduce sl_atr_multiplier if the symbol is suffering from frequent stop-outs).

Output Format (Strict JSON):
{{
  "diagnoses": [
    {{
      "symbol": "SYMBOL_NAME",
      "reason": "Brief explanation of the issue (compare current parameters vs trade summary)",
      "suggested_params": {{
        "sl_atr_multiplier": 2.5,
        "tp_atr_multiplier": 4.0,
        "add_entry_pct": 0.3,
        "leverage": 5
      }},
      "confidence_score": 0.9
    }}
  ]
}}
"""
        
        try:
            # 這裡以 OpenAI 為例，若使用 Claude 請更換請求頭與 URL
            response = requests.post(
                "https://api.openai.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {AI_API_KEY}", "Content-Type": "application/json"},
                data=json.dumps({
                    "model": AI_MODEL,
                    "messages": [{"role": "system", "content": "You are a helpful trading assistant."},
                                  {"role": "user", "content": prompt}],
                    "response_format": {"type": "json_object"}
                }),
                timeout=30
            )
            res_json = response.json()
            content = json.loads(res_json["choices"][0]["message"]["content"])
            return content.get("diagnoses", [])
        except Exception as e:
            logger.error(f"AI API 調用失敗: {e}")
            return None

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
