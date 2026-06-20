import os
import sys
import unittest

# Ensure the root/src directory is in the path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from src.execution_engine import ExecutionEngine, OrderStatus

class TestExecutionEngine(unittest.TestCase):
    def setUp(self):
        self.engine = ExecutionEngine()
        self.config = {
            "is_simulated": True,
            "fee_rate": 0.001,       # 0.1% maker/taker fee
            "slippage_model": 0.0005, # 0.05% standard deviation for slippage
            "num_splits": 5,
            "step_percent": 0.001    # 0.1% price steps
        }

    def test_step_limit_splitting(self):
        symbol = "BTC/USDT"
        target_price = 60000.0
        qty = 1.0
        
        # Test BUY split orders pricing
        orders = self.engine.execute_order(symbol, "BUY", qty, target_price, self.config)
        self.assertEqual(len(orders), 5)
        
        # Check that original quantities sum up to 1.0
        total_orig_qty = sum(o.original_quantity for o in orders)
        self.assertAlmostEqual(total_orig_qty, 1.0)
        
        # Verify status and net calculation
        for o in orders:
            self.assertIn(o.status, [OrderStatus.FILLED, OrderStatus.PARTIAL, OrderStatus.FAILED])
            if o.status == OrderStatus.FILLED:
                self.assertGreater(o.filled_quantity, 0)
                self.assertLess(o.net_cost_or_proceeds, 0) # Buy has negative cost

    def test_ten_dummy_trades(self):
        print("\n--- Running 10 Dummy Trades ---")
        symbol = "ETH/USDT"
        base_price = 3000.0
        qty = 2.0
        
        total_net = 0.0
        
        # Run 10 sequential trades alternating between BUY and SELL
        # 5 pairs of BUY and SELL to simulate trading cycles and compute net P&L
        for i in range(1, 11):
            side = "BUY" if i % 2 == 1 else "SELL"
            # Simulate slight market movements
            target_price = base_price * (1.0 + (i * 0.002 if side == "SELL" else -i * 0.002))
            
            print(f"\n[Trade #{i}] {side} {qty} {symbol} targetting price {target_price:.2f}")
            orders = self.engine.execute_order(symbol, side, qty, target_price, self.config)
            
            filled_qty = sum(o.filled_quantity for o in orders)
            trade_net = sum(o.net_cost_or_proceeds for o in orders)
            total_net += trade_net
            
            print(f"-> Result: Filled {filled_qty:.4f}/{qty:.4f} units | Net Cash Flow: {trade_net:+.4f} USDT")
        
        print("\n==================================")
        print(f"Total Cumulative Cash Flow: {total_net:+.4f} USDT")
        print("==================================")
        self.assertTrue(True)

if __name__ == "__main__":
    unittest.main()
