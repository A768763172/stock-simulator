"""全模块联动性核验 · 验证单元测试。

覆盖四大联动维度与对应缺陷修复点（D1–D5）：
- 数据流转联动（行情接入→撮合定价→落盘存储→看板展示 同源/同时效/同异常处理）
- 状态联动（盘前/连续竞价/午休/收盘 + 涨跌停/停牌/正常 跨规则·撮合·看板同步）
- 调度时序联动（行情快照刷新→收盘收尾→持仓落盘→盈亏核算 按依赖串行）
- 异常容错联动（行情源中断/撮合异常/落盘失败 全链路同步降级、数据一致）

D1 (P0·状态联动): 看板 `ms` 未定义 NameError 崩溃 → 改用 phase_state 绑定
D2 (P1·异常容错/数据流转): 盘中刷新复用快照 industry，解耦基本面网络
D3 (P1·异常容错联动): 盈亏核算源与回退均失败时显式 price_ready 信号，不静默写 0
D4 (P2·调度时序): 收盘边界补一次快照刷新，先于 15:35 收盘流程串行执行
D5 (P2·数据流转): Mock 行情 open/high/low 与现价可区分，字段透传可验证
脏价二级防御: 超出涨跌停区间的异常价不落盘
"""
import os
import sys
import json
import types
import tempfile
import unittest
from datetime import date, datetime, time as dtime
from unittest.mock import MagicMock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from config.settings import SimulatorConfig, FeeConfig
from core.simulator import Simulator
from core.order import Position, OrderType, OrderStatus, Side
from core.pnl import PnLStatistics
from core.trading_rules import TradingRules
from storage.local_store import LocalStore
from market.mock_provider import MockProvider
from market.base import Quote, MarketProvider
from market.feed import detect_suspension, validate_quote
from market.display import resolve_display_price, market_phase_label

import scheduler  # noqa: E402


def _tmp() -> str:
    return tempfile.mkdtemp(prefix="link_")


def _sim(code="sh600519", price=1705.0, prev=1700.0, suspended=False,
         tmp=None, storage=None, capital=1_000_000):
    tmp = tmp or _tmp()
    mp = MockProvider()
    mp.set_price(code, price, prev)
    if suspended:
        mp.set_suspended(code, True)
    st = storage or LocalStore(tmp)
    sim = Simulator(SimulatorConfig(provider="mock", storage_dir=tmp,
                                    initial_capital=capital),
                    market=mp, storage=st)
    sim.set_day(date(2026, 9, 23))
    sim.account.positions[code] = Position(
        code=code, name=mp.get_quote(code).name, quantity=100,
        available=100, cost_price=price,
    )
    return sim, tmp, code, mp


class TestDataFlowLinkage(unittest.TestCase):
    """行情接入→撮合→落盘→看板 全链路同数据源、字段透传完整。"""

    def test_quote_match_live_display_same_source(self):
        """成交判定价与看板展示价共用同一行情源：展示价=行情价，成交价=行情价+已知滑点。"""
        sim, tmp, code, mp = _sim(price=1705.0, prev=1700.0)
        order = sim.buy(code, 100, OrderType.MARKET,
                        now=datetime(2026, 9, 23, 10, 0, 0))
        self.assertEqual(order.status, OrderStatus.FILLED)
        slip = FeeConfig().slippage_rate
        # 成交价在 Trade 上（Order 无 fill_price 字段）
        fill = sim.account.trades[-1].price
        self.assertAlmostEqual(fill, 1705.0 * (1.0 + slip), 4)
        # 落盘展示价 = 行情源价（与成交同源，非两套数据）
        data = sim.refresh_live_prices()
        self.assertAlmostEqual(data[code]["price"], 1705.0, 4)
        self.assertEqual(data["_meta"]["source"], "mock")
        # 看板解析价与落盘价一致
        d = resolve_display_price(data, code)
        self.assertEqual(d["status"], "READY")
        self.assertAlmostEqual(d["price"], 1705.0, 4)

    def test_ohlc_distinct_transmittable(self):
        """D5: open/high/low 与现价可区分，字段确有透传而非恒等于现价。"""
        for code in ("sz000001", "sh600519", "bj830799"):
            q = MockProvider().get_quote(code)
            self.assertNotEqual(q.open, q.price, f"{code} open==price")
            self.assertGreater(q.high, q.price, f"{code} high<=price")
            self.assertLess(q.low, q.price, f"{code} low>=price")
            self.assertGreaterEqual(q.high, q.open)
            self.assertLessEqual(q.low, q.open)

    def test_full_fields_landed_and_displayed(self):
        """落盘全套行情字段在看板解析层一一对应透传。"""
        sim, tmp, code, mp = _sim(price=1705.0, prev=1700.0)
        data = sim.refresh_live_prices()
        e = data[code]
        d = resolve_display_price(data, code)
        for f in ("price", "prev_close", "open", "high", "low",
                  "volume", "change_pct", "ceiling", "floor",
                  "industry", "is_suspended", "limit_hit",
                  "updated_at", "source"):
            self.assertEqual(d.get(f), e.get(f),
                             f"字段 {f} 落盘与看板解析不一致")

    def test_industry_refresh_decoupled_from_fundamentals(self):
        """D2: 盘中刷新复用快照 industry，不触达基本面网络（避免级联拖垮实时价）。"""
        sim, tmp, code, mp = _sim(price=1705.0, prev=1700.0)
        called = {"n": 0}

        def _gf(c):
            called["n"] += 1
            raise RuntimeError("no fundamentals")

        sim.market.get_fundamentals = _gf
        # 有快照 industry 兜底：不调用 get_fundamentals
        self.assertEqual(sim._industry(code, fallback={code: {"industry": "FALLBACK_IND"}}),
                         "FALLBACK_IND")
        self.assertEqual(called["n"], 0)
        # 无兜底且源异常：优雅降级为「其他」，不崩溃
        self.assertEqual(sim._industry(code), "其他")


class TestStateLinkage(unittest.TestCase):
    """盘前/连续竞价/午休/收盘 与 涨跌停/停牌/正常 跨模块同步。"""

    def test_dashboard_phase_binding_no_nameerror(self):
        """D1(P0): 看板休市分支使用 phase_state 绑定，不再引用未定义 ms（防 NameError 崩溃）。"""
        with open(os.path.join(ROOT, "dashboard.py"), encoding="utf-8") as f:
            src = f.read()
        self.assertIn('phase_state = phase["state"]', src,
                      "看板须将阶段状态绑定到 phase_state")
        self.assertIn('phase_state == "CLOSED"', src,
                      "休市视图判定须使用 phase_state")
        self.assertNotIn('ms == "CLOSED"', src,
                         "不得再引用未定义的 ms（曾导致 NameError 崩溃看板）")

    def test_dashboard_overview_threads_price_ready(self):
        """D3 联动: 概览显式透传 price_ready 信号（源不可得时联动告警，杜绝悄悄少算）。"""
        import importlib
        if "streamlit" not in sys.modules:
            sys.modules["streamlit"] = MagicMock()
        dashboard = importlib.import_module("dashboard")
        snaps_not_ready = [{"day": "2026-09-23", "total_assets": 50000.0,
                            "price_ready": False}]
        ov = dashboard.compute_overview({}, {}, snaps_not_ready, {}, {})
        self.assertFalse(ov["price_ready"])
        snaps_ready = [{"day": "2026-09-23", "total_assets": 50000.0}]
        ov2 = dashboard.compute_overview({}, {}, snaps_ready, {}, {})
        self.assertTrue(ov2["price_ready"])

    def test_suspension_state_consistent_across_modules(self):
        """停牌状态在 规则/撮合/看板 三处一致：均判停牌且禁止可交易价。"""
        sim, tmp, code, mp = _sim(suspended=True)
        # 撮合拒单
        order = sim.buy(code, 100, OrderType.MARKET,
                        now=datetime(2026, 9, 23, 10, 0, 0))
        self.assertEqual(order.status, OrderStatus.REJECTED)
        self.assertIn("停牌", order.reject_reason)
        # 落盘标停牌
        data = sim.refresh_live_prices()
        self.assertTrue(data[code]["is_suspended"])
        # 看板解析标 SUSPENDED
        d = resolve_display_price(data, code)
        self.assertEqual(d["status"], "SUSPENDED")

    def test_phase_label_covers_all_states(self):
        for dt, st in [
            (datetime(2026, 9, 23, 9, 15), "PRE"),     # 盘前/集合竞价
            (datetime(2026, 9, 23, 10, 0), "LIVE"),    # 连续竞价
            (datetime(2026, 9, 23, 12, 0), "PAUSE"),    # 午休
            (datetime(2026, 9, 26, 10, 0), "CLOSED"),   # 收盘/休市
        ]:
            self.assertEqual(market_phase_label(dt)["state"], st)


class TestSchedulingTimingLinkage(unittest.TestCase):
    """调度守护任务按依赖串行：快照刷新→收盘收尾→持仓落盘→盈亏核算。"""

    def test_scheduler_constants(self):
        self.assertEqual(scheduler.REFRESH_SEC, 3)
        self.assertEqual(scheduler.CLOSE_RUN, dtime(15, 35, 0))

    def test_end_of_day_refresh_before_close_run(self):
        """D4: 收盘边界补一次快照刷新，时序上位于盘中循环之后、15:35 收盘流程之前。"""
        with open(os.path.join(ROOT, "scheduler.py"), encoding="utf-8") as f:
            src = f.read()
        end_refresh = src.rfind("refresh_intraday(market)")
        close_run = src.rfind("run_daily(market)")
        self.assertGreater(end_refresh, 0)
        self.assertGreater(close_run, 0)
        self.assertLess(end_refresh, close_run,
                        "收盘边界快照刷新须先于收盘流程执行")
        self.assertIn('if datetime.now().time() >= CLOSE_RUN:',
                      src, "收盘流程须受 15:35 时间门控串行触发")

    def test_refresh_intraday_self_heals_on_source_failure(self):
        """异常容错联动: 刷新时源/落盘异常被捕获，主循环自愈不退出。"""
        class _BoomSim:
            def __init__(self, *a, **k):
                self.account = None

            def load(self):
                return True

            def refresh_live_prices(self):
                raise RuntimeError("source dead")

        import core.simulator as _cs
        orig = _cs.Simulator
        _cs.Simulator = _BoomSim
        try:
            fake_market = MagicMock()
            fake_market.save_cache = lambda: None
            # 不应抛出异常（异常在 refresh_intraday 内被捕获）
            scheduler.refresh_intraday(fake_market)
        finally:
            _cs.Simulator = orig


class TestExceptionToleranceLinkage(unittest.TestCase):
    """行情源中断/撮合异常/落盘失败 全链路同步降级、数据一致。"""

    def _boom_market(self, sim):
        def _boom(code):
            raise RuntimeError("quote boom")
        sim.market.get_quote = _boom
        return sim

    def test_pnl_signals_unavailable_no_silent_zero(self):
        """D3: 源与回退均失败时，price_ready=False（不静默写 0 制造数据割裂）。"""
        sim, tmp, code, mp = _sim(price=1705.0, prev=1700.0)
        self._boom_market(sim)
        snap = sim.pnl.snapshot()
        self.assertFalse(snap["price_ready"])
        self.assertIn(code, snap["price_unavailable"])
        details = sim.pnl.position_details()
        self.assertTrue(details)
        self.assertFalse(details[0]["price_ready"])

    def test_pnl_falls_back_to_live_snapshot(self):
        """异常容错联动: 源失败时复用实时价快照兜底，不写成 0。"""
        sim, tmp, code, mp = _sim(price=1705.0, prev=1700.0)
        sim.refresh_live_prices()  # 先写入有效快照
        stored = sim.refresh_live_prices()[code]["price"]
        self._boom_market(sim)
        prices = sim.pnl._current_prices()
        self.assertAlmostEqual(prices[code], stored, 4)
        self.assertTrue(sim.pnl.snapshot()["price_ready"])

    def test_dirty_price_out_of_limit_not_landed(self):
        """脏价二级防御: 超出涨跌停区间的异常价不落盘（源/看板间不扩散脏数据）。"""
        class _DirtyMarket(MockProvider):
            def get_quote(self, code):
                return Quote(code=code, name="X", price=99999.0,
                             prev_close=100.0, open=100.0, high=100.0,
                             low=100.0, ceiling=110.0, floor=90.0,
                             time="09:30", volume=1.0, change_pct=0.0,
                             suspended=False)

        tmp = _tmp()
        st = LocalStore(tmp)
        sim = Simulator(SimulatorConfig(provider="mock", storage_dir=tmp),
                        market=_DirtyMarket(), storage=st)
        sim.set_day(date(2026, 9, 23))
        sim.account.positions["X"] = Position(
            code="X", name="X", quantity=100, available=100, cost_price=100.0)
        data = sim.refresh_live_prices()
        self.assertNotIn("X", data, "超出涨跌停区间的脏价不得落盘")
        self.assertEqual(data["_meta"]["positions"], 0)

    def test_nonpositive_price_guard(self):
        """validate_quote 拦截非正/NaN 价，脏价不进入链路。"""
        self.assertFalse(validate_quote(-1.0, 100.0, 110.0, 90.0)[0])
        self.assertFalse(validate_quote(float("nan"), 100.0, 110.0, 90.0)[0])

    def test_matching_rejects_on_quote_failure(self):
        """撮合异常联动: 行情获取失败直接拒单，不进入脏撮合。"""
        sim, tmp, code, mp = _sim(price=1705.0, prev=1700.0)
        self._boom_market(sim)
        order = sim.buy(code, 100, OrderType.MARKET,
                        now=datetime(2026, 9, 23, 10, 0, 0))
        self.assertEqual(order.status, OrderStatus.REJECTED)
        self.assertIn("行情获取失败", order.reject_reason)


if __name__ == "__main__":
    unittest.main(verbosity=2)
