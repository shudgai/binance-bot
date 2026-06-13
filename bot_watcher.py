import subprocess
import asyncio
import aiohttp
import os
import time

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")

async def ask_claude(error_log: str) -> str:
    async with aiohttp.ClientSession() as session:
        resp = await session.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json"
            },
            json={
                "model": "claude-3-5-sonnet-20240620", # Changed from claude-sonnet-4-6 which doesn't exist to current version
                "max_tokens": 500,
                "messages": [{
                    "role": "user",
                    "content": f"這是我的交易 bot 崩潰的錯誤 log，請用繁體中文簡短說明原因和修復方法：\n\n{error_log}"
                }]
            }
        )
        data = await resp.json()
        if "content" in data:
            return data["content"][0]["text"]
        else:
            return f"Error from Claude API: {data}"

async def send_telegram(msg: str):
    if not TELEGRAM_TOKEN:
        return
    async with aiohttp.ClientSession() as session:
        await session.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": msg}
        )

async def run_bot():
    while True:
        print("▶️ 啟動 bot...")
        proc = await asyncio.create_subprocess_exec(
            ".venv/bin/python", "multi_coin_bot_v2.py", # Fixed from multi_coin_bot.py to _v2
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd="/home/shudgai999/project/binance-bot"
        )

        # 收集 log
        log_lines = []
        async for line in proc.stdout:
            text = line.decode("utf-8", errors="ignore")
            print(text, end="")
            log_lines.append(text)
            if len(log_lines) > 100:
                log_lines.pop(0)  # 只保留最後 100 行

        await proc.wait()
        exit_code = proc.returncode

        if exit_code != 0:
            error_log = "".join(log_lines[-30:])  # 最後 30 行
            print(f"❌ Bot 崩潰 (exit {exit_code})，正在分析...")

            # 問 Claude
            try:
                if ANTHROPIC_API_KEY:
                    suggestion = await ask_claude(error_log)
                else:
                    suggestion = "未設定 ANTHROPIC_API_KEY，跳過分析。"
            except Exception as e:
                suggestion = f"Claude 分析失敗: {e}"

            # 傳 Telegram
            msg = (
                f"🚨 Bot 崩潰了！(exit code: {exit_code})\n\n"
                f"📋 最後錯誤:\n{error_log[-500:]}\n\n"
                f"🤖 Claude 分析:\n{suggestion}"
            )
            await send_telegram(msg)
            print(f"🤖 Claude 建議: {suggestion}")

        print("🔄 10 秒後重啟...")
        await asyncio.sleep(10)

if __name__ == "__main__":
    asyncio.run(run_bot())
