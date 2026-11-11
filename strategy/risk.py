"""回撤风控铁律状态机（要求 5，不可突破）。

优先级（高→低）：历史高点回撤 ≥5% > 单周回撤 ≥3% > 单日回撤 ≥1.5% > 正常。

铁律映射：
- 单日账户总回撤 ≥1.5%：当日强制降总仓位至 30% 以下；次日暂停开新仓。
- 单周账户总回撤 ≥3%：进入防守模式，只卖不买，总仓位不超过 20%。
- 账户从历史高点回撤 ≥5%：全面防守，仅保留 10% 以内底仓（只卖不买）。
"""
from datetime import date
from enum import Enum
from typing import Optional

from config.strategy import StrategyConfig
from market.base import MarketProvider


class RiskMode(str, Enum):
    NORMAL = "NORMAL"
    DEFENSE_DAILY = "DEFENSE_DAILY"      # 当日降仓 + 次日禁开
    DEFENSE_WEEKLY = "DEFENSE_WEEKLY"    # 只卖不买，≤20%
    DEFENSE_PEAK = "DEFENSE_PEAK"        # 全面防守，≤10%


class RiskController:
    def __init__(self, cfg: StrategyConfig, market: MarketProvider):
        self.cfg = cfg
        self.market = market
        self.peak_equity: float = 0.0
        self.prev_day_equity: float = 0.0
        self.week_start_equity: float = 0.0
        self.week_key: Optional[str] = None
        self.mode: RiskMode = RiskMode.NORMAL
        self.pause_open_until: Optional[date] = None

    # ---------- 周键 ----------
    @staticmethod
    def _week_key(d: date) -> str:
        return f"{d.isocalendar()[0]}-W{d.isocalendar()[1]:02d}"

    # ---------- 日初：检测状态（基于前收 vs 今值） ----------
    def update(self, equity: float, day: date) -> RiskMode:
        if self.peak_equity <= 0:
            self.peak_equity = equity
        if self.prev_day_equity <= 0:
            self.prev_day_equity = equity
        if self.week_start_equity <= 0 or self.week_key != self._week_key(day):
            self.week_start_equity = equity
            self.week_key = self._week_key(day)

        peak_dd = (self.peak_equity - equity) / self.peak_equity if self.peak_equity > 0 else 0.0
        daily_dd = (self.prev_day_equity - equity) / self.prev_day_equity if self.prev_day_equity > 0 else 0.0
        weekly_dd = (self.week_start_equity - equity) / self.week_start_equity if self.week_start_equity > 0 else 0.0

        if peak_dd >= self.cfg.dd_peak_max:
            self.mode = RiskMode.DEFENSE_PEAK
            self.pause_open_until = None
        elif weekly_dd >= self.cfg.dd_weekly_max:
            self.mode = RiskMode.DEFENSE_WEEKLY
            self.pause_open_until = None
        elif daily_dd >= self.cfg.dd_daily_max:
            self.mode = RiskMode.DEFENSE_DAILY
            self.pause_open_until = self.market.next_trading_day(day)
        else:
            self.mode = RiskMode.NORMAL
            self.pause_open_until = None
        return self.mode

    # ---------- 日终：刷新峰值/前收/周初（基于交易后净值） ----------
    def close(self, equity: float, day: date) -> None:
        self.peak_equity = max(self.peak_equity, equity)
        self.prev_day_equity = equity
        if self.week_key != self._week_key(day):
            self.week_start_equity = equity
            self.week_key = self._week_key(day)

    # ---------- 查询 ----------
    def position_cap(self) -> float:
        if self.mode == RiskMode.DEFENSE_PEAK:
            return self.cfg.dd_peak_pos_cap
        if self.mode == RiskMode.DEFENSE_WEEKLY:
            return self.cfg.dd_weekly_pos_cap
        if self.mode == RiskMode.DEFENSE_DAILY:
            return self.cfg.dd_daily_reduce_to
        return 1.0

    def sell_only(self) -> bool:
        return self.mode in (RiskMode.DEFENSE_WEEKLY, RiskMode.DEFENSE_PEAK)

    def allow_open(self, day: date) -> bool:
        if self.sell_only():
            return False
        if self.pause_open_until is not None and day <= self.pause_open_until:
            return False
        return True

    # ---------- 序列化 ----------
    def to_dict(self) -> dict:
        return {
            "peak_equity": self.peak_equity,
            "prev_day_equity": self.prev_day_equity,
            "week_start_equity": self.week_start_equity,
            "week_key": self.week_key,
            "mode": self.mode.value,
            "pause_open_until": self.pause_open_until.isoformat()
            if self.pause_open_until else None,
        }

    def load_dict(self, d: dict) -> None:
        if not d:
            return
        self.peak_equity = d.get("peak_equity", 0.0) or 0.0
        self.prev_day_equity = d.get("prev_day_equity", 0.0) or 0.0
        self.week_start_equity = d.get("week_start_equity", 0.0) or 0.0
        self.week_key = d.get("week_key")
        self.mode = RiskMode(d.get("mode", "NORMAL"))
        pu = d.get("pause_open_until")
        self.pause_open_until = date.fromisoformat(pu) if pu else None

    # ---------- 健全性清洗（防 demo/历史污染锁死） ----------
    def sanitize(self, equity: float, initial_capital: float,
                 has_real_trades: bool = False) -> bool:
        """清洗被污染的风控峰值，避免永久锁死 DEFENSE_PEAK。

        peak_equity 的语义是账户历史真实达到过的最高净值，只能由真实
        equity 经 close() 推高。从外部状态文件加载时可能带入 demo 或历史
        残留的非真实高点，从而误判回撤并永久防守（尤其账户从未真实交易时，
        净值恒为初始本金，却被一个凭空的高点判定为持续大幅回撤）。

        清洗规则：
          - 若峰值低于当前净值（违反定义），上修为当前净值；
          - 仅当账户「从未真实交易」时，峰值才绝对不应超过初始本金——
            未经任何买卖的账户净值恒为 initial_capital，任何高于它的峰值
            必为污染，必须钳制。已交易账户的峰值是真实历史高点（含回撤情形，
            真实防守依赖它），不可剪除，交由 close() 经真实 equity 自然维护。
        返回是否做了修正。
        """
        fixed = False
        if self.peak_equity < equity:
            self.peak_equity = equity
            fixed = True
        if not has_real_trades and self.peak_equity > initial_capital:
            self.peak_equity = initial_capital
            fixed = True
        return fixed
