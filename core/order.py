"""订单 / 成交 / 持仓 数据结构。

量化交易专家审核要点：
- Position.available 与 quantity 分离，用于实现 T+1（当日买入 available 不增加）。
- Trade.cash_flow 统一符号：买入为负（现金流出），卖出为正（现金流入），含费后净额。
- cost_price 为成交价加权成本（不含费）；税费在盈亏统计中单独体现，口径清晰。
"""
from dataclasses import dataclass, field
from datetime import datetime, date
from enum import Enum
from typing import Optional


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(str, Enum):
    MARKET = "MARKET"   # 市价单：按当前行情价撮合
    LIMIT = "LIMIT"     # 限价单：价格触及时撮合，否则挂单


class OrderStatus(str, Enum):
    PENDING = "PENDING"       # 已报，待撮合（如限价未触达）
    FILLED = "FILLED"         # 全部成交
    PARTIAL = "PARTIAL"       # 部分成交
    REJECTED = "REJECTED"     # 拒单（规则校验未通过）
    CANCELLED = "CANCELLED"   # 已撤单


@dataclass
class Order:
    order_id: str
    code: str
    name: str
    side: Side
    order_type: OrderType
    price: float                       # 限价（MARKET 时为 0）
    quantity: int                      # 申报数量（股）
    quantity_filled: int = 0
    status: OrderStatus = OrderStatus.PENDING
    reject_reason: str = ""
    created_at: datetime = field(default_factory=datetime.now)
    trading_day: Optional[date] = None


@dataclass
class Trade:
    """一笔成交记录（已撮合）。"""
    trade_id: str
    order_id: str
    code: str
    name: str
    side: Side
    price: float
    quantity: int
    amount: float                      # 成交额 = price * quantity
    commission: float
    stamp_tax: float
    transfer_fee: float
    total_fee: float
    cash_flow: float                   # 含费后现金变动：买入负 / 卖出正
    realized_pnl: float = 0.0          # 该笔卖出带来的已实现盈亏（买入为 0）
    datetime: datetime = field(default_factory=datetime.now)
    trading_day: Optional[date] = None


@dataclass
class Position:
    """持仓（按股票代码）。"""
    code: str
    name: str
    quantity: int = 0                  # 总持仓（股）
    available: int = 0                 # 可卖数量（T+1 释放后）
    cost_price: float = 0.0            # 加权持仓成本（成交价，不含费）
    frozen: int = 0                    # 已挂卖单冻结数量

    def market_value(self, price: float) -> float:
        return self.quantity * price

    def unrealized_pnl(self, price: float) -> float:
        return (price - self.cost_price) * self.quantity
