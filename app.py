import json
import os
from flask import Flask, jsonify

app = Flask(__name__)

STATE_FILE = "bot_state.json"
PAPER_STATE_FILE = "paper_state.json"

def get_bot_status():
    try:
        with open(STATE_FILE, "r") as f:
            bot_state = json.load(f)
        
        with open(PAPER_STATE_FILE, "r") as f:
            paper_state = json.load(f)
            
        # 計算總已實現利潤
        # 這裡我們從 paper_state.json 的 trades 紀錄中計算
        # 為了效能，如果檔案很大，這裡可能需要優化
        total_realized_pnl = 0.0
        for trade in paper_state.get("trades", []):
            if trade.get("is_close"):
                total_realized_pnl += trade.get("realized_pnl", 0.0)

        return {
            "is_running": bot_state.get("is_running", False),
            "active_symbols": bot_state.get("active_symbols", []),
            "watch_symbols": bot_state.get("watch_symbols", []),
            "trade_amount": bot_state.get("trade_amount", 0.0),
            "balance_quote": paper_state.get("balance_usdt", 0.0),
            "session_start_balance": 150.0,  # 這裡可以根據需求動態調整或從檔案讀取
            "pnl_realized": total_realized_pnl,
            "regime": "NEUTRAL"  # 這裡可以擴充從機器人獲取動態 regime
        }
    except Exception as e:
        return {
            "is_running": False,
            "active_symbols": [],
            "watch_symbols": [],
            "trade_amount": 0.0,
            "balance_quote": 0.0,
            "session_start_balance": 150.0,
            "pnl_realized": 0.0,
            "regime": "ERROR"
        }

@app.route('/api/bot-status', methods=['GET'])
def bot_status():
    return jsonify(get_bot_status())

@app.route('/api/positions', methods=['GET'])
def positions():
    try:
        with open(PAPER_STATE_FILE, "r") as f:
            paper_state = json.load(f)
        return jsonify(paper_state.get("positions", {}))
    except Exception as e:
        return jsonify({})


if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)