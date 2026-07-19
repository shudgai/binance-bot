"""
分析 trade_history.json 裡「利潤回吐」(peak giveback) 的情況：
比較 max_profit_reached 跟最終 profit_pct 的差距，
找出哪些出場原因(exit_reason)、哪些路由(entry_reason)最常發生
「明明有賺、卻沒守住」的狀況。

使用方式：
    python3 peak_giveback_stats.py
    python3 peak_giveback_stats.py --file data/trade_history.json --min-peak 0.003
"""
import json
import argparse
from collections import defaultdict


def load_trades(filepath):
    with open(filepath, "r", encoding="utf-8") as f:
        return json.load(f)


def analyze_giveback(trades, min_peak_pct=0.003):
    """
    只看曾經達到過一定獲利峰值(預設0.3%)的交易，
    計算回吐幅度 = max_profit_reached - profit_pct
    """
    records = []
    for t in trades:
        peak = float(t.get("max_profit_reached", 0.0) or 0.0)
        final = float(t.get("profit_pct", 0.0) or 0.0)
        if peak < min_peak_pct:
            continue  # 沒有明顯獲利過，不算回吐問題
        giveback = peak - final
        records.append({
            "symbol": t.get("symbol"),
            "entry_reason": t.get("entry_reason", "UNKNOWN"),
            "exit_reason": t.get("exit_reason", "UNKNOWN"),
            "peak_pct": peak,
            "final_pct": final,
            "giveback_pct": giveback,
            "ended_in_loss": final < 0,
            "gave_back_all_profit": final <= 0 and peak > 0,
        })
    return records


def print_report(records):
    if not records:
        print("沒有找到任何曾經有明顯獲利峰值的交易紀錄。")
        return

    total = len(records)
    gave_back_to_loss = sum(1 for r in records if r["gave_back_all_profit"])
    avg_peak = sum(r["peak_pct"] for r in records) / total
    avg_final = sum(r["final_pct"] for r in records) / total
    avg_giveback = sum(r["giveback_pct"] for r in records) / total

    print(f"=== 整體統計（曾達到獲利峰值門檻的交易，共 {total} 筆）===")
    print(f"平均峰值獲利: {avg_peak*100:.3f}%")
    print(f"平均最終獲利: {avg_final*100:.3f}%")
    print(f"平均回吐幅度: {avg_giveback*100:.3f}%")
    print(f"曾經獲利、最終卻虧損出場的筆數: {gave_back_to_loss} / {total} ({gave_back_to_loss/total*100:.1f}%)")

    print("\n=== 依 exit_reason 分組（哪種出場方式最常回吐）===")
    by_exit = defaultdict(list)
    for r in records:
        by_exit[r["exit_reason"]].append(r)
    for reason, rs in sorted(by_exit.items(), key=lambda kv: -len(kv[1])):
        n = len(rs)
        avg_gb = sum(r["giveback_pct"] for r in rs) / n
        loss_count = sum(1 for r in rs if r["gave_back_all_profit"])
        print(f"{reason:<35} 筆數={n:>4}  平均回吐={avg_gb*100:>6.3f}%  轉虧筆數={loss_count}")

    print("\n=== 依 entry_reason 分組（哪條路由最常回吐）===")
    by_entry = defaultdict(list)
    for r in records:
        by_entry[r["entry_reason"]].append(r)
    for reason, rs in sorted(by_entry.items(), key=lambda kv: -len(kv[1])):
        n = len(rs)
        avg_gb = sum(r["giveback_pct"] for r in rs) / n
        loss_count = sum(1 for r in rs if r["gave_back_all_profit"])
        print(f"{reason:<20} 筆數={n:>4}  平均回吐={avg_gb*100:>6.3f}%  轉虧筆數={loss_count}")

    print("\n=== 回吐幅度最大的前 10 筆交易 ===")
    worst = sorted(records, key=lambda r: -r["giveback_pct"])[:10]
    for r in worst:
        print(f"{r['symbol']:<12} {r['entry_reason']:<15} {r['exit_reason']:<25} "
              f"峰值={r['peak_pct']*100:>6.3f}% 最終={r['final_pct']*100:>6.3f}% "
              f"回吐={r['giveback_pct']*100:>6.3f}%")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--file", default="data/trade_history.json")
    parser.add_argument("--min-peak", type=float, default=0.003,
                         help="最低獲利峰值門檻(小數)，預設0.003=0.3%%")
    args = parser.parse_args()

    trades = load_trades(args.file)
    records = analyze_giveback(trades, min_peak_pct=args.min_peak)
    print_report(records)


if __name__ == "__main__":
    main()
