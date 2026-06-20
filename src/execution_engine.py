import time
import random
import uuid
import logging
from enum import Enum
from dataclasses import dataclass
from typing import Dict, Any, List

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
    def __init__(self) -> None:
        # Tracks pending/active orders
        self.pending_orders: List[Order] = []

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
        BUY: target_price * (1 - step_percent * i)
        SELL: target_price * (1 + step_percent * i)
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

    def execute_order(
        self,
        symbol: str,
        side: str,
        total_quantity: float,
        target_price: float,
        config: Dict[str, Any]
    ) -> List[Order]:
        """
        Executes an order. Triggers step-limit splits if quantity > split_threshold
        or if coin_type is "HighVolatility".
        """
        is_simulated = config.get("is_simulated", True)
        split_threshold = config.get("split_threshold", 100.0)
        coin_type = config.get("coin_type", "Normal")
        num_splits = config.get("num_splits", 5)
        step_percent = config.get("step_percent", 0.001)
        fee_rate = config.get("fee_rate", 0.001)
        slippage_model = config.get("slippage_model", 0.0005)

        should_split = (total_quantity > split_threshold) or (coin_type == "HighVolatility")

        logger.info(
            f"🔄 execute_order: {symbol} | {side} | Qty: {total_quantity} | Target: {target_price} "
            f"| Volatility: {coin_type} | Should Split: {should_split}"
        )

        executed_orders: List[Order] = []

        if should_split:
            specs = self.generate_split_orders(total_quantity, target_price, side, num_splits, step_percent)
            
            for spec in specs:
                order_id = str(uuid.uuid4())
                price = spec["price"]
                qty = spec["qty"]
                idx = spec["index"]

                # Simulated probability calculation (fill probability decreases with step distance)
                # 1st split (index 0) has 95% prob, 2nd has 80%, etc.
                fill_probability = max(0.1, 0.95 - idx * 0.15)

                if is_simulated:
                    order = self._place_simulated_order(
                        order_id, symbol, side, qty, price, fee_rate, slippage_model, fill_probability
                    )
                else:
                    order = self._place_real_order(order_id, symbol, side, qty, price)

                executed_orders.append(order)
                
                # State tracking
                if order.status != OrderStatus.FILLED:
                    self.pending_orders.append(order)
                    logger.warning(f"⚠️ Order {order.order_id} not fully filled. Status: {order.status.value}")
        else:
            # Single order execution
            order_id = str(uuid.uuid4())
            if is_simulated:
                order = self._place_simulated_order(
                    order_id, symbol, side, total_quantity, target_price, fee_rate, slippage_model, 0.95
                )
            else:
                order = self._place_real_order(order_id, symbol, side, total_quantity, target_price)
            executed_orders.append(order)
            if order.status != OrderStatus.FILLED:
                self.pending_orders.append(order)

        # Logging execution summary
        total_filled = sum(o.filled_quantity for o in executed_orders)
        if total_filled > 0:
            avg_fill_price = sum(o.avg_price * o.filled_quantity for o in executed_orders) / total_filled
        else:
            avg_fill_price = 0.0

        logger.info(
            f"📊 Execution Summary | Total Filled: {total_filled:.4f}/{total_quantity:.4f} "
            f"| Avg Fill Price: {avg_fill_price:.6f} vs Original Target: {target_price:.6f}"
        )

        return executed_orders

    def recalculate_remaining_splits(
        self,
        symbol: str,
        side: str,
        remaining_quantity: float,
        current_market_price: float,
        config: Dict[str, Any]
    ) -> List[Order]:
        """
        Recalculates split orders for any remaining unfilled quantity based on current market price.
        """
        logger.info(f"🔄 Recalculating remaining splits for {symbol} | Side: {side} | Remaining Qty: {remaining_quantity:.4f}")
        # Treat the remaining quantity as a new split order request
        return self.execute_order(symbol, side, remaining_quantity, current_market_price, config)

    def _place_simulated_order(
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
        # Simulate network latency (100ms - 500ms)
        delay = random.uniform(0.1, 0.5)
        time.sleep(delay)

        roll = random.random()

        if roll > fill_probability:
            # FAILED order
            status = OrderStatus.FAILED
            filled_qty = 0.0
            avg_price = 0.0
            net = 0.0
        elif roll > fill_probability - 0.1:
            # PARTIAL fill
            status = OrderStatus.PARTIAL
            filled_qty = qty * random.uniform(0.2, 0.8)
            slippage = random.normalvariate(0, slippage_model)
            avg_price = price * (1.0 + slippage)
            raw_val = filled_qty * avg_price
            fee = raw_val * fee_rate
            net = -(raw_val + fee) if side.upper() == "BUY" else (raw_val - fee)
        else:
            # FILLED order
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

    def _place_real_order(self, order_id: str, symbol: str, side: str, qty: float, price: float) -> Order:
        logger.warning("Real exchange execution is not implemented. Returning placeholder filled order.")
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
