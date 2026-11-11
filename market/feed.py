"""行情接入守护 / 校验层（要求 1：行情接入核查与修复）。

系统行情为 pull 模式（由策略调度 / 看板按需调用 MarketProvider.get_quote）。
本模块在行情进入交易链路前提供一道统一校验与清洗闸门，覆盖：
- 数据准确性：价格必须为正且有限；涨跌停价（若有）须 ceiling ≥ floor。
- 停牌检测：行情源显式标注 / 成交量为 0 且现价等于昨收（长期无成交）→ 判停牌，
  禁止进入撮合与展示为可交易价。
- 时间连续性：时间戳单调递增，发现回退 / 断档（间隔超过阈值）即标记。
- 推送去重：同一标的同时间戳同价视为重复条目，直接丢弃。
- 推送频率：统计实际推送间隔，与预期频率比对（误差≤10% 视为正常）。

Feed 为纯函数 + 轻量状态机，可离线注入样本行情做单元验证。
"""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Tuple


def validate_quote(price: float, prev_close: float,
                   ceiling: float = 0.0, floor: float = 0.0) -> Tuple[bool, str]:
    """行情数据准确性校验。返回 (是否通过, 不通过原因)。"""
    try:
        p = float(price)
    except (TypeError, ValueError):
        return False, "价格非数值"
    if not (p == p):                      # NaN
        return False, "价格为 NaN"
    if p <= 0:
        return False, f"价格非正: {price}"
    try:
        pc = float(prev_close)
    except (TypeError, ValueError):
        return False, "昨收非数值"
    if pc <= 0:
        return False, f"昨收非正: {prev_close}"
    if ceiling > 0 and floor > 0 and ceiling < floor:
        return False, f"涨跌停价倒置: ceiling={ceiling} < floor={floor}"
    return True, ""


def detect_suspension(price: float, prev_close: float,
                      volume: float = 0.0, suspended: bool = False) -> bool:
    """停牌检测（行情校验层）。

    判定规则：
      - 行情源显式标注 suspended=True → 直接判停牌；
      - 辅助判定：成交量为 0 且现价等于昨收（长期无成交、无波动）→ 疑似停牌。
    返回 True 表示标的处于停牌状态，禁止撮合交易、展示为「停牌」而非可交易价。
    """
    if suspended:
        return True
    try:
        p = float(price); pc = float(prev_close); v = float(volume)
    except (TypeError, ValueError):
        return False
    if v == 0 and p > 0 and abs(p - pc) < 1e-9:
        return True
    return False


@dataclass
class Tick:
    """一条行情 tick（测试 / 守护注入用）。"""
    code: str
    price: float
    ts: datetime
    prev_close: float = 0.0
    ceiling: float = 0.0
    floor: float = 0.0


@dataclass
class FeedResult:
    code: str
    price: float
    ts: datetime
    ok: bool = True                 # 是否为可采纳（通过校验且非重复）
    reason: str = ""                # 不采纳 / 告警原因
    duplicate: bool = False
    gap: bool = False
    seq: int = 0


class MarketFeed:
    """行情清洗闸门：去重 + 时间连续性 + 频率统计。

    push(tick): 顺序喂入行情 tick，返回 FeedResult（重复/异常会被标记且 ok=False）。
    report(): 汇总重复条数、断档次数、时间戳是否单调递增、推送频率误差。
    """

    def __init__(self, expected_interval_sec: float = 3.0,
                 gap_threshold_sec: float = 60.0):
        self.expected_interval = expected_interval_sec
        self.gap_threshold = gap_threshold_sec
        self._last: Dict[str, datetime] = {}
        self._last_price: Dict[str, float] = {}
        self._seq: Dict[str, int] = {}
        self.results: List[FeedResult] = []
        self.duplicates: int = 0
        self.gaps: int = 0
        self.monotonic: bool = True
        self._intervals: Dict[str, List[float]] = {}

    def push(self, tick: Tick) -> FeedResult:
        ok, reason = validate_quote(tick.price, tick.prev_close,
                                     tick.ceiling, tick.floor)
        seq = self._seq.get(tick.code, 0) + 1
        self._seq[tick.code] = seq
        res = FeedResult(code=tick.code, price=tick.price, ts=tick.ts,
                         ok=ok, reason=reason, seq=seq)

        if not ok:
            self.results.append(res)
            return res

        # 时间连续性：不允许时间戳回退
        prev_ts = self._last.get(tick.code)
        if prev_ts is not None:
            dt = (tick.ts - prev_ts).total_seconds()
            if dt < 0:
                self.monotonic = False
                res.reason = f"时间戳回退 {dt:.1f}s"
            elif dt > self.gap_threshold:
                res.gap = True
                self.gaps += 1
                res.reason = f"断档 {dt:.1f}s > 阈值 {self.gap_threshold:.0f}s"
            self._intervals.setdefault(tick.code, []).append(abs(dt))

        # 去重：同标的同时间戳同价
        if (prev_ts is not None and tick.ts == prev_ts
                and tick.price == self._last_price.get(tick.code)):
            res.duplicate = True
            res.ok = False
            res.reason = "重复行情条目（同标的同时间戳同价）"
            self.duplicates += 1
            self.results.append(res)
            return res

        self._last[tick.code] = tick.ts
        self._last_price[tick.code] = tick.price
        self.results.append(res)
        return res

    def report(self) -> dict:
        """汇总校验报告。"""
        freq_errors: List[dict] = []
        for code, ivs in self._intervals.items():
            if not ivs:
                continue
            avg = sum(ivs) / len(ivs)
            if self.expected_interval > 0:
                err = abs(avg - self.expected_interval) / self.expected_interval
                if err > 0.10:
                    freq_errors.append({
                        "code": code, "avg_interval_sec": round(avg, 2),
                        "expected_sec": self.expected_interval,
                        "error_pct": round(err * 100, 2),
                    })
        return {
            "total": len(self.results),
            "accepted": sum(1 for r in self.results if r.ok),
            "duplicates": self.duplicates,
            "gaps": self.gaps,
            "timestamp_monotonic": self.monotonic,
            "frequency_errors": freq_errors,
            "frequency_ok": len(freq_errors) == 0,
        }
