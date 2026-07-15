import json
import os
import requests
import time
import logging
import asyncio
from pathlib import Path
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Any, Optional
from dotenv import load_dotenv

load_dotenv()

# --- 配置 ---
AI_API_KEY = os.getenv("AI_COMPAT_API_KEY") or os.getenv("OPENAI_API_KEY")
AI_EXTERNAL_REVIEW_ENABLED = os.getenv("AI_EXTERNAL_REVIEW_ENABLED", "false").lower() == "true"
AI_BASE_URL = (os.getenv("AI_COMPAT_BASE_URL") or os.getenv("OPENAI_BASE_URL") or "https://api.openai.com/v1").rstrip("/")
AI_MODEL = os.getenv("AI_COMPAT_MODEL") or os.getenv("OPENAI_MODEL", "gpt-5.6-luna")
AI_REQUEST_TIMEOUT_SEC = float(os.getenv("AI_REQUEST_TIMEOUT_SEC", "90"))
AI_MAX_TOKENS = int(os.getenv("AI_MAX_TOKENS", "2048"))
AI_AUTO_REVIEW_ENABLED = os.getenv("AI_AUTO_REVIEW_ENABLED", "false").lower() == "true"
AI_AUTO_REVIEW_EVERY_TRADES = max(1, int(os.getenv("AI_AUTO_REVIEW_EVERY_TRADES", "5")))
AI_REPORT_FILE = os.path.join(os.path.dirname(__file__), "..", "data", "ai_latest_report.json")
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
        self.report_path = AI_REPORT_FILE
        self._history_cache_mtime = None
        self._history_cache = []
        self._auto_review_baseline_count = None
        self._auto_review_task = None

    def _prune_raw_responses(self, raw_dir: Path, keep_days: int = 7):
        """刪除 raw responses 目錄中超過 keep_days 的檔案。"""
        cutoff = datetime.now(timezone.utc) - timedelta(days=keep_days)
        for f in raw_dir.glob('response_*.txt'):
            try:
                mtime = datetime.fromtimestamp(f.stat().st_mtime, timezone.utc)
                if mtime < cutoff:
                    f.unlink()
                    logger.info(f"🧹 刪除舊的 AI raw response: {f}")
            except Exception as e:
                logger.warning(f"刪除舊 AI raw response 時發生錯誤: {e} (file: {f})")

    def _get_recent_memories(self, limit: int = 50) -> List[Dict]:
        """讀取最近交易並依檔案 mtime 快取，避免每輪候選排序重複讀檔。"""
        if not os.path.exists(self.history_path):
            self._history_cache_mtime = None
            self._history_cache = []
            return []
        try:
            mtime = os.path.getmtime(self.history_path)
            if self._history_cache_mtime != mtime:
                with open(self.history_path, "r", encoding="utf-8") as f:
                    history = json.load(f)
                self._history_cache = history if isinstance(history, list) else []
                self._history_cache_mtime = mtime
            return self._history_cache[-limit:]
        except Exception as e:
            logger.error(f"讀取歷史紀錄失敗: {e}")
            return []

    def get_candidate_quality_adjustment(self, symbol: str, route: str) -> float:
        """Return a bounded history-only ranking adjustment; never changes entry direction or guards."""
        symbol = str(symbol or "").upper()
        route = str(route or "").lower()
        matching = [
            row for row in self._get_recent_memories(limit=200)
            if str(row.get("symbol", "")).upper() == symbol
            and str(row.get("entry_reason", "")).lower() == route
            and isinstance(row.get("profit_pct"), (int, float))
        ][-20:]
        if len(matching) < 5:
            return 0.0
        profits = [float(row.get("profit_pct", 0.0) or 0.0) for row in matching]
        avg_profit = sum(profits) / len(profits)
        win_rate = sum(1 for value in profits if value > 0.0) / len(profits)
        if avg_profit >= 0.002 and win_rate >= 0.60:
            adjustment = 2.0
        elif avg_profit > 0.0 and win_rate >= 0.50:
            adjustment = 1.0
        elif avg_profit <= -0.002 or win_rate <= 0.30:
            adjustment = -2.0
        elif avg_profit < 0.0 or win_rate < 0.45:
            adjustment = -1.0
        else:
            adjustment = 0.0
        if all(value < 0.0 for value in profits[-3:]):
            adjustment -= 0.5
        avg_friction = sum(float(row.get("friction_rate", 0.0) or 0.0) for row in matching) / len(matching)
        if avg_friction > 0.30:
            adjustment -= 0.5
        return round(max(-2.0, min(2.0, adjustment)), 2)

    def build_local_analysis(self) -> Dict:
        """Build a deterministic post-trade report without network calls or trading mutations."""
        memories = self._get_recent_memories(limit=100)
        grouped = {}
        anomalies = 0
        for row in memories:
            route = str(row.get("entry_reason", "UNKNOWN") or "UNKNOWN")
            bucket = grouped.setdefault(route, {"trades": 0, "wins": 0, "profit_sum": 0.0})
            profit = float(row.get("profit_pct", 0.0) or 0.0)
            bucket["trades"] += 1
            bucket["wins"] += int(profit > 0.0)
            bucket["profit_sum"] += profit
            anomalies += int(bool(row.get("ai_anomaly_tags")))
        routes = {}
        for route, bucket in grouped.items():
            count = bucket["trades"]
            routes[route] = {
                "trades": count,
                "win_rate": round(bucket["wins"] / count, 4) if count else 0.0,
                "avg_profit_pct": round(bucket["profit_sum"] / count, 6) if count else 0.0,
            }
        return {
            "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "sample_count": len(memories),
            "anomaly_count": anomalies,
            "routes": routes,
            "auto_apply": False,
        }

    def get_latest_report(self) -> Dict:
        try:
            if os.path.exists(self.report_path):
                with open(self.report_path, "r", encoding="utf-8") as f:
                    report = json.load(f)
                if isinstance(report, dict):
                    return report
        except Exception as e:
            logger.warning(f"讀取 AI 報告失敗: {e}")
        return {"local_analysis": self.build_local_analysis(), "external_diagnoses": [], "auto_apply": False}

    def _fetch_ai_diagnosis(self, memories: List[Dict]) -> Optional[List[Dict]]:
        """發送數據給 AI 並獲取診斷結果。"""
        if not memories:
            return None

        # 只傳送交易複盤需要的欄位，不傳動態幣種設定，縮短上下文並避免模型誤認為可調參。
        review_rows = [
            {key: row.get(key) for key in (
                "symbol", "entry_reason", "exit_reason", "profit_pct",
                "max_profit_reached", "market_mode", "friction_rate", "ai_anomaly_tags"
            )}
            for row in memories
        ]
        prompt = "Recent closed trades: " + json.dumps(review_rows, ensure_ascii=False) + "\n\n"
        prompt += (
            "Analyze only and return JSON only. Use exactly {\"diagnoses\": [...]} with at most "
            "3 highest-priority items. Each item must contain symbol, confidence_score (0 to 1), "
            "observed_pattern, risk_flags, and review_note. Keep observed_pattern and review_note "
            "under 160 characters each. Do not propose orders, parameter changes, or guard bypasses."
        )
        
        # Prepare request payload
        req_payload = {
            "model": AI_MODEL,
            "messages": [
                {"role": "system", "content": "You are a read-only trading review assistant. Never issue orders, change direction, modify risk controls, or recommend bypassing MA, BTC, support-resistance, stop, or slot guards."},
                {"role": "user", "content": prompt}
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0,
            "max_tokens": AI_MAX_TOKENS,
            "chat_template_kwargs": {"enable_thinking": False},
        }

        # Ensure directory for raw responses exists
        raw_dir = Path(os.path.join(os.path.dirname(__file__), '..', 'data', 'ai_raw_responses')).resolve()
        raw_dir.mkdir(parents=True, exist_ok=True)
        # prune old raw responses to avoid disk filling
        try:
            self._prune_raw_responses(raw_dir, keep_days=7)
        except Exception:
            logger.warning("無法執行 raw responses 清理，但繼續執行 API 呼叫")

        max_attempts = 3
        backoff_base = 1.0
        for attempt in range(1, max_attempts + 1):
            try:
                headers = {"Content-Type": "application/json"}
                if AI_API_KEY:
                    headers["Authorization"] = f"Bearer {AI_API_KEY}"
                response = requests.post(
                    f"{AI_BASE_URL}/chat/completions",
                    headers=headers,
                    data=json.dumps(req_payload),
                    timeout=AI_REQUEST_TIMEOUT_SEC
                )
            except requests.RequestException as e:
                logger.warning(f"AI API 請求失敗 (attempt {attempt}/{max_attempts}): {e}")
                if attempt < max_attempts:
                    time.sleep(backoff_base * (2 ** (attempt - 1)))
                    continue
                logger.error(f"AI API 請求最終失敗: {e}")
                return None

            # Save raw response if non-200 or content issues
            timestamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
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
                return None

            diagnoses = content.get("diagnoses", []) if isinstance(content, dict) else []
            if not isinstance(diagnoses, list):
                return []
            known_symbols = {str(row.get("symbol", "")).upper() for row in review_rows}
            safe_diagnoses = []
            for item in diagnoses[:3]:
                if not isinstance(item, dict):
                    continue
                symbol = str(item.get("symbol", "")).upper()[:20]
                if symbol not in known_symbols:
                    continue
                try:
                    confidence = max(0.0, min(1.0, float(item.get("confidence_score", 0.0))))
                except (TypeError, ValueError):
                    confidence = 0.0
                flags = item.get("risk_flags", [])
                if not isinstance(flags, list):
                    flags = []
                safe_diagnoses.append({
                    "symbol": symbol,
                    "confidence_score": round(confidence, 4),
                    "observed_pattern": str(item.get("observed_pattern", ""))[:160],
                    "risk_flags": [str(flag)[:64] for flag in flags[:5]],
                    "review_note": str(item.get("review_note", ""))[:160],
                })
            return safe_diagnoses

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
                logger.warning(f"⚠️ [安全閥門] AI 未知參數 {key} 已拒絕")
        
        return validated_params

    def _load_auto_review_baseline(self, history_count: int) -> int:
        if self._auto_review_baseline_count is not None:
            return self._auto_review_baseline_count
        baseline = None
        try:
            if os.path.exists(self.report_path):
                with open(self.report_path, "r", encoding="utf-8") as handle:
                    report = json.load(handle)
                baseline = report.get("analyzed_history_count")
                if baseline is None:
                    baseline = (report.get("local_analysis") or {}).get("sample_count")
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            baseline = None
        self._auto_review_baseline_count = int(baseline) if isinstance(baseline, (int, float)) else int(history_count)
        return self._auto_review_baseline_count

    def is_auto_review_due(self, history_count: int) -> bool:
        if not AI_EXTERNAL_REVIEW_ENABLED or not AI_AUTO_REVIEW_ENABLED:
            return False
        history_count = max(0, int(history_count))
        baseline = self._load_auto_review_baseline(history_count)
        if history_count < baseline:
            self._auto_review_baseline_count = history_count
            return False
        return history_count - baseline >= AI_AUTO_REVIEW_EVERY_TRADES

    def schedule_auto_review_if_due(self, history_count: int) -> bool:
        if not self.is_auto_review_due(history_count):
            return False
        if self._auto_review_task is not None and not self._auto_review_task.done():
            return False
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.warning("🤖 [AI 自動複盤] 目前無執行中的事件迴圈，本次略過")
            return False
        history_count = int(history_count)
        self._auto_review_baseline_count = history_count
        self._auto_review_task = loop.create_task(
            self.run_ai_diagnosis_cycle(), name="ai-auto-trade-review"
        )

        def _review_done(task):
            self._auto_review_task = None
            try:
                task.result()
            except Exception as exc:
                self._auto_review_baseline_count = max(0, history_count - AI_AUTO_REVIEW_EVERY_TRADES)
                logger.error(f"🤖 [AI 自動複盤] 執行失敗：{exc}")

        self._auto_review_task.add_done_callback(_review_done)
        logger.info(f"🤖 [AI 自動複盤] 新增 {AI_AUTO_REVIEW_EVERY_TRADES} 筆平倉交易，已排入背景分析")
        return True

    def apply_ai_updates(self, diagnoses: List[Dict]):
        """Compatibility safety stop: AI reports are read-only and never mutate trading config."""
        if diagnoses:
            logger.warning("🛑 [AI 只讀模式] 已拒絕自動改寫交易參數")
        return 0

    async def run_ai_diagnosis_cycle(self):
        """Run an on-demand read-only review; never update trading configuration."""
        logger.info("🤖 [AI 複盤] 啟動只讀診斷")
        memories = self._get_recent_memories(limit=50)
        total_history_count = len(self._history_cache)
        local_analysis = self.build_local_analysis()
        diagnoses = await asyncio.to_thread(self._fetch_ai_diagnosis, memories) if AI_EXTERNAL_REVIEW_ENABLED else []
        report = {
            "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "local_analysis": local_analysis,
            "external_diagnoses": diagnoses or [],
            "external_ai_used": bool(AI_EXTERNAL_REVIEW_ENABLED and diagnoses),
            "external_ai_enabled": AI_EXTERNAL_REVIEW_ENABLED,
            "external_ai_model": AI_MODEL,
            "analyzed_history_count": total_history_count,
            "auto_review_enabled": AI_AUTO_REVIEW_ENABLED,
            "auto_review_every_trades": AI_AUTO_REVIEW_EVERY_TRADES,
            "auto_apply": False,
        }
        try:
            with open(self.report_path, "w", encoding="utf-8") as f:
                json.dump(report, f, ensure_ascii=False, indent=2)
            self._auto_review_baseline_count = total_history_count
        except Exception as e:
            logger.warning(f"AI 報告寫入失敗: {e}")
        logger.info("🤖 [AI 複盤] 診斷完成；交易參數未變更")
        return report

# 實例化
ai_engine = AIManager()
