"""
main.py — Thin entry-point (refactored)
All trading logic lives in core/*.py modules.
"""
import asyncio
import fcntl
import os
import signal
import sys
import time

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


def _terminate_process(pid):
    try:
        os.kill(pid, signal.SIGTERM)
        time.sleep(0.2)
        if _process_exists(pid):
            os.kill(pid, signal.SIGKILL)
        return True
    except Exception:
        return False


def ensure_single_instance():
    global lock_file_handle

    def _create_lock():
        global lock_file_handle
        try:
            if lock_file_handle:
                try:
                    lock_file_handle.close()
                except Exception:
                    pass
            try:
                os.remove(LOCK_FILE)
            except Exception:
                pass
            lock_file_handle = open(LOCK_FILE, "a+")
            fcntl.flock(lock_file_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            lock_file_handle.seek(0)
            lock_file_handle.truncate()
            lock_file_handle.write(str(os.getpid()))
            lock_file_handle.flush()
            return True
        except IOError:
            return False

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
        except Exception:
            pass

        if stale_pid and stale_pid != os.getpid():
            if _process_exists(stale_pid):
                print(f"ℹ️ [防禦分流] 偵測到已有核心在盯盤 (PID={stale_pid})，本多餘執行緒自動退出。")
                sys.exit(0)
            else:
                print(f"⚠️ 偵測到鎖定進程 PID={stale_pid} 已不存在，清理過期鎖檔並繼續啟動...")

            if _create_lock():
                return

        print("🚨 錯誤: 偵測到系統中已有另一個機器人正在執行！")
        print("💡 為了避免重複下單與邏輯衝突，本次啟動已自動攔截並退出。")
        print("💡 提示: 若是意外關閉舊程式，請先刪除過期的鎖定檔 /tmp/binance_bot_v2.lock，再重新啟動。")
        sys.exit(1)


# ── Bootstrap: initialise shared state then start ────────────
if __name__ == "__main__":
    ensure_single_instance()

    # Import after single-instance check so we don't initialise twice
    from core.ctx import init_states
    from core.symbol_profile import load_symbol_pool, load_symbol_profiles
    from core.config import DEFAULT_SYMBOLS
    from core.runner import main
    from core.exchange_client import exchange_futures, exchange_spot
    import core.ctx as ctx

    # Initialise shared state
    symbols = load_symbol_pool() or list(DEFAULT_SYMBOLS)
    load_symbol_profiles()
    init_states(symbols)

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("🛑 程式已被手動終止")
    finally:
        async def _cleanup():
            await exchange_futures.close()
            await exchange_spot.close()
        asyncio.run(_cleanup())
