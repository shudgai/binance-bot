import json
import httpx
from typing import List, Optional, Literal
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

app = FastAPI(title="AI Trading Analysis Server")

# 1. 定義 Pydantic 資料結構
class TradeData(BaseModel):
    timestamp: int = Field(..., description="Unix 標記時間")
    price: float = Field(..., description="收盤價")
    volume: float = Field(..., description="成交量")
    rsi: Optional[float] = Field(None, description="RSI 指標 (可選)")
    macd: Optional[float] = Field(None, description="MACD 指標 (可選)")

class AnalyzeRequest(BaseModel):
    symbol: str = Field(..., description="交易對 (例如 BTCUSDT)")
    data: List[TradeData] = Field(..., description="時間序列交易數據")

class AnalyzeResponse(BaseModel):
    decision: Literal["BUY", "SELL", "HOLD"]
    confidence: float = Field(..., ge=0.0, le=1.0)
    analysis: str = Field(..., max_length=500)

# System Prompt 限制 AI 的角色與輸出格式
SYSTEM_PROMPT = """你是一位專業的「定量交易分析師」。
請根據使用者提供的時間序列交易數據，給出下一個步驟的交易決策。
你的回應【必須】是純 JSON 格式，且必須包含以下三個欄位：
- "decision": 只能是 "BUY", "SELL", 或 "HOLD" 之一。
- "confidence": 0.0 到 1.0 之間的浮點數，表示你對此決策的信心程度。
- "analysis": 100 字以內的簡短分析理由。
請不要輸出任何多餘的解釋或 Markdown 標記，僅輸出 JSON。"""

LLAMA_API_URL = "http://127.0.0.1:8888/v1/chat/completions"

@app.post("/analyze", response_model=AnalyzeResponse)
async def analyze_data(request: AnalyzeRequest):
    # 2. 將接收到的數據轉成 JSON 字串作為 User Prompt
    user_prompt = json.dumps(request.model_dump(), ensure_ascii=False)
    
    # 3. 準備發送給 llama.cpp 的 payload
    payload = {
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt}
        ],
        "temperature": 0.2,
        "response_format": {"type": "json_object"}
    }
    
    # 4. 非同步呼叫 llama.cpp 的 API
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(LLAMA_API_URL, json=payload)
            response.raise_for_status()
            response_data = response.json()
            
            # 從回傳結果中提取 AI 輸出的文字
            content = response_data["choices"][0]["message"]["content"]
            
            # 5. 使用 json.loads 驗證與解析 JSON 格式
            decision_json = json.loads(content)
            
            # 回傳給發送端，FastAPI 會利用 AnalyzeResponse 再次驗證格式
            return decision_json

    except httpx.RequestError as e:
        raise HTTPException(status_code=502, detail=f"與 llama.cpp 伺服器連線失敗: {str(e)}")
    except json.JSONDecodeError:
        raise HTTPException(status_code=500, detail="AI 回傳的格式不是有效的 JSON")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"系統發生錯誤: {str(e)}")

# 啟動指令提示：
# uvicorn ai_server:app --host 0.0.0.0 --port 8000 --reload
