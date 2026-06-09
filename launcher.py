import subprocess
import time
import sys

# 這裡就是您的「幣種池」，想加幾個幣就加幾個
symbols = ["SOL/USDT", "BTC/USDT", "ETH/USDT"]

def start_bot(symbol):
    print(f"🚀 啟動幣種: {symbol}")
    # 這裡會執行您原本的程式，並帶入參數
    return subprocess.Popen(["python3", "futures_bot.py", "--symbol", symbol, "--amount", "10"])

active_bots = {}

try:
    print("🤖 系統啟動中...")
    for sym in symbols:
        active_bots[sym] = start_bot(sym)
        time.sleep(3) # 避免 API 被瞬間觸發頻率限制

    print("\n✅ 所有機器人已在背景執行。按 Ctrl+C 可停止所有機器人。")
    while True:
        time.sleep(10)
except KeyboardInterrupt:
    print("\n🛑 正在關閉所有機器人...")
    for sym, proc in active_bots.items():
        proc.terminate()
    print("✅ 所有機器人已停止。")
    sys.exit(0)
