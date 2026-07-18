"""
路由勝率統計腳本
====================================
讀取 data/trade_history.json，依 entry_reason（路由）分組統計：
  - 交易筆數
  - 勝率
  - 平均獲利 / 虧損
  - 總損益（以 profit_pct 加總估算，非美元金額）
  - 異常標記（ai_anomaly_tags）出現頻率，方便看出哪條路由容易高滑點/高摩擦力/方向錯誤

使用方式：
    python3 route_stats.py
    python3 route_stats.py --file /path/to/trade_history.json
    python3 route_stats.py --since 2026-07-16          # 只統計某日期之後的交易
"""

import json
import argparse
from collections import defaultdict
from datetime import datetime


def load_trades(filepath: str) -> list:
    with open(filepath, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise ValueError(f"預期 JSON 為陣列格式，但讀到 {type(data)}")
    return data


def filter_since(trades: list, since: str) -> list:
    if not since:
        return trades
    cutoff = datetime.strptime(since, "%Y-%m-%d")
    filtered = []
    for t in trades:
        ts = t.get("timestamp")
        if not ts:
            continue
        try:
            trade_dt = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
        if trade_dt >= cutoff:
            filtered.append(t)
    return filtered


def compute_route_stats(trades: list) -> dict:
    """
    依 entry_reason 分組，回傳每條路由的統計摘要。
    """
    grouped = defaultdict(list)
    for t in trades:
        route = t.get("entry_reason", "UNKNOWN")
        grouped[route].append(t)

    stats = {}
    for route, route_trades in grouped.items():
        profits = [t.get("profit_pct", 0.0) for t in route_trades]
        wins = [p for p in profits if p > 0]
        losses = [p for p in profits if p <= 0]

        # 統計每條路由觸發的異常標記次數
        anomaly_counter = defaultdict(int)
        for t in route_trades:
            for tag in t.get("ai_anomaly_tags", []) or []:
                anomaly_counter[tag] += 1

        friction_rates = [t.get("friction_rate", 0.0) for t in route_trades if t.get("friction_rate") is not None]

        stats[route] = {
            "total_trades": len(route_trades),
            "win_count": len(wins),
            "loss_count": len(losses),
            "win_rate_pct": round(len(wins) / len(route_trades) * 100, 2) if route_trades else 0.0,
            "avg_profit_pct": round(sum(profits) / len(profits) * 100, 4) if profits else 0.0,
            "avg_win_pct": round(sum(wins) / len(wins) * 100, 4) if wins else 0.0,
            "avg_loss_pct": round(sum(losses) / len(losses) * 100, 4) if losses else 0.0,
            "total_profit_pct_sum": round(sum(profits) * 100, 4),
            "avg_friction_rate_pct": round(sum(friction_rates) / len(friction_rates), 4) if friction_rates else 0.0,
            "anomaly_tags": dict(anomaly_counter),
        }

    return stats


def print_report(stats: dict):
    if not stats:
        print("沒有符合條件的交易紀錄。")
        return

    # 按交易筆數由多到少排序，筆數多的路由排前面（統計上比較有代表性）
    sorted_routes = sorted(stats.items(), key=lambda kv: kv[1]["total_trades"], reverse=True)

    print(f"{'路由':<22} {'筆數':>6} {'勝率':>8} {'平均損益%':>10} {'平均獲利%':>10} {'平均虧損%':>10} {'平均摩擦%':>10}")
    print("-" * 90)
    for route, s in sorted_routes:
        print(
            f"{route:<22} {s['total_trades']:>6} {s['win_rate_pct']:>7.2f}% "
            f"{s['avg_profit_pct']:>9.4f}% {s['avg_win_pct']:>9.4f}% "
            f"{s['avg_loss_pct']:>9.4f}% {s['avg_friction_rate_pct']:>9.4f}%"
        )

    print("\n=== 異常標記分佈（依路由） ===")
    for route, s in sorted_routes:
        if s["anomaly_tags"]:
            tags_str = ", ".join(f"{k}={v}" for k, v in s["anomaly_tags"].items())
            print(f"{route:<22} {tags_str}")

    total_trades = sum(s["total_trades"] for s in stats.values())
    total_wins = sum(s["win_count"] for s in stats.values())
    print(f"\n=== 整體 ===")
    print(f"總交易筆數: {total_trades}")
    print(f"整體勝率: {round(total_wins / total_trades * 100, 2) if total_trades else 0.0}%")

    # 提醒筆數過少的路由，統計上不夠可靠
    low_sample_routes = [route for route, s in stats.items() if s["total_trades"] < 10]
    if low_sample_routes:
        print(f"\n⚠️ 以下路由樣本數 <10 筆，勝率數字僅供參考，不建議用來下結論：{', '.join(low_sample_routes)}")


def main():
    parser = argparse.ArgumentParser(description="從 trade_history.json 統計各路由勝率")
    parser.add_argument("--file", default="data/trade_history.json", help="trade_history.json 路徑")
    parser.add_argument("--since", default=None, help="只統計此日期之後的交易，格式 YYYY-MM-DD")
    args = parser.parse_args()

    trades = load_trades(args.file)
    if args.since:
        trades = filter_since(trades, args.since)

    stats = compute_route_stats(trades)
    print_report(stats)


if __name__ == "__main__":
    main()
