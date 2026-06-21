import sys

with open("multi_coin_bot.py", "r", encoding="utf-8") as f:
    code = f.read()

# 1. Add import
if "from ai_manager import ai_engine" not in code:
    code = code.replace("import asyncio\n", "import asyncio\nfrom ai_manager import ai_engine\n")

# 2. Add AI check in main_loop
ai_check_code = """
            # --- AI 大腦診斷 ---
            if time.time() % 1800 < 6: # 每 30 分鐘執行一次
                asyncio.create_task(ai_engine.run_ai_diagnosis_cycle())
"""
if "run_ai_diagnosis_cycle" not in code:
    code = code.replace("update_all_dynamic_personalities()", "update_all_dynamic_personalities()\n" + ai_check_code)

with open("multi_coin_bot.py", "w", encoding="utf-8") as f:
    f.write(code)

print("Applied ai_manager modifications!")
