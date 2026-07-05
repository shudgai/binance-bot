import os
import logging
import requests

LINE_TOKEN = os.getenv("LINE_NOTIFY_TOKEN")
logger = logging.getLogger(__name__)

def send_line_alert(msg: str):
    if not LINE_TOKEN:
        return
    try:
        requests.post(
            "https://notify-api.line.me/api/notify",
            headers={"Authorization": f"Bearer {LINE_TOKEN}"},
            data={"message": msg},
            timeout=5
        )
    except requests.RequestException as e:
        logger.warning(f"⚠️ LINE 通知發送失敗: {e}")
