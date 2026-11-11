"""仓位管理与分批建仓（要求 3）。

规则：
- 总仓位 20%-80%，永久保留≥20% 现金。
- 单股≤总资金 5%，单行业≤15%。
- 分批建仓：首仓 40%，回调 2% 加 30%，再回调 2% 加剩余 30%。
- 亏损头寸绝不补仓（仅盈利头寸可加仓）。
"""
from typing import List

from config.strategy import StrategyConfig
from core.order import Side, OrderType


class PositionPlanner:
    def __init__(self, cfg: StrategyConfig):
        self.cfg = cfg

    @staticmethod
    def to_lot(qty: int) -> int:
        """向下取整到整手（100 股）。"""
        return (qty // 100) * 100

    def target_shares(self, total_assets: float, weight: float, price: float) -> int:
        """按占总资金的权重计算目标股数（整手）。"""
        if price <= 0:
            return 0
        mv = total_assets * weight
        return self.to_lot(int(mv // price))

    def batch_shares(self, target_shares: int, step: int) -> int:
        """第 step 批（1-based）应买入股数：取 batch_plan 对应比例。"""
        if step < 1 or step > len(self.cfg.batch_plan):
            return 0
        proportion = self.cfg.batch_plan[step - 1]
        return self.to_lot(int(target_shares * proportion))

    def pullback_threshold(self, entry_price: float, step: int) -> float:
        """第 step 批（已买入 step-1 批）触发加仓的回调价阈值。"""
        return round(entry_price * (1 - self.cfg.batch_pullback * (step - 1)), 3)
