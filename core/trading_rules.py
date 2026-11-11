"""交易规则引擎：交易时段、T+1、涨跌幅、整手校验。

所有方法为纯函数式校验，不依赖外部状态，便于单元测试与回测复用。
优先使用行情返回的涨停价/跌停价（price_ceiling / price_floor）；
无行情时按股票名称是否含 'st' 判定涨跌停幅度（ST 股 ±5%，其余主板 ±10%）。

交易时段口径声明（一致性核查要求显式声明）：
- 撮合引擎仅在连续竞价时段执行：9:30–11:30、13:00–15:00（含端点）。
- 9:00–9:30 为盘前 / 集合竞价准备时段，本系统不实现集合竞价的独立撮合算法，
  该时段不撮合、不拒单等待，仅作展示标签（看板标注为「盘前 / 集合竞价」）。
"""
from datetime import datetime, date, time as dtime
from config.settings import TradingRuleConfig


class TradingRules:
    def __init__(self, config: TradingRuleConfig):
        self.cfg = config

    # ---------- 交易时段 ----------
    def _in_range(self, t: dtime, o, c) -> bool:
        return dtime(*o) <= t <= dtime(*c)

    def is_trading_time(self, dt: datetime) -> bool:
        """是否在连续竞价时段：9:30-11:30 或 13:00-15:00（含端点）。"""
        t = dt.time()
        return self._in_range(t, self.cfg.morning_open, self.cfg.morning_close) or \
            self._in_range(t, self.cfg.afternoon_open, self.cfg.afternoon_close)

    # ---------- 整手 ----------
    def validate_lot(self, quantity: int) -> bool:
        """申报数量必须为 100 股正整数倍。"""
        return quantity > 0 and quantity % self.cfg.lot_size == 0

    # ---------- 板块涨跌幅 ----------
    def price_limit_pct(self, code: str, name: str = "") -> float:
        """根据代码 / 名称判定涨跌幅比例。

        稳定复利策略仅交易沪深主板：
        - ST / *ST：±5%
        - 其余（主板）：±10%
        """
        if "st" in (name or "").lower():
            return self.cfg.price_limit_pct["star"]
        return self.cfg.price_limit_pct["main"]

    def compute_price_limit(self, code: str, prev_close: float, name: str = "") -> tuple:
        """回退计算涨停价 / 跌停价（四舍五入至分）。"""
        pct = self.price_limit_pct(code, name)
        ceiling = round(prev_close * (1 + pct), 2)
        floor = round(prev_close * (1 - pct), 2)
        return ceiling, floor

    def validate_price_limit(self, price: float, ceiling: float, floor: float) -> bool:
        """委托/成交价须落在 [跌停, 涨停] 区间内。"""
        if ceiling <= 0 or floor <= 0:
            return True  # 无涨跌停数据（如指数/ETF）时不限制
        return floor <= price <= ceiling

    # ---------- T+1 ----------
    def can_sell(self, available: int, quantity: int) -> bool:
        """可卖数量需满足：当日买入（available 未释放）不可卖。"""
        return available >= quantity
