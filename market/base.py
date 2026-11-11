"""行情数据源抽象接口（行情对接模块）。

所有行情实现必须继承 MarketProvider：
- WeStockProvider：对接 westock-data 技能（westock CLI），获取实时行情 / K线 / 基本面。
- MockProvider：离线演示与单元测试（内置样本数据，无需联网）。

统一通过 get_quote / get_kline / get_fundamentals 三个入口获取数据，
策略层额外依赖 get_close_history（因子计算）/ get_index_constituents（指数池）/
get_universe（全市场扫描起点），通过 is_trading_day / next_trading_day 提供交易日历。
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date
from typing import List, Dict, Any, Optional


@dataclass
class Quote:
    code: str
    name: str
    price: float
    prev_close: float
    open: float
    high: float
    low: float
    ceiling: float = 0.0       # 涨停价（A股由 westock 直接返回）
    floor: float = 0.0         # 跌停价
    time: str = ""
    volume: float = 0.0        # 成交量（股）
    change_pct: float = 0.0    # 涨跌幅（%，(price-prev_close)/prev_close*100）
    suspended: bool = False    # 是否停牌（行情源标注 / 辅助判定）


@dataclass
class Kline:
    date: str
    open: float
    close: float
    high: float
    low: float
    volume: float
    amount: float
    change_pct: float


@dataclass
class Fundamentals:
    """标准化基本面（因子模型输入）。所有金额单位：元 / 比率无量纲。"""
    code: str
    name: str
    industry: str = ""
    # 质量因子
    roe_3y: List[float] = field(default_factory=list)   # 近3年 ROE（近期在前）
    ocf: float = 0.0                                    # 经营活动现金流净额
    net_profit: float = 0.0                             # 净利润（TTM）
    net_profit_ttm: float = 0.0                         # 近一年净利润（≤0 即亏损）
    gross_margin_series: List[float] = field(default_factory=list)  # 近年毛利率序列
    # 估值因子
    pe: float = 0.0
    pb: float = 0.0
    pe_pct_3y: float = 1.0                             # PE 近3年分位 [0,1]
    pb_pct_3y: float = 1.0                             # PB 近3年分位 [0,1]
    # 风险因子
    pledge_ratio: float = 0.0                          # 股权质押率
    avg_turnover_30d: float = 0.0                      # 近30日日均成交额
    # 原始标记
    is_st: bool = False


class MarketProvider(ABC):
    @abstractmethod
    def get_quote(self, code: str) -> Quote:
        """获取实时行情（含涨跌停价）。"""

    @abstractmethod
    def get_kline(self, code: str, period: str = "day", limit: int = 20) -> List[Kline]:
        """获取 K 线。period: day/week/month/season/year 或 m1/m5/...。"""

    @abstractmethod
    def get_fundamentals(self, code: str) -> Fundamentals:
        """获取标准化基本面（因子模型输入）。"""

    @abstractmethod
    def get_close_history(self, code: str, days: int = 126) -> List[float]:
        """获取最近 days 个交易日的收盘价序列（旧→新），用于波动率/回撤/动量/分位计算。"""

    @abstractmethod
    def get_index_constituents(self, index_code: str) -> List[str]:
        """获取指数成分股代码列表（如沪深300 sh000300 / 中证500 sz399905）。"""

    @abstractmethod
    def get_universe(self) -> List[str]:
        """返回可供筛选的全市场股票代码（默认应在策略层再按板块/基本面过滤）。"""

    @abstractmethod
    def is_trading_day(self, d: date) -> bool:
        """判断 date 是否为 A股交易日（用于 T+1 跨日判定）。"""

    @abstractmethod
    def next_trading_day(self, d: date) -> date:
        """返回 d 之后的下一个交易日。"""

    def market_breadth(self) -> List[dict]:
        """市场宽度快照：返回当日候选池行情列表，用于情绪面结构指标计算。

        每条 dict 至少含 code/price/prev_close/high/low/ceiling/floor/change_pct；
        无数据源时返回空列表（情绪指标自动降级为仅有价格派生项）。
        默认实现为空，由具体 provider 按需覆写（如从已预热的 quote 缓存零成本提取）。
        """
        return []
