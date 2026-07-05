import sys
import os

# Add the project root to PYTHONPATH to allow importing from services
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.append(project_root)

try:
    from services.update_paper_state import manual_close_paper_position
except ImportError as e:
    print(f"Error importing module: {e}")
    print("Make sure you are running this script from the project root directory.")
    sys.exit(1)

def main():
    if len(sys.argv) < 2:
        print("Usage: python3 scripts/manual_close_paper.py <SYMBOL>")
        print("Example: python3 scripts/manual_close_paper.py BTC")
        sys.exit(1)

    symbol = sys.argv[1]
    print(f"Attempting to manually close paper position for: {symbol}...")
    manual_close_paper_position(symbol)

if __name__ == "__main__":
    main()
