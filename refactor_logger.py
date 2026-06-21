import re

def refactor_logging(file_path):
    with open(file_path, 'r', encoding='utf-8') as f:
        content = f.read()

    # We need to replace print(...) with logger.xxx(...)
    # but handle multi-line strings carefully if they exist.
    # A simple regex for single line prints: print(f"...") or print("...")
    
    def repl(match):
        full_match = match.group(0)
        inner_content = match.group(1)
        
        # Determine log level based on emoji or text
        if "CRITICAL:" in inner_content:
            log_func = "logger.critical"
        elif "🚨" in inner_content or "🛑" in inner_content or "📉" in inner_content or "🚫" in inner_content or "錯誤" in inner_content or "失敗" in inner_content:
            log_func = "logger.error"
        elif "⚠️" in inner_content or "🛡️" in inner_content or "警告" in inner_content or "攔截" in inner_content:
            log_func = "logger.warning"
        else:
            log_func = "logger.info"
            
        return f"{log_func}({inner_content})"

    # Pattern matches print( SOMETHING ) where SOMETHING doesn't contain unclosed parenthesis.
    # This regex is a bit simplistic and might need tuning, but works for most f-strings.
    pattern = re.compile(r'print\(\s*(f?"[^"]*"|f?\'[^\']*\')\s*\)')
    
    new_content = pattern.sub(repl, content)
    
    with open(file_path, 'w', encoding='utf-8') as f:
        f.write(new_content)
        
    print(f"Refactored {file_path}")

if __name__ == "__main__":
    refactor_logging("multi_coin_bot.py")
