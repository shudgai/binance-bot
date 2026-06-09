from flask import Flask, jsonify

app = Flask(__name__)

def get_current_positions():
    # 這裡應該連接到交易所 API 獲取持倉信息，這裡用一個示例返回
    return {
        "BTCUSDT": {"position_side": "buy", "quantity": 0.5},
        # "ETHUSDT": {"position_side": "sell", "quantity": 1.2}
    }

@app.route('/api/positions', methods=['GET'])
def positions():
    positions = get_current_positions()
    return jsonify(positions)

if __name__ == '__main__':
    app.run(debug=True)