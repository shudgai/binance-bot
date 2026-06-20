import os
import sys
import unittest
import tempfile
import asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from src.execution_engine import ExecutionEngine, OrderStatus

class TestExecutionEngine(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = ExecutionEngine()
        self.config = {
            "is_simulated": True,
            "fee_rate": 0.001,
            "slippage_model": 0.0005,
            "num_splits": 5,
            "step_percent": 0.01,
            "split_threshold": 10.0,
            "coin_type": "HighVolatility",
            "adaptive_threshold": 0.50
        }

    def test_generate_split_orders(self):
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
        self.assertAlmostEqual(specs[0]["price"], 10.0)
        self.assertAlmostEqual(specs[1]["price"], 9.9)

    async def test_re_fill_orders_and_adaptive_steps(self):
        orders = await self.engine.execute_order("SYN/USDT", "BUY", 200.0, 0.150000, self.config)
        remaining_qty_before = self.engine.remaining_quantity
        
        if remaining_qty_before > 0:
            refilled = await self.engine.re_fill_orders("SYN/USDT", "BUY", 0.151, self.config)
            self.assertEqual(self.engine.refill_attempts, 1)
            self.assertTrue(len(refilled) > 0)

    def test_state_persistence(self):
        temp_dir = tempfile.gettempdir()
        state_file = os.path.join(temp_dir, "test_execution_state.json")
        
        if os.path.exists(state_file):
            os.remove(state_file)
            
        self.engine.remaining_quantity = 87.5
        self.engine.refill_attempts = 3
        self.engine.initial_target_price = 1.25
        self.engine.final_avg_fill_price = 1.248
        self.engine.total_units_filled = 12.5
        
        self.engine.save_state(state_file)
        self.assertTrue(os.path.exists(state_file))
        
        new_engine = ExecutionEngine()
        new_engine.load_state(state_file)
        
        self.assertEqual(new_engine.remaining_quantity, 87.5)
        self.assertEqual(new_engine.refill_attempts, 3)
        self.assertEqual(new_engine.initial_target_price, 1.25)
        self.assertEqual(new_engine.final_avg_fill_price, 1.248)
        self.assertEqual(new_engine.total_units_filled, 12.5)
        
        if os.path.exists(state_file):
            os.remove(state_file)

if __name__ == "__main__":
    unittest.main()
