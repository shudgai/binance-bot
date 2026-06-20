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
        pass

    def execute_order(
        self,
        symbol: str,
        side: str,
        total_quantity: float,
        target_price: float,
        config: Dict[str, Any]
    ) -> List[Order]:
        """
        Executes a trade order by splitting it into multiple step-limit orders.
        Supports both high-fidelity simulation and real exchange execution.
        """
        is_simulated = config.get("is_simulated", True)
        num_splits = config.get("num_splits", 5)
        step_percent = config.get("step_percent", 0.001)
        fee_rate = config.get("fee_rate", 0.001)
        slippage_model = config.get("slippage_model", 0.0005)

        logger.info(
            f"🔄 Starting execution for {symbol} | Side: {side} | Total Qty: {total_quantity} "
            f"| Target Price: {target_price} | Splits: {num_splits} | Simulated: {is_simulated}"
        )

        if num_splits <= 0:
            raise ValueError("num_splits must be greater than 0")

        qty_per_split = total_quantity / num_splits
        orders: List[Order] = []

        for i in range(num_splits):
            # Calculate step-limit price
            if side.upper() == "BUY":
                # For BUY, place orders at slightly lower prices
                price = target_price * (1.0 - step_percent * i)
            elif side.upper() == "SELL":
                # For SELL, place orders at slightly higher prices
                price = target_price * (1.0 + step_percent * i)
            else:
                raise ValueError("side must be 'BUY' or 'SELL'")

            order_id = str(uuid.uuid4())
            logger.info(f" Placing split order {i+1}/{num_splits} | Price: {price:.6f} | Qty: {qty_per_split:.6f}")

            if is_simulated:
                order = self._place_simulated_order(order_id, symbol, side, qty_per_split, price, fee_rate, slippage_model)
            else:
                order = self._place_real_order(order_id, symbol, side, qty_per_split, price)

            orders.append(order)
            logger.info(f" Order {order.order_id} status: {order.status.value} | Avg Price: {order.avg_price:.6f} | Filled Qty: {order.filled_quantity:.6f}")

        return orders

    def _place_simulated_order(
        self,
        order_id: str,
        symbol: str,
        side: str,
        qty: float,
        price: float,
        fee_rate: float,
        slippage_model: float
    ) -> Order:
        # 1. Latency simulation: random delay between 0.1 and 0.5 seconds
        delay = random.uniform(0.1, 0.5)
        time.sleep(delay)

        # 2. Random outcome (high fidelity)
        # 85% fully filled, 10% partially filled, 5% failed
        roll = random.random()
        if roll < 0.05:
            # Failed
            status = OrderStatus.FAILED
            filled_qty = 0.0
            avg_price = 0.0
            net = 0.0
        elif roll < 0.15:
            # Partial fill (between 10% and 90% of the qty)
            status = OrderStatus.PARTIAL
            filled_qty = qty * random.uniform(0.1, 0.9)
            # Add dynamic slippage
            slippage = random.normalvariate(0, slippage_model)
            avg_price = price * (1.0 + slippage)
            
            # Cost or proceeds calculations
            raw_val = filled_qty * avg_price
            fee = raw_val * fee_rate
            if side.upper() == "BUY":
                net = -(raw_val + fee)
            else:
                net = raw_val - fee
        else:
            # Fully filled
            status = OrderStatus.FILLED
            filled_qty = qty
            # Add dynamic slippage
            slippage = random.normalvariate(0, slippage_model)
            avg_price = price * (1.0 + slippage)
            
            raw_val = filled_qty * avg_price
            fee = raw_val * fee_rate
            if side.upper() == "BUY":
                net = -(raw_val + fee)
            else:
                net = raw_val - fee

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
        """
        Placeholder method for the actual exchange API logic.
        """
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
