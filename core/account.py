"""账户管理模块。

负责账户状态（现金、持仓、订单、成交、已实现盈亏）的维护与本地序列化。
不直接撮合交易，仅提供状态读写与冻结/解冻能力，由撮合引擎驱动。
"""
from dataclasses import dataclass, field
from datetime import date
from typing import Dict, List, Optional

from config.settings import SimulatorConfig
from core.order import Order, Trade, Position


class AccountManager:
    def __init__(self, config: SimulatorConfig):
        self.cfg = config
        self.initial_capital: float = config.initial_capital
        self.cash: float = config.initial_capital          # 可用现金（已扣除挂单冻结）
        self.frozen_cash: float = 0.0                      # 挂单冻结资金（限价买单保证金）
        self.positions: Dict[str, Position] = {}
        self.orders: Dict[str, Order] = {}
        self.trades: List[Trade] = []
        self.realized_pnl: float = 0.0
        self.current_day: Optional[date] = None

    # ---------- 持仓访问 ----------
    def get_position(self, code: str) -> Optional[Position]:
        return self.positions.get(code)

    def ensure_position(self, code: str, name: str) -> Position:
        if code not in self.positions:
            self.positions[code] = Position(code=code, name=name)
        return self.positions[code]

    # ---------- 冻结 / 解冻（限价挂单使用）----------
    def freeze_cash(self, amount: float) -> None:
        self.cash -= amount

    def release_cash(self, amount: float) -> None:
        self.cash += amount

    # ---------- T+1 跨日释放 ----------
    def advance_trading_day(self, new_day: date) -> None:
        """切换到新交易日：当日买入的股票变为可卖（保留挂单冻结部分）。"""
        self.current_day = new_day
        for pos in self.positions.values():
            pos.available = pos.quantity - pos.frozen

    # ---------- 序列化 ----------
    def to_dict(self) -> dict:
        return {
            "initial_capital": self.initial_capital,
            "cash": round(self.cash, 4),
            "frozen_cash": round(self.frozen_cash, 4),
            "realized_pnl": round(self.realized_pnl, 4),
            "current_day": self.current_day.isoformat() if self.current_day else None,
            "positions": {
                c: {
                    "code": p.code, "name": p.name, "quantity": p.quantity,
                    "available": p.available, "cost_price": p.cost_price,
                    "frozen": p.frozen,
                } for c, p in self.positions.items()
            },
        }

    @classmethod
    def from_dict(cls, data: dict, config: SimulatorConfig) -> "AccountManager":
        acc = cls(config)
        acc.initial_capital = data.get("initial_capital", config.initial_capital)
        acc.cash = data.get("cash", config.initial_capital)
        acc.frozen_cash = data.get("frozen_cash", 0.0) or 0.0
        acc.realized_pnl = data.get("realized_pnl", 0.0)
        day = data.get("current_day")
        acc.current_day = date.fromisoformat(day) if day else None
        for c, p in data.get("positions", {}).items():
            acc.positions[c] = Position(
                code=p["code"], name=p["name"], quantity=p["quantity"],
                available=p["available"], cost_price=p["cost_price"], frozen=p["frozen"],
            )
        return acc
