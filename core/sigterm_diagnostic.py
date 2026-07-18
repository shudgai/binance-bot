"""
SIGTERM 診斷處理器
====================================
用於排查過去發生過的 exit -15 意外重啟，記錄收到 SIGTERM 當下
所有執行緒的堆疊快照，方便之後判斷是外部強制終止還是程式內部問題。

已確認全專案（排除 .venv/.claude 等第三方與暫存目錄）目前完全沒有
任何地方在處理 SIGTERM/SIGINT，因此本模組不會與現有「系統守護」
重啟機制衝突。
"""

import signal
import os
import sys
import traceback
import threading
import logging
from datetime import datetime

logger = logging.getLogger(__name__)


def intentional_stop_marker_path(pid: int) -> str:
    """Marker shared with the parent watchdog for an expected SIGTERM."""
    return f"/tmp/binance_bot_intentional_stop_{int(pid)}"


def _dump_all_thread_stacks() -> str:
    """把目前所有執行緒的呼叫堆疊組成文字，方便寫進 log 或存檔。"""
    lines = [f"=== SIGTERM 收到時的執行緒堆疊快照 {datetime.now().isoformat()} ==="]
    for thread_id, frame in sys._current_frames().items():
        thread_name = next(
            (t.name for t in threading.enumerate() if t.ident == thread_id),
            f"Thread-{thread_id}",
        )
        lines.append(f"\n--- Thread: {thread_name} (id={thread_id}) ---")
        lines.append("".join(traceback.format_stack(frame)))
    return "\n".join(lines)


def install_sigterm_diagnostic_handler(dump_to_file: bool = True):
    """
    在程式啟動時呼叫一次即可（建議放在 main.py 的 logging.basicConfig 之後，
    比任何交易邏輯都早初始化）。

    收到 SIGTERM 時：
      1. 記錄當下所有執行緒的堆疊到 log
      2. 若 dump_to_file=True，額外寫一份帶時間戳的檔案，方便事後追查
      3. 恢復預設行為，讓程式照原本方式終止（不吃掉這個訊號、
         不影響既有的系統守護自動重啟機制）
    """

    def _handler(signum, frame):
        marker = intentional_stop_marker_path(os.getpid())
        if os.path.exists(marker):
            try:
                os.remove(marker)
            except OSError:
                pass
            logger.info("ℹ️ [SIGTERM] 管理程序要求停止，正常結束")
            signal.signal(signal.SIGTERM, signal.SIG_DFL)
            signal.raise_signal(signal.SIGTERM)
            return

        snapshot = _dump_all_thread_stacks()
        logger.error(f"⚠️ [SIGTERM_DIAG] 收到 SIGTERM，程式即將終止。堆疊快照：\n{snapshot}")

        if dump_to_file:
            try:
                filename = f"sigterm_dump_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
                with open(filename, "w", encoding="utf-8") as f:
                    f.write(snapshot)
                logger.error(f"⚠️ [SIGTERM_DIAG] 堆疊快照已存檔：{filename}")
            except Exception as exc:
                logger.error(f"⚠️ [SIGTERM_DIAG] 存檔失敗：{exc}")

        # 恢復預設 SIGTERM 行為並重新拋出，讓程式正常終止
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
        signal.raise_signal(signal.SIGTERM)

    signal.signal(signal.SIGTERM, _handler)
    logger.info("✅ [SIGTERM_DIAG] SIGTERM 診斷處理器已安裝")
