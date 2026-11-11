"""持仓计算模块。

提供持仓成本更新、市值与浮动盈亏计算的纯函数式工具。
cost_price 采用成交价加权法（不含费），税费在盈亏统计中单独核算。
"""
from typing import Dict

from core.order import Position, Side


class PositionCalculator:
    @staticmethod
    def update_after_buy(pos: Position, qty: int, amount: float) -> None:
        """买入后更新持仓：加权成本上升，数量增加，available 不变（T+1）。"""
        old_qty = pos.quantity
        new_qty = old_qty + qty
        if new_qty > 0:
            pos.cost_price = (old_qty * pos.cost_price + amount) / new_qty
        pos.quantity = new_qty

    @staticmethod
    def update_after_sell(pos: Position, qty: int) -> None:
        """卖出后更新持仓：数量与可卖同步减少，清仓则成本归零。"""
        pos.quantity -= qty
        pos.available -= qty
        pos.frozen = max(0, pos.frozen - qty)
        if pos.quantity <= 0:
            pos.quantity = 0
            pos.available = 0
            pos.frozen = 0
            pos.cost_price = 0.0

    @staticmethod
    def release_t1(pos: Position) -> None:
        """T+1 跨日释放：可卖 = 总持仓 - 挂单冻结。"""
        pos.available = pos.quantity - pos.frozen

    @staticmethod
    def market_value(pos: Position, price: float) -> float:
        return pos.market_value(price)

    @staticmethod
    def unrealized(pos: Position, price: float) -> float:
        return pos.unrealized_pnl(price)

    @staticmethod
    def total_market_value(positions: Dict[str, Position],
                           prices: Dict[str, float]) -> float:
        return sum(p.market_value(prices.get(c, 0.0)) for c, p in positions.items())
