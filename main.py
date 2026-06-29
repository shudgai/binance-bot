"""
main.py — Thin entry-point (refactored)
All trading logic lives in core/*.py modules.
"""
import asyncio
import fcntl
import os
import sys

from dotenv import load_dotenv

load_dotenv()

sys.path.append(os.path.dirname(os.path.abspath(__file__)))

# ── Single-instance lock ──────────────────────────────────────
LOCK_FILE = "/tmp/binance_bot_32f2e2ed.lock"
lock_file_handle = None


def _process_exists(pid):
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def ensure_single_instance():
    global lock_file_handle

    lock_file_handle = open(LOCK_FILE, "a+")
    try:
        fcntl.flock(lock_file_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        lock_file_handle.seek(0)
        lock_file_handle.truncate()
        lock_file_handle.write(str(os.getpid()))
        lock_file_handle.flush()
        return

    except IOError:
        lock_file_handle.seek(0)
        pid_text = lock_file_handle.read().strip()
        stale_pid = None
        try:
            stale_pid = int(pid_text)
        except ValueError:
            pass

        if stale_pid and stale_pid != os.getpid():
            if _process_exists(stale_pid):
                print(f"ℹ️ [防禦分流] 偵測到已有核心在盯盤 (PID={stale_pid})，本多餘執行緒自動退出。")
                sys.exit(0)
            else:
                print(f"⚠️ 偵測到鎖定進程 PID={stale_pid} 已不存在，清理過期鎖檔並重新接管...")
                try:
                    fcntl.flock(lock_file_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    lock_file_handle.seek(0)
                    lock_file_handle.truncate()
                    lock_file_handle.write(str(os.getpid()))
                    lock_file_handle.flush()
                    return
                except IOError:
                    pass

        print("🚨 錯誤: 偵測到系統中已有另一個機器人正在執行！")
        print("💡 為了避免重複下單與邏輯衝突，本次啟動已自動攔截並退出。")
        print(f"💡 提示: 若是意外關閉舊程式，請先手動刪除鎖定檔：\n   rm -f {LOCK_FILE}\n  然後再重新啟動。")
        sys.exit(1)


# ── Bootstrap: initialise shared state then start ────────────
if __name__ == "__main__":
    ensure_single_instance()

    # Import after single-instance check so we don't initialise twice
    from core.ctx import init_states
    from core.symbol_profile import load_symbol_pool, load_symbol_profiles
    from core.config import DEFAULT_SYMBOLS
    from core.runner import main
    from core.exchange_client import exchange_futures

    # Initialise shared state
    symbols = load_symbol_pool() or list(DEFAULT_SYMBOLS)
    load_symbol_profiles()
    init_states(symbols)

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n🛑 程式已被手動終止 (KeyboardInterrupt)")
    except Exception as e:
        import traceback
        print(f"\n🚨 核心運行遭遇未捕獲異常: {e}", file=sys.stderr)
        traceback.print_exc()
    # exchange_futures.close() 已在 core.runner.main() 的 finally 中處理（同一 event loop）
