import os
import sys
import json
import argparse
import math
import copy

# Default file paths
TRADE_HISTORY_FILE = "trade_history.json"
COIN_PROFILES_PATHS = ["coin_profiles.json", "config/coin_profiles.json"]

def get_coin_profiles_path():
    """Finds the existing coin profiles file or returns the default root path."""
    for path in COIN_PROFILES_PATHS:
        if os.path.exists(path):
            return path
    return "coin_profiles.json"

def load_json_file(file_path, default_val):
    """Safely loads a JSON file."""
    if not os.path.exists(file_path):
        return default_val
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        print(f"⚠️ Error loading {file_path}: {e}")
        return default_val

def save_json_file(file_path, data):
    """
    Saves a JSON file atomically with pretty formatting.
    Writes to a temporary file first, then renames it to the target file.
    """
    temp_file = file_path + ".tmp"
    try:
        # Create parent directories if they don't exist
        parent_dir = os.path.dirname(file_path)
        if parent_dir:
            os.makedirs(parent_dir, exist_ok=True)
            
        with open(temp_file, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=4, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())  # Ensure all internal buffers are written to disk
            
        os.replace(temp_file, file_path)  # Atomic rename (POSIX compliant)
        print(f"💾 Successfully saved configuration atomically to {file_path}")
    except Exception as e:
        print(f"🚨 Failed to save configuration atomically to {file_path}: {e}")
        if os.path.exists(temp_file):
            try:
                os.remove(temp_file)
            except Exception:
                pass

def get_default_parameters(symbol):
    """Gets default parameters for a symbol from multi_coin_bot if available, otherwise standard defaults."""
    default_params = {}
    try:
        sys.path.append(os.getcwd())
        from multi_coin_bot import DEFAULT_CONFIG
        default_params = DEFAULT_CONFIG.get(symbol, {})
    except Exception:
        pass

    standard_defaults = {
        "num_splits": 5,
        "mmp": 0.005,
        "sl_atr_multiplier": 2.0,
        "tp_atr_multiplier": 4.0
    }
    return {**standard_defaults, **default_params}

def populate_default_sectors_to_config(profiles_path, current_config):
    """Populates the config file with default sectors if empty or missing."""
    updated = False
    config_copy = copy.deepcopy(current_config)
    
    # Get symbols from multi_coin_bot if possible
    from_bot_config = {}
    try:
        sys.path.append(os.getcwd())
        from multi_coin_bot import DEFAULT_CONFIG
        from_bot_config = DEFAULT_CONFIG
    except Exception:
        pass
        
    for symbol, params in from_bot_config.items():
        if symbol not in config_copy:
            config_copy[symbol] = copy.deepcopy(params)
            updated = True
            
        if "sector" not in config_copy[symbol]:
            base_symbol = symbol.replace("USDT", "")
            if base_symbol in ["SOL", "LINK", "TRX", "SUI", "INJ", "AVAX", "XRP", "ADA", "DOT", "UNI", "BTC", "ETH"]:
                config_copy[symbol]["sector"] = "Layer1_Layer2"
            elif base_symbol in ["ARB", "OP", "STRK"]:
                config_copy[symbol]["sector"] = "Layer2"
            elif base_symbol in ["RENDER", "NEAR", "FET", "TAO", "WLD"]:
                config_copy[symbol]["sector"] = "AI"
            elif base_symbol in ["DOGE", "PEPE", "1000PEPE", "SHIB", "1000BONK", "1000FLOKI", "MEME"]:
                config_copy[symbol]["sector"] = "Meme"
            elif base_symbol in ["BEAM", "BEAMX", "ESPORTS", "IMVU"]:
                config_copy[symbol]["sector"] = "Gaming"
            else:
                config_copy[symbol]["sector"] = "Speculative"
            updated = True
            
    # Also add symbols from bot_symbols.json if not present
    symbols_list = load_json_file("bot_symbols.json", {}).get("symbols", [])
    for symbol in symbols_list:
        norm_sym = symbol if symbol.endswith("USDT") else f"{symbol}USDT"
        if norm_sym not in config_copy:
            config_copy[norm_sym] = {}
            updated = True
        if "sector" not in config_copy[norm_sym]:
            base_symbol = norm_sym.replace("USDT", "")
            if base_symbol in ["SOL", "LINK", "TRX", "SUI", "INJ", "AVAX", "XRP", "ADA", "DOT", "UNI", "BTC", "ETH"]:
                config_copy[norm_sym]["sector"] = "Layer1_Layer2"
            elif base_symbol in ["ARB", "OP", "STRK"]:
                config_copy[norm_sym]["sector"] = "Layer2"
            elif base_symbol in ["RENDER", "NEAR", "FET", "TAO", "WLD"]:
                config_copy[norm_sym]["sector"] = "AI"
            elif base_symbol in ["DOGE", "PEPE", "1000PEPE", "SHIB", "1000BONK", "1000FLOKI", "MEME"]:
                config_copy[norm_sym]["sector"] = "Meme"
            elif base_symbol in ["BEAM", "BEAMX", "ESPORTS", "IMVU"]:
                config_copy[norm_sym]["sector"] = "Gaming"
            else:
                config_copy[norm_sym]["sector"] = "Speculative"
            updated = True

    if updated:
        save_json_file(profiles_path, config_copy)
        return config_copy
    return current_config

def calculate_confidence(trades_count, metric_val):
    """Calculates a confidence score (0 to 1) based on number of trades and metric consistency."""
    # Base confidence grows with number of trades, capped at 1.0
    sample_size_factor = min(1.0, trades_count / 10.0)
    # Scale based on the metric value
    return round(metric_val * sample_size_factor, 2)

def clamp_parameter_change(current_val, suggested_val, is_int=False):
    """
    Safety Valve: Clamps the applied change to +/- 10% of the current value.
    For integer values (like num_splits), we allow a minimum step of +/- 1 if a change is needed.
    """
    lower_bound = current_val * 0.90
    upper_bound = current_val * 1.10
    
    if is_int:
        clamped = max(lower_bound, min(suggested_val, upper_bound))
        rounded = int(round(clamped))
        # Ensure we can change by at least 1 if a change is suggested and not exceeding bounds
        if rounded == current_val and suggested_val != current_val:
            if suggested_val > current_val:
                rounded = min(int(suggested_val), current_val + 1)
            else:
                rounded = max(int(suggested_val), current_val - 1)
        return rounded
    else:
        return round(max(lower_bound, min(suggested_val, upper_bound)), 4)

def analyze_trades(history_path):
    """Analyzes trade history and groups metrics by symbol."""
    history = load_json_file(history_path, [])
    if not history:
        print(f"⚠️ No trade history found at {history_path}")
        return {}

    symbol_groups = {}
    for trade in history:
        symbol = trade.get("symbol")
        if not symbol:
            continue
        # Normalize symbol format
        if not symbol.endswith("USDT"):
            symbol = f"{symbol}USDT"
            
        if symbol not in symbol_groups:
            symbol_groups[symbol] = []
        symbol_groups[symbol].append(trade)

    analysis_results = {}
    for symbol, trades in symbol_groups.items():
        total_trades = len(trades)
        
        # Calculate Slippage Erosion / Friction Rate
        slippage_erosion_sum = 0.0
        slippage_valid_count = 0
        slippage_high_count = 0
        
        friction_rate_sum = 0.0
        
        # Calculate MMP Blocks
        mmp_blocks = 0
        
        # Calculate Wins
        wins = 0
        
        # Calculate Over-held trades
        over_held_count = 0

        for trade in trades:
            # Try loading friction_rate directly
            friction_rate = trade.get("friction_rate")
            if friction_rate is not None:
                friction_rate_sum += friction_rate
            else:
                # Fallback: estimate friction rate from theoretical vs real or slippage
                actual_val = trade.get("actual_entry", trade.get("entry_price", 1.0)) * trade.get("qty", 1.0)
                slippage_val = trade.get("slippage", 0.0)
                fees_val = trade.get("fees", 0.0)
                est_friction = slippage_val * trade.get("qty", 1.0) + fees_val
                friction_rate = (est_friction / actual_val) * 100 if actual_val > 0 else 0.0
                friction_rate_sum += friction_rate

            # Theoretical vs Real profits
            theoretical = trade.get("Theoretical_Profit") or trade.get("theoretical_profit")
            real = trade.get("Real_Profit") or trade.get("real_profit") or trade.get("profit_pct", 0.0)
            max_profit = trade.get("max_profit_reached") or real
            
            if theoretical is not None and theoretical != 0:
                erosion = (theoretical - real) / theoretical
                slippage_erosion_sum += erosion
                slippage_valid_count += 1
                if erosion > 0.10:
                    slippage_high_count += 1
            
            # MMP check
            exit_reason = str(trade.get("exit_reason", "")).upper()
            if "MMP" in exit_reason or "BELOW_MMP" in exit_reason:
                mmp_blocks += 1
                
            # Win check
            if real > 0:
                wins += 1
                
            # Over-held Check: if peak profit is higher than final profit by 5% or more
            if max_profit - real >= 0.05:
                over_held_count += 1

        avg_slippage_erosion = slippage_erosion_sum / slippage_valid_count if slippage_valid_count > 0 else 0.0
        avg_friction_rate = friction_rate_sum / total_trades if total_trades > 0 else 0.0
        mmp_block_rate = mmp_blocks / total_trades if total_trades > 0 else 0.0
        win_rate = wins / total_trades if total_trades > 0 else 0.0
        over_held_rate = over_held_count / total_trades if total_trades > 0 else 0.0

        analysis_results[symbol] = {
            "total_trades": total_trades,
            "slippage_erosion": avg_slippage_erosion,
            "slippage_high_ratio": slippage_high_count / slippage_valid_count if slippage_valid_count > 0 else 0.0,
            "mmp_block_rate": mmp_block_rate,
            "win_rate": win_rate,
            "over_held_rate": over_held_rate,
            "friction_rate": avg_friction_rate,
            "raw_trades": trades
        }

    return analysis_results

def generate_sector_report(analysis_results, current_config):
    """
    Groups trade analysis results by sector and outputs a sector health report.
    """
    sector_stats = {}
    
    for symbol, metrics in analysis_results.items():
        symbol_config = current_config.get(symbol, {})
        base_symbol = symbol.replace("USDT", "")
        
        # Get sector from config, otherwise fallback to default classification rules
        sector = symbol_config.get("sector")
        if not sector:
            if base_symbol in ["SOL", "LINK", "TRX", "SUI", "INJ", "AVAX", "XRP", "ADA", "DOT", "UNI", "BTC", "ETH"]:
                sector = "Layer1_Layer2"
            elif base_symbol in ["ARB", "OP", "STRK"]:
                sector = "Layer2"
            elif base_symbol in ["RENDER", "NEAR", "FET", "TAO", "WLD"]:
                sector = "AI"
            elif base_symbol in ["DOGE", "PEPE", "1000PEPE", "SHIB", "1000BONK", "1000FLOKI", "MEME"]:
                sector = "Meme"
            elif base_symbol in ["BEAM", "BEAMX", "ESPORTS", "IMVU"]:
                sector = "Gaming"
            else:
                sector = "Speculative"
                
        if sector not in sector_stats:
            sector_stats[sector] = {
                "total_trades": 0,
                "win_rate_sum": 0.0,
                "slippage_erosion_sum": 0.0,
                "over_held_sum": 0.0,
                "friction_rate_sum": 0.0,
                "symbols_count": 0
            }
            
        sector_stats[sector]["total_trades"] += metrics["total_trades"]
        sector_stats[sector]["win_rate_sum"] += metrics["win_rate"]
        sector_stats[sector]["slippage_erosion_sum"] += metrics["slippage_erosion"]
        sector_stats[sector]["over_held_sum"] += metrics["over_held_rate"]
        sector_stats[sector]["friction_rate_sum"] += metrics.get("friction_rate", 0.0)
        sector_stats[sector]["symbols_count"] += 1

    report = {}
    for sector, stats in sector_stats.items():
        avg_win_rate = stats["win_rate_sum"] / stats["symbols_count"] if stats["symbols_count"] > 0 else 0.0
        avg_slippage = stats["slippage_erosion_sum"] / stats["symbols_count"] if stats["symbols_count"] > 0 else 0.0
        avg_over_held = stats["over_held_sum"] / stats["symbols_count"] if stats["symbols_count"] > 0 else 0.0
        avg_friction = stats["friction_rate_sum"] / stats["symbols_count"] if stats["symbols_count"] > 0 else 0.0
        
        # Sector Health Recommendation Rules
        if avg_win_rate >= 0.60 and avg_friction <= 0.30:
            rec = "增加權重 (這是目前最穩健的獲利來源)"
        elif avg_win_rate >= 0.50 and avg_friction <= 0.60:
            rec = "繼續擴張 (表現良好，維持常規交易)"
        elif avg_friction >= 0.80:
            rec = "提高 MMP 門檻 (滑價與手續費損耗高，防守線拉緊)"
        elif avg_over_held > 0.30:
            rec = "調低獲利目標 (過度持倉高，防守線拉緊)"
        else:
            rec = "微調觀察 (勝率偏低或摩擦率高，建議保守交易)"
            
        report[sector] = {
            "total_trades": stats["total_trades"],
            "win_rate": avg_win_rate,
            "slippage_erosion": avg_slippage,
            "over_held_rate": avg_over_held,
            "friction_rate": avg_friction,
            "recommendation": rec
        }
    return report

def optimize_strategy(analysis_results, current_config, sector_report):
    """Applies optimization logic and safety valves to suggest and calculate parameter changes."""
    optimizations = {}

    for symbol, metrics in analysis_results.items():
        total_trades = metrics["total_trades"]
        
        # Safety Valve: Confidence Check - check if we have at least 5 trades
        if total_trades < 5:
            print(f"ℹ️ Symbol: {symbol} | Skipped optimization (Insufficient data: {total_trades}/5 trades)")
            continue

        slippage_erosion = metrics["slippage_erosion"]
        mmp_block_rate = metrics["mmp_block_rate"]
        win_rate = metrics["win_rate"]
        over_held_rate = metrics["over_held_rate"]

        # Fetch current parameter values (with default fallbacks)
        symbol_config = current_config.get(symbol, {})
        defaults = get_default_parameters(symbol)
        
        current_num_splits = symbol_config.get("num_splits") or defaults.get("num_splits", 5)
        current_mmp = symbol_config.get("mmp") or defaults.get("mmp", 0.005)
        current_sl_atr = symbol_config.get("sl_atr_multiplier") or defaults.get("sl_atr_multiplier", 2.0)
        current_tp_atr = symbol_config.get("tp_atr_multiplier") or defaults.get("tp_atr_multiplier", 4.0)

        suggestions = []
        symbol_sector = symbol_config.get("sector", "Unknown")

        # 1. High Slippage -> Increase num_splits by 20% (max 15 splits)
        if slippage_erosion > 0.10:
            suggested_num_splits = min(15, int(round(current_num_splits * 1.20)))
            if suggested_num_splits != current_num_splits:
                applied_num_splits = clamp_parameter_change(current_num_splits, suggested_num_splits, is_int=True)
                confidence = calculate_confidence(total_trades, metrics["slippage_high_ratio"])
                suggestions.append({
                    "parameter": "num_splits",
                    "issue": "High Slippage",
                    "current": current_num_splits,
                    "suggested": suggested_num_splits,
                    "applied": applied_num_splits,
                    "confidence": confidence,
                    "reasoning": f"Increasing num_splits for {symbol} because Slippage_Erosion reached {slippage_erosion:.1%} over the last {total_trades} trades (Sector: {symbol_sector})"
                })

        # --- 新增：基於「摩擦力佔比 (Friction Rate)」的動態 MMP 調整公式 (第一階段) ---
        friction_rate = metrics.get("friction_rate", 0.0)
        # 若摩擦力大於 0.8% (高摩擦警報)：自動將 MMP 提高 15%，以過濾掉可能被磨損的手續費微利單
        if friction_rate >= 0.8:
            suggested_mmp = current_mmp * 1.15
            applied_mmp = clamp_parameter_change(current_mmp, suggested_mmp, is_int=False)
            confidence = calculate_confidence(total_trades, min(1.0, friction_rate / 2.0))
            suggestions.append({
                "parameter": "mmp",
                "issue": "High Friction Cost Warning",
                "current": current_mmp,
                "suggested": suggested_mmp,
                "applied": applied_mmp,
                "confidence": confidence,
                "reasoning": f"Increasing mmp for {symbol} by 15% because average friction rate is high ({friction_rate:.2f}%) over the last {total_trades} trades. (Blocking high friction micro trades)"
            })
        # 若摩擦力小於 0.3% (極佳流動性)：代表磨損很小，可以調低 MMP 10% 以捕捉更多微小獲利機會
        elif friction_rate < 0.3 and win_rate >= 0.50:
            suggested_mmp = current_mmp * 0.90
            applied_mmp = clamp_parameter_change(current_mmp, suggested_mmp, is_int=False)
            confidence = calculate_confidence(total_trades, win_rate)
            suggestions.append({
                "parameter": "mmp",
                "issue": "Excellent Liquidity / Low Friction",
                "current": current_mmp,
                "suggested": suggested_mmp,
                "applied": applied_mmp,
                "confidence": confidence,
                "reasoning": f"Decreasing mmp for {symbol} by 10% because average friction rate is very low ({friction_rate:.2f}%) and win rate is stable ({win_rate:.1%}) over the last {total_trades} trades."
            })

        # 2. High MMP Block Rate -> Decrease mmp by 10%
        if mmp_block_rate > 0.70:
            suggested_mmp = current_mmp * 0.90
            applied_mmp = clamp_parameter_change(current_mmp, suggested_mmp, is_int=False)
            confidence = calculate_confidence(total_trades, mmp_block_rate)
            suggestions.append({
                "parameter": "mmp",
                "issue": "High MMP Block Rate",
                "current": current_mmp,
                "suggested": suggested_mmp,
                "applied": applied_mmp,
                "confidence": confidence,
                "reasoning": f"Decreasing mmp for {symbol} because MMP_Block_Rate reached {mmp_block_rate:.1%} over the last {total_trades} trades (Sector: {symbol_sector})"
            })

        # 3. Low Win Rate & Low Slippage -> Increase sl_atr_multiplier by 10%
        if win_rate < 0.40 and slippage_erosion <= 0.10:
            suggested_sl_atr = current_sl_atr * 1.10
            applied_sl_atr = clamp_parameter_change(current_sl_atr, suggested_sl_atr, is_int=False)
            confidence = calculate_confidence(total_trades, 1.0 - win_rate)
            suggestions.append({
                "parameter": "sl_atr_multiplier",
                "issue": "Low Win Rate",
                "current": current_sl_atr,
                "suggested": suggested_sl_atr,
                "applied": applied_sl_atr,
                "confidence": confidence,
                "reasoning": f"Increasing sl_atr_multiplier for {symbol} because Win_Rate was {win_rate:.1%} and Slippage_Erosion was low ({slippage_erosion:.1%}) over the last {total_trades} trades (Sector: {symbol_sector})"
            })

        # 4. Over-held trades -> Decrease profit target (tp_atr_multiplier) by 10%
        # Additionally adjust trailing stop parameters (tighten them to lock profit quicker)
        if over_held_rate > 0.30:
            suggested_tp_atr = current_tp_atr * 0.90
            applied_tp_atr = clamp_parameter_change(current_tp_atr, suggested_tp_atr, is_int=False)
            confidence = calculate_confidence(total_trades, over_held_rate)
            suggestions.append({
                "parameter": "tp_atr_multiplier",
                "issue": "Over-held Position",
                "current": current_tp_atr,
                "suggested": suggested_tp_atr,
                "applied": applied_tp_atr,
                "confidence": confidence,
                "reasoning": f"Decreasing tp_atr_multiplier for {symbol} because Over_Held_Rate reached {over_held_rate:.1%} due to profit retracements exceeding 5% (Sector: {symbol_sector})"
            })
            
            # Tighten trailing activation by 15% (e.g. 5% -> 4.25%) to trigger trailing sooner
            current_trail_act = symbol_config.get("trailing_activation", defaults.get("trailing_activation", 0.03))
            suggested_trail_act = max(0.01, current_trail_act * 0.85)
            applied_trail_act = clamp_parameter_change(current_trail_act, suggested_trail_act, is_int=False)
            suggestions.append({
                "parameter": "trailing_activation",
                "issue": "Over-held Position (Trailing Activation)",
                "current": current_trail_act,
                "suggested": suggested_trail_act,
                "applied": applied_trail_act,
                "confidence": confidence,
                "reasoning": f"Tightening trailing_activation for {symbol} to trigger earlier due to high over-held rate {over_held_rate:.1%}"
            })
            
            # Tighten trailing distance by 10% (e.g. 1.2 ATR -> 1.08 ATR) to exit quicker on pullback
            current_trail_dist = symbol_config.get("trailing_distance_atr", defaults.get("trailing_distance_atr", 1.2))
            suggested_trail_dist = max(0.5, current_trail_dist * 0.90)
            applied_trail_dist = clamp_parameter_change(current_trail_dist, suggested_trail_dist, is_int=False)
            suggestions.append({
                "parameter": "trailing_distance_atr",
                "issue": "Over-held Position (Trailing Distance)",
                "current": current_trail_dist,
                "suggested": suggested_trail_dist,
                "applied": applied_trail_dist,
                "confidence": confidence,
                "reasoning": f"Reducing trailing_distance_atr for {symbol} to secure profits quicker due to high over-held rate {over_held_rate:.1%}"
            })

        # Sector level safety override: if the sector has high slippage, automatically advise adjusting MMP threshold
        sector_metrics = sector_report.get(symbol_sector, {})
        if sector_metrics.get("slippage_erosion", 0.0) > 0.08 and mmp_block_rate <= 0.70:
            # Recommend raising MMP slightly to filter out micro trades in high-friction sectors
            suggested_mmp = current_mmp * 1.10
            applied_mmp = clamp_parameter_change(current_mmp, suggested_mmp, is_int=False)
            confidence = calculate_confidence(total_trades, sector_metrics["slippage_erosion"])
            if not any(s["parameter"] == "mmp" for s in suggestions):
                suggestions.append({
                    "parameter": "mmp",
                    "issue": "High Sector Slippage Friction",
                    "current": current_mmp,
                    "suggested": suggested_mmp,
                    "applied": applied_mmp,
                    "confidence": confidence,
                    "reasoning": f"Increasing mmp for {symbol} because average slippage for Sector {symbol_sector} reached {sector_metrics['slippage_erosion']:.1%}"
                })

        if suggestions:
            optimizations[symbol] = suggestions

    return optimizations

def main():
    parser = argparse.ArgumentParser(description="AI Strategy Optimizer - Self-Evolving Feedback Loop")
    parser.add_argument("--dry-run", type=str, default="True", help="Dry run mode (True/False). Default is True.")
    parser.add_argument("--history", type=str, default=TRADE_HISTORY_FILE, help="Path to trade history file.")
    parser.add_argument("--profiles", type=str, default=None, help="Path to coin profiles config file.")
    args = parser.parse_args()

    # Determine dry_run value
    dry_run = args.dry_run.lower() in ("true", "1", "yes")

    # Find profiles path
    profiles_path = args.profiles if args.profiles else get_coin_profiles_path()

    print("=" * 60)
    print(f"🚀 AI Strategy Optimizer starting (Dry Run: {dry_run})")
    print(f"  Trade History Path: {args.history}")
    print(f"  Coin Profiles Path: {profiles_path}")
    print("=" * 60)

    # 1. Load data
    analysis_results = analyze_trades(args.history)
    if not analysis_results:
        print("❌ No trades to analyze. Exiting.")
        return

    raw_config = load_json_file(profiles_path, {})
    # Initialize config file with default sector mappings if missing
    current_config = populate_default_sectors_to_config(profiles_path, raw_config)

    # 2. Sector Health Diagnostics (賽道戰報)
    sector_report = generate_sector_report(analysis_results, current_config)
    print("\n📊 Sector Health Report (賽道戰報):")
    print("-" * 115)
    print(f"{'Sector (賽道)':<15} | {'Trades':<6} | {'Avg Win Rate':<12} | {'Avg Friction':<12} | {'Avg Over-held':<13} | {'Action Recommendation':<30}")
    print("-" * 115)
    for sector, metrics in sector_report.items():
        print(f"{sector:<15} | {metrics['total_trades']:<6} | {metrics['win_rate']:<12.2%} | {metrics['friction_rate']:<12.2%} | {metrics['over_held_rate']:<13.2%} | {metrics['recommendation']}")
    print("-" * 115)

    # 3. Run optimization logic
    optimizations = optimize_strategy(analysis_results, current_config, sector_report)

    if not optimizations:
        print("\n💡 No strategy optimization recommendations generated.")
        return

    # 4. Print summaries and apply updates
    updated_config = dict(current_config)
    changes_made = False

    print("\n🔍 Optimization Recommendations:")
    for symbol, suggestions in optimizations.items():
        if symbol not in updated_config:
            updated_config[symbol] = {}

        for sug in suggestions:
            action_desc = f"Change {sug['parameter']} from {sug['current']} to {sug['applied']}"
            if sug['suggested'] != sug['applied']:
                action_desc += f" (clamped from {sug['suggested']:.4f})"
                
            confidence_pct = int(sug['confidence'] * 100)
            print(f"Symbol: {symbol} | Issue: {sug['issue']} | Action: {action_desc} | Confidence: {confidence_pct}% | Reasoning: {sug['reasoning']}")

            # Apply change to local config copy
            updated_config[symbol][sug['parameter']] = sug['applied']
            changes_made = True

    # 5. Save updates if not in dry_run mode
    if not dry_run and changes_made:
        print("\nSaving updated parameters...")
        save_json_file(profiles_path, updated_config)
    elif dry_run:
        print("\nℹ️ Dry run mode active: No changes were written to the coin profiles file.")

if __name__ == "__main__":
    main()
