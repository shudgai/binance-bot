import json
import logging
from typing import List, Optional, Literal
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
import aiohttp

# 初始化日誌
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ai_relay")

app = FastAPI(title="交易分析中轉伺服器", description="將交易數據轉送至 llama.cpp 進行 AI 分析")

# Llama.cpp 伺服器設定
LLAMA_API_URL = "http://127.0.0.1:8888/v1/chat/completions"

# System Prompt 設定
SYSTEM_PROMPT = """你是一位專業的「定量交易分析師」。
請根據使用者提供的時間序列交易數據進行分析，並給出你的交易決策。

你必須「嚴格」只輸出 JSON 格式，且 JSON 必須精確包含以下三個欄位，不能有任何其他欄位或多餘的文字說明：
1. "decision": 只能是 "BUY", "SELL", 或 "HOLD" 之一。
2. "confidence": 一個 0.0 到 1.0 之間的浮點數，代表你的信心程度。
3. "analysis": 100 字以內的簡短分析理由。

範例輸出：
{"decision": "BUY", "confidence": 0.85, "analysis": "價格突破且成交量放大，MACD呈現黃金交叉，多頭動能強勁。"}
"""

# 定義輸入數據結構
class TradeDataPoint(BaseModel):
    timestamp: int = Field(..., description="時間戳記")
    price: float = Field(..., description="價格")
    volume: float = Field(..., description="成交量")
    rsi: Optional[float] = Field(None, description="RSI指標 (可選)")
    macd: Optional[float] = Field(None, description="MACD數值 (可選)")

class AnalyzeRequest(BaseModel):
    symbol: str = Field(..., description="交易對名稱，例如 BTCUSDT")
    data: List[TradeDataPoint] = Field(..., description="時間序列交易數據")

# 定義輸出的資料結構 (僅供 OpenAPI 文件參考與驗證)
class AnalyzeResponse(BaseModel):
    decision: Literal["BUY", "SELL", "HOLD"]
    confidence: float
    analysis: str

@app.post("/analyze", response_model=AnalyzeResponse)
async def analyze_trading_data(request: AnalyzeRequest):
    # 將收到的時間序列數據轉成 JSON 字串，作為 User Prompt
    user_prompt = request.model_dump_json()
    
    # 建立發送給 llama.cpp 的 payload
    payload = {
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"請分析以下數據：\n{user_prompt}"}
        ],
        "temperature": 0.2,
        "response_format": {"type": "json_object"}
    }
    
    try:
        # 使用 aiohttp 非同步呼叫 llama.cpp
        async with aiohttp.ClientSession() as session:
            async with session.post(LLAMA_API_URL, json=payload, timeout=30) as resp:
                if resp.status != 200:
                    error_text = await resp.text()
                    logger.error(f"Llama.cpp 回應錯誤: {resp.status} - {error_text}")
                    raise HTTPException(status_code=502, detail="AI 模型伺服器錯誤")
                
                result = await resp.json()
    except Exception as e:
        logger.error(f"呼叫 AI 伺服器失敗: {str(e)}")
        raise HTTPException(status_code=500, detail=f"與 AI 伺服器連線失敗: {str(e)}")

    # 擷取 AI 回應內容
    ai_content = result.get("choices", [{}])[0].get("message", {}).get("content", "").strip()
    
    try:
        # 驗證 AI 的回應是否為合法的 JSON
        parsed_response = json.loads(ai_content)
        
        # 確保必要欄位存在
        if not all(k in parsed_response for k in ("decision", "confidence", "analysis")):
            raise ValueError("AI 回傳的 JSON 缺少必要欄位")
            
        return parsed_response
        
    except json.JSONDecodeError:
        logger.error(f"AI 回傳了非 JSON 格式的資料: {ai_content}")
        raise HTTPException(status_code=500, detail="AI 回傳格式錯誤，無法解析為 JSON")
    except Exception as e:
        logger.error(f"解析 AI 回應時發生錯誤: {str(e)}")
        raise HTTPException(status_code=500, detail=f"解析 AI 回應失敗: {str(e)}")

if __name__ == "__main__":
    import uvicorn
    # 若要獨立運行此中轉伺服器，可直接執行 python ai_relay_server.py
    uvicorn.run(app, host="0.0.0.0", port=8080)
