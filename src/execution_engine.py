import asyncio
import threading
import random
import uuid
import logging
import json
import os
from enum import Enum
from dataclasses import dataclass, asdict
from typing import Dict, Any, List

# Try importing ccxt for real exchange exceptions
try:
    import ccxt
except ImportError:
    ccxt = None

# Configure logger
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("ExecutionEngine")

class OrderStatus(Enum):
    PENDING = "PENDING"
    PARTIAL = "PARTIAL"
    FILLED = "FILLED"
    FAILED = "FAILED"

@dataclass
class Order:
    order_id: str
    symbol: str
    side: str
    status: OrderStatus
    original_quantity: float
    filled_quantity: float
    avg_price: float
    net_cost_or_proceeds: float = 0.0  # Positive for proceeds (sell), negative for cost (buy)

class ExecutionEngine:
    def __init__(self, exchange=None) -> None:
        self.exchange = exchange
        self.pending_orders: List[Order] = []
        self.remaining_quantity: float = 0.0
        self.refill_attempts: int = 0
        self.initial_target_price: float = 0.0
        self.final_avg_fill_price: float = 0.0
        self.total_units_filled: float = 0.0
        
        # Concurrency Locks
        self.lock = asyncio.Lock()
        self.sync_lock = threading.Lock()

    def generate_split_orders(
        self,
        total_quantity: float,
        target_price: float,
        side: str,
        num_splits: int,
        step_percent: float
    ) -> List[Dict[str, float]]:
        """
        Generates N split orders with step-limit prices.
        """
        orders_spec = []
        qty_per_split = total_quantity / num_splits
        for i in range(num_splits):
            if side.upper() == "BUY":
                price = target_price * (1.0 - step_percent * i)
            else:
                price = target_price * (1.0 + step_percent * i)
            orders_spec.append({"price": price, "qty": qty_per_split, "index": i})
        return orders_spec

    async def execute_order(
        self,
        symbol: str,
        side: str,
        total_quantity: float,
        target_price: float,
        config: Dict[str, Any]
    ) -> List[Order]:
        """
        Thread-safe entry point to execute an order.
        """
        async with self.lock:
            return await self._execute_order_unlocked(symbol, side, total_quantity, target_price, config)

    async def _execute_order_unlocked(
        self,
        symbol: str,
        side: str,
        total_quantity: float,
        target_price: float,
        config: Dict[str, Any]
    ) -> List[Order]:
        """
        Internal order execution logic (unlocked to prevent deadlocks).
        """
        is_simulated = config.get("is_simulated", True)
        split_threshold = config.get("split_threshold", 100.0)
        coin_type = config.get("coin_type", "Normal")
        num_splits = config.get("num_splits", 5)
        step_percent = config.get("step_percent", 0.001)
        fee_rate = config.get("fee_rate", 0.001)
        slippage_model = config.get("slippage_model", 0.0005)

        with self.sync_lock:
            if self.refill_attempts == 0:
                self.initial_target_price = target_price
                self.total_units_filled = 0.0
                self.remaining_quantity = total_quantity

        should_split = (self.remaining_quantity > split_threshold) or (coin_type == "HighVolatility")

        logger.info(
            f"🔄 execute_order: {symbol} | {side} | Remaining Qty: {self.remaining_quantity:.4f} | Target: {target_price:.6f} "
            f"| Volatility: {coin_type} | Should Split: {should_split} | Step Pct: {step_percent:.4f}"
        )

        executed_orders: List[Order] = []

        if should_split:
            specs = self.generate_split_orders(self.remaining_quantity, target_price, side, num_splits, step_percent)
            for spec in specs:
                order_id = str(uuid.uuid4())
                price = spec["price"]
                qty = spec["qty"]
                idx = spec["index"]

                fill_probability = max(0.1, 0.95 - idx * 0.15)

                if is_simulated:
                    order = await self._place_simulated_order(
                        order_id, symbol, side, qty, price, fee_rate, slippage_model, fill_probability
                    )
                else:
                    order = await self._place_real_order(order_id, symbol, side, qty, price)

                executed_orders.append(order)
                with self.sync_lock:
                    if order.status != OrderStatus.FILLED:
                        self.pending_orders.append(order)
        else:
            order_id = str(uuid.uuid4())
            if is_simulated:
                order = await self._place_simulated_order(
                    order_id, symbol, side, self.remaining_quantity, target_price, fee_rate, slippage_model, 0.95
                )
            else:
                order = await self._place_real_order(order_id, symbol, side, self.remaining_quantity, target_price)
            executed_orders.append(order)
            with self.sync_lock:
                if order.status != OrderStatus.FILLED:
                    self.pending_orders.append(order)

        # Update filled totals atomically
        with self.sync_lock:
            step_filled = sum(o.filled_quantity for o in executed_orders)
            if step_filled > 0:
                step_avg_price = sum(o.avg_price * o.filled_quantity for o in executed_orders) / step_filled
                self.final_avg_fill_price = (
                    (self.final_avg_fill_price * self.total_units_filled + step_avg_price * step_filled) /
                    (self.total_units_filled + step_filled)
                )
            self.total_units_filled += step_filled
            self.remaining_quantity = max(0.0, self.remaining_quantity - step_filled)

            self._report_execution()

        return executed_orders

    async def re_fill_orders(
        self,
        symbol: str,
        side: str,
        current_market_price: float,
        config: Dict[str, Any]
    ) -> List[Order]:
        """
        Thread-safe entry point to re-fill remaining order quantities.
        """
        async with self.lock:
            if self.remaining_quantity <= 0.0001:
                logger.info("✅ All units are already filled. No re-fill needed.")
                return []

            with self.sync_lock:
                self.refill_attempts += 1
                logger.info(f"🔄 Re-fill Attempt #{self.refill_attempts} for {symbol} | Remaining Qty: {self.remaining_quantity:.4f}")

                total_requested_before = self.remaining_quantity + self.total_units_filled
                fill_rate = self.total_units_filled / total_requested_before if total_requested_before > 0 else 1.0

            adaptive_threshold = config.get("adaptive_threshold", 0.20)
            step_percent = config.get("step_percent", 0.001)

            if fill_rate < adaptive_threshold:
                new_step_percent = step_percent * 0.9
                logger.warning(
                    f"⚠️ Fill rate {fill_rate*100:.1f}% is below threshold {adaptive_threshold*100:.1f}%. "
                    f"Reducing step_percent from {step_percent:.5f} to {new_step_percent:.5f} (10% reduction)."
                )
                config = config.copy()
                config["step_percent"] = new_step_percent

            return await self._execute_order_unlocked(symbol, side, self.remaining_quantity, current_market_price, config)

    def save_state(self, filepath: str) -> None:
        """
        Persists the current state of execution to a JSON file.
        """
        with self.sync_lock:
            state_data = {
                "remaining_quantity": self.remaining_quantity,
                "refill_attempts": self.refill_attempts,
                "initial_target_price": self.initial_target_price,
                "final_avg_fill_price": self.final_avg_fill_price,
                "total_units_filled": self.total_units_filled,
                "pending_orders": [
                    {
                        "order_id": o.order_id,
                        "symbol": o.symbol,
                        "side": o.side,
                        "status": o.status.value,
                        "original_quantity": o.original_quantity,
                        "filled_quantity": o.filled_quantity,
                        "avg_price": o.avg_price,
                        "net_cost_or_proceeds": o.net_cost_or_proceeds
                    } for o in self.pending_orders
                ]
            }
        
        # Write atomically
        temp_file = filepath + ".tmp"
        try:
            with open(temp_file, "w") as f:
                json.dump(state_data, f, indent=4)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temp_file, filepath)
            logger.info(f"💾 Execution state saved atomically to {filepath}")
        except Exception as e:
            logger.error(f"🚨 Failed to save state to {filepath}: {e}")
            if os.path.exists(temp_file):
                os.remove(temp_file)

    def load_state(self, filepath: str) -> None:
        """
        Restores execution state from a JSON file.
        """
        if not os.path.exists(filepath):
            logger.warning(f"⚠️ State file {filepath} not found. Starting fresh.")
            return

        with self.sync_lock:
            try:
                with open(filepath, "r") as f:
                    state_data = json.load(f)

                self.remaining_quantity = state_data.get("remaining_quantity", 0.0)
                self.refill_attempts = state_data.get("refill_attempts", 0)
                self.initial_target_price = state_data.get("initial_target_price", 0.0)
                self.final_avg_fill_price = state_data.get("final_avg_fill_price", 0.0)
                self.total_units_filled = state_data.get("total_units_filled", 0.0)
                
                self.pending_orders = []
                for o_data in state_data.get("pending_orders", []):
                    order = Order(
                        order_id=o_data["order_id"],
                        symbol=o_data["symbol"],
                        side=o_data["side"],
                        status=OrderStatus(o_data["status"]),
                        original_quantity=o_data["original_quantity"],
                        filled_quantity=o_data["filled_quantity"],
                        avg_price=o_data["avg_price"],
                        net_cost_or_proceeds=o_data["net_cost_or_proceeds"]
                    )
                    self.pending_orders.append(order)
                    
                logger.info(f"📂 Execution state loaded from {filepath} | Remaining Qty: {self.remaining_quantity:.4f}")
            except Exception as e:
                logger.error(f"🚨 Error loading state from {filepath}: {e}")

    def _report_execution(self) -> None:
        logger.info("==============================================")
        logger.info(f"📋 Execution Report:")
        logger.info(f"   - Initial Target Price   : {self.initial_target_price:.6f}")
        logger.info(f"   - Final Average Fill Price: {self.final_avg_fill_price:.6f}")
        logger.info(f"   - Total Units Filled     : {self.total_units_filled:.4f}")
        logger.info(f"   - Remaining Quantity     : {self.remaining_quantity:.4f}")
        logger.info(f"   - Re-fill Attempts       : {self.refill_attempts}")
        logger.info("==============================================")

    async def _place_simulated_order(
        self,
        order_id: str,
        symbol: str,
        side: str,
        qty: float,
        price: float,
        fee_rate: float,
        slippage_model: float,
        fill_probability: float
    ) -> Order:
        delay = random.uniform(0.1, 0.5)
        await asyncio.sleep(delay)

        roll = random.random()

        if roll > fill_probability:
            status = OrderStatus.FAILED
            filled_qty = 0.0
            avg_price = 0.0
            net = 0.0
        elif roll > fill_probability - 0.1:
            status = OrderStatus.PARTIAL
            filled_qty = qty * random.uniform(0.2, 0.8)
            slippage = random.normalvariate(0, slippage_model)
            avg_price = price * (1.0 + slippage)
            raw_val = filled_qty * avg_price
            fee = raw_val * fee_rate
            net = -(raw_val + fee) if side.upper() == "BUY" else (raw_val - fee)
        else:
            status = OrderStatus.FILLED
            filled_qty = qty
            slippage = random.normalvariate(0, slippage_model)
            avg_price = price * (1.0 + slippage)
            raw_val = filled_qty * avg_price
            fee = raw_val * fee_rate
            net = -(raw_val + fee) if side.upper() == "BUY" else (raw_val - fee)

        return Order(
            order_id=order_id,
            symbol=symbol,
            side=side.upper(),
            status=status,
            original_quantity=qty,
            filled_quantity=filled_qty,
            avg_price=avg_price,
            net_cost_or_proceeds=net
        )

    async def _place_real_order(self, order_id: str, symbol: str, side: str, qty: float, price: float) -> Order:
        """
        Places a real order via CCXT with advanced exception handling and position/balance reconciliation.
        """
        if not self.exchange:
            logger.warning("No exchange instance provided to ExecutionEngine. Using simulated execution fallback.")
            return Order(
                order_id=order_id,
                symbol=symbol,
                side=side.upper(),
                status=OrderStatus.FILLED,
                original_quantity=qty,
                filled_quantity=qty,
                avg_price=price,
                net_cost_or_proceeds=0.0
            )

        if not ccxt:
            logger.error("CCXT library not available. Failing real order placement.")
            return Order(
                order_id=order_id,
                symbol=symbol,
                side=side.upper(),
                status=OrderStatus.FAILED,
                original_quantity=qty,
                filled_quantity=0.0,
                avg_price=price
            )

        logger.info(f"🚀 Placing real order via CCXT: {symbol} | {side} | Qty: {qty} | Price: {price}")
        
        try:
            # Place order on exchange (using Limit order type for execution control)
            order_params = {'clientOrderId': order_id}
            response = await self.exchange.create_order(
                symbol=symbol,
                type='limit',
                side=side.lower(),
                amount=qty,
                price=price,
                params=order_params
            )
            
            filled_qty = float(response.get('filled', 0.0))
            status_str = response.get('status', 'open')
            avg_price = float(response.get('average', price) or price)
            
            if status_str == 'closed':
                status = OrderStatus.FILLED
            elif filled_qty > 0:
                status = OrderStatus.PARTIAL
            else:
                status = OrderStatus.PENDING

            return Order(
                order_id=order_id,
                symbol=symbol,
                side=side.upper(),
                status=status,
                original_quantity=qty,
                filled_quantity=filled_qty,
                avg_price=avg_price
            )

        except ccxt.InsufficientFunds as e:
            logger.error(f"❌ Real order InsufficientFunds for {symbol}: {e}")
            return Order(
                order_id=order_id,
                symbol=symbol,
                side=side.upper(),
                status=OrderStatus.FAILED,
                original_quantity=qty,
                filled_quantity=0.0,
                avg_price=price
            )

        except ccxt.InvalidOrder as e:
            logger.error(f"❌ Real order InvalidOrder (price moved too fast or invalid size) for {symbol}: {e}")
            return Order(
                order_id=order_id,
                symbol=symbol,
                side=side.upper(),
                status=OrderStatus.FAILED,
                original_quantity=qty,
                filled_quantity=0.0,
                avg_price=price
            )

        except ccxt.NetworkError as e:
            logger.warning(f"⚠️ Real order NetworkError occurred for {symbol}: {e}. Initiating position reconciliation...")
            # Reconcile position to see if order was partially/fully filled
            try:
                # 1. Attempt to fetch order status using clientOrderId
                orders = await self.exchange.fetch_orders(symbol, limit=10)
                for o in orders:
                    if o.get('clientOrderId') == order_id or o.get('id') == response.get('id', ''):
                        filled_qty = float(o.get('filled', 0.0))
                        status_str = o.get('status', 'open')
                        avg_price = float(o.get('average', price) or price)
                        logger.info(f"Reconciled order via history: {o['id']} status={status_str}, filled={filled_qty}")
                        
                        if status_str == 'closed':
                            status = OrderStatus.FILLED
                        elif filled_qty > 0:
                            status = OrderStatus.PARTIAL
                        else:
                            status = OrderStatus.PENDING

                        return Order(
                            order_id=order_id,
                            symbol=symbol,
                            side=side.upper(),
                            status=status,
                            original_quantity=qty,
                            filled_quantity=filled_qty,
                            avg_price=avg_price
                        )
            except Exception as hist_err:
                logger.error(f"Failed to reconcile via order history: {hist_err}")

            try:
                # 2. Reconcile via current position / balance balance checks
                if hasattr(self.exchange, 'fetch_positions'):
                    positions = await self.exchange.fetch_positions([symbol])
                    for pos in positions:
                        if pos.get('symbol') == symbol:
                            pos_size = abs(float(pos.get('contracts', pos.get('size', 0.0))))
                            logger.info(f"Reconciled position details for {symbol}: current position contracts={pos_size}")
                            # If there's an active position, we treat it as partially filled
                            if pos_size > 0.0:
                                return Order(
                                    order_id=order_id,
                                    symbol=symbol,
                                    side=side.upper(),
                                    status=OrderStatus.PARTIAL,
                                    original_quantity=qty,
                                    filled_quantity=min(qty, pos_size),
                                    avg_price=price
                                )
            except Exception as pos_err:
                logger.error(f"Reconciliation via position balance failed: {pos_err}")

            # Return PENDING state to allow subsequent re-check/refill loop reconciliation
            return Order(
                order_id=order_id,
                symbol=symbol,
                side=side.upper(),
                status=OrderStatus.PENDING,
                original_quantity=qty,
                filled_quantity=0.0,
                avg_price=price
            )

        except Exception as e:
            logger.error(f"❌ Real order unexpected execution error for {symbol}: {e}")
            return Order(
                order_id=order_id,
                symbol=symbol,
                side=side.upper(),
                status=OrderStatus.FAILED,
                original_quantity=qty,
                filled_quantity=0.0,
                avg_price=price
            )
