import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from src.execution_engine import ExecutionEngine, OrderStatus

class TestExecutionEngine(unittest.TestCase):
    def setUp(self):
        self.engine = ExecutionEngine()
        self.config = {
            "is_simulated": True,
            "fee_rate": 0.001,
            "slippage_model": 0.0005,
            "num_splits": 5,
            "step_percent": 0.001,
            "split_threshold": 100.0,
            "coin_type": "Normal"
        }

    def test_generate_split_orders(self):
        # Verify BUY splits pricing ladder
        specs = self.engine.generate_split_orders(
            total_quantity=500.0,
            target_price=10.0,
            side="BUY",
            num_splits=5,
            step_percent=0.01
        )
        self.assertEqual(len(specs), 5)
        for spec in specs:
            self.assertAlmostEqual(spec["qty"], 100.0)
        
        # Verify prices descend: 10.0, 9.9, 9.8, 9.7, 9.6
        self.assertAlmostEqual(specs[0]["price"], 10.0)
        self.assertAlmostEqual(specs[1]["price"], 9.9)
        self.assertAlmostEqual(specs[2]["price"], 9.8)
        self.assertAlmostEqual(specs[3]["price"], 9.7)
        self.assertAlmostEqual(specs[4]["price"], 9.6)

    def test_split_triggers(self):
        # 1. Below threshold, Normal volatility -> No split
        orders = self.engine.execute_order("BTC/USDT", "BUY", 50.0, 60000.0, self.config)
        self.assertEqual(len(orders), 1)

        # 2. Below threshold, High Volatility -> Should split
        high_vol_config = self.config.copy()
        high_vol_config["coin_type"] = "HighVolatility"
        orders_split_vol = self.engine.execute_order("BTC/USDT", "BUY", 50.0, 60000.0, high_vol_config)
        self.assertEqual(len(orders_split_vol), 5)

        # 3. Above threshold, Normal volatility -> Should split
        orders_split_thresh = self.engine.execute_order("BTC/USDT", "BUY", 150.0, 60000.0, self.config)
        self.assertEqual(len(orders_split_thresh), 5)

    def test_partial_fill_and_recalculation(self):
        # Execute an order that will split
        high_vol_config = self.config.copy()
        high_vol_config["coin_type"] = "HighVolatility"
        
        orders = self.engine.execute_order("SYN/USDT", "BUY", 200.0, 0.15, high_vol_config)
        
        # Check if any orders ended up in the pending/unfilled queue
        total_qty = 200.0
        total_filled = sum(o.filled_quantity for o in orders)
        
        remaining_qty = total_qty - total_filled
        print(f"\n[Partial Fill Test] Total Qty: {total_qty} | Total Filled: {total_filled:.4f} | Remaining: {remaining_qty:.4f}")
        
        if remaining_qty > 0.001:
            # Recalculate remaining splits based on current market price (e.g. price moved to 0.152)
            recalculated_orders = self.engine.recalculate_remaining_splits(
                symbol="SYN/USDT",
                side="BUY",
                remaining_quantity=remaining_qty,
                current_market_price=0.152,
                config=high_vol_config
            )
            # Should have split the remaining qty into another 5 orders
            self.assertEqual(len(recalculated_orders), 5)
            total_recalc_qty = sum(o.original_quantity for o in recalculated_orders)
            self.assertAlmostEqual(total_recalc_qty, remaining_qty)

if __name__ == "__main__":
    unittest.main()
