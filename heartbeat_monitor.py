import time
import os
import subprocess
import json
from line_notifier import send_line_alert
from dotenv import load_dotenv

load_dotenv()

BOT_FILES = ["multi_coin_bot.py", "futures_bot.py"]
CHECK_INTERVAL_SEC = 60

def is_process_running(script_name):
    try:
        # 使用 pgrep 或 ps 檢查進程
        cmd = f"ps aux | grep {script_name} | grep -v grep"
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
        return len(result.stdout.strip()) > 0
    except Exception as e:
        print(f"檢查進程失敗: {e}")
        return False

def check_paper_state():
    try:
        if not os.path.exists("paper_state.json"):
            return True, "檔案不存在，略過檢查"
            
        with open("paper_state.json", "r") as f:
            state = json.load(f)
            
        # 基本完整性檢查
        balance = state.get("balance_usdt")
        positions = state.get("positions", {})
        
        if balance is None:
            return False, "paper_state.json 缺少 balance_usdt 欄位"
            
        if not isinstance(positions, dict):
            return False, "paper_state.json 的 positions 欄位格式錯誤"
            
        return True, "狀態檔正常"
    except json.JSONDecodeError:
        return False, "paper_state.json 格式毀損 (JSONDecodeError)"
    except Exception as e:
        return False, f"讀取 paper_state.json 發生異常: {e}"

def main():
    print("💓 [心跳監控] 啟動中，每 60 秒檢查一次機器人健康狀態...")
    
    # 初始化警報狀態，避免重複狂發
    alert_triggered = {bot: False for bot in BOT_FILES}
    state_alert_triggered = False
    
    while True:
        try:
            # 1. 檢查機器人進程
            active_bots = []
            for bot in BOT_FILES:
                if is_process_running(bot):
                    active_bots.append(bot)
                    if alert_triggered[bot]:
                        send_line_alert(f"✅ [心跳恢復] {bot} 已重新上線恢復運行。")
                        alert_triggered[bot] = False
                else:
                    # 如果該機器人本來就沒在跑，這裡會一直觸發，所以在實務上可以針對「預期要跑的腳本」發布警報
                    # 為了避免打擾，我們只在確定該跑的機器人死掉時報警
                    # 此處簡單處理：如果需要嚴格監控，可將其改為 True
                    pass
            
            # 若所有核心機器人都沒在跑，且之前沒報警過
            if len(active_bots) == 0 and not alert_triggered.get('ALL', False):
                error_msg = "🚨 [心跳異常] 所有的交易機器人進程 (multi_coin_bot.py / futures_bot.py) 皆已停止運行！請立即檢查伺服器狀態！"
                print(error_msg)
                send_line_alert(error_msg)
                alert_triggered['ALL'] = True
            elif len(active_bots) > 0 and alert_triggered.get('ALL', False):
                alert_triggered['ALL'] = False

            # 2. 檢查狀態檔完整性
            is_state_ok, state_msg = check_paper_state()
            if not is_state_ok and not state_alert_triggered:
                error_msg = f"🚨 [狀態檔異常] paper_state.json 發生錯誤: {state_msg}"
                print(error_msg)
                send_line_alert(error_msg)
                state_alert_triggered = True
            elif is_state_ok and state_alert_triggered:
                send_line_alert(f"✅ [狀態檔恢復] paper_state.json 已恢復正常。")
                state_alert_triggered = False

            time.sleep(CHECK_INTERVAL_SEC)
            
        except KeyboardInterrupt:
            print("心跳監控手動停止。")
            break
        except Exception as e:
            print(f"心跳監控本身發生異常: {e}")
            time.sleep(CHECK_INTERVAL_SEC)

if __name__ == "__main__":
    main()
