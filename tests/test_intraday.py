"""盘中实时决策回归测试：高频退出监控 + 低频完整决策 + 选股并行预热。

运行：python tests/test_intraday.py
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import date, datetime

from config.strategy import StrategyConfig
from config.settings import SimulatorConfig
from core.simulator import Simulator
from core.order import Side
from market.mock_provider import MockProvider
from storage.local_store import LocalStore
from strategy.agent import StrategyAgent
from strategy.screener import Screener
from strategy.risk import RiskMode


NOW = datetime(2026, 9, 21, 10, 0, 0)   # 交易时段内的固定时刻（避免非交易时段拒单）


class TestIntradayMonitor(unittest.TestCase):
    def setUp(self):
        self.m = MockProvider()
        self.tmp = tempfile.TemporaryDirectory()
        self.sim = Simulator(SimulatorConfig(provider="mock"), market=self.m,
                             storage=LocalStore(self.tmp.name))
        self.sim.account.initial_capital = 50000.0
        self.sim.account.cash = 50000.0
        self.sim.set_day(date(2026, 9, 21))
        self.agent = StrategyAgent(self.sim, StrategyConfig(),
                                   storage=self.sim.storage)

    def _seed(self, code, cost, qty=200):
        self.sim.account.ensure_position(code, code)
        pos = self.sim.account.get_position(code)
        pos.quantity = qty
        pos.available = qty
        pos.cost_price = cost
        pos.frozen = 0
        self.agent.pos_state[code] = {
            "entry_price": cost, "entry_factors": {}, "entry_env": None,
            "target_shares": qty, "batch_step": 1, "last_buy_price": cost,
            "tp1_done": False, "holding_days": 1,
        }

    def test_stop_loss_triggers_sell(self):
        self._seed("sh600519", 100.0, 200)
        # 实时价较成本下跌 5% → 触发止损（>3%）
        n = self.agent.intraday_monitor(NOW, {"sh600519": 95.0})
        self.assertEqual(n, 1)
        self.assertNotIn("sh600519", self.agent.pos_state)
        self.assertEqual(len(self.sim.account.trades), 1)
        self.assertEqual(self.sim.account.trades[0].side, Side.SELL)

    def test_take_profit_half_sells_half(self):
        self._seed("sh600519", 100.0, 200)
        # 盈利 10%（>=tp1 8% 且 < tp2 15%）→ 止盈半仓
        n = self.agent.intraday_monitor(NOW, {"sh600519": 110.0})
        self.assertEqual(n, 1)
        self.assertIn("sh600519", self.agent.pos_state)
        self.assertEqual(self.sim.account.get_position("sh600519").quantity, 100)
        self.assertTrue(self.agent.pos_state["sh600519"]["tp1_done"])

    def test_hold_timeout_exits(self):
        self._seed("sh600519", 100.0, 200)
        self.agent.pos_state["sh600519"]["holding_days"] = self.agent.cfg.hold_max_days
        n = self.agent.intraday_monitor(NOW, {"sh600519": 100.0})
        self.assertEqual(n, 1)
        self.assertNotIn("sh600519", self.agent.pos_state)

    def test_no_exit_when_price_ok(self):
        self._seed("sh600519", 100.0, 200)
        # 盈利 1%，未到任何退出阈值
        n = self.agent.intraday_monitor(NOW, {"sh600519": 101.0})
        self.assertEqual(n, 0)
        self.assertIn("sh600519", self.agent.pos_state)

    def test_monitor_pure_local_no_network(self):
        # 不传 live_prices 时退回 self._prices()（行情源）；给定价时完全本地，不触网判定
        self._seed("sh600519", 100.0, 200)
        n = self.agent.intraday_monitor(NOW, {"sh600519": 95.0})
        self.assertEqual(n, 1)


class TestIntradayStep(unittest.TestCase):
    def setUp(self):
        self.m = MockProvider()
        self.tmp = tempfile.TemporaryDirectory()
        self.sim = Simulator(SimulatorConfig(provider="mock"), market=self.m,
                             storage=LocalStore(self.tmp.name))
        self.sim.account.initial_capital = 50000.0
        self.sim.account.cash = 50000.0
        self.sim.set_day(date(2026, 9, 21))
        self.agent = StrategyAgent(self.sim, StrategyConfig(),
                                   storage=self.sim.storage)

    def _seed(self, code, cost, qty=200):
        self.sim.account.ensure_position(code, code)
        pos = self.sim.account.get_position(code)
        pos.quantity = qty
        pos.available = qty
        pos.cost_price = cost
        pos.frozen = 0
        self.agent.pos_state[code] = {
            "entry_price": cost, "entry_factors": {}, "entry_env": None,
            "target_shares": qty, "batch_step": 1, "last_buy_price": cost,
            "tp1_done": False, "holding_days": 3,
        }

    def test_does_not_increment_holding_days(self):
        # 关键回归：盘中多 tick 不能重复累计 holding_days（否则 HOLD_TIMEOUT 立即触发）
        self._seed("sh600519", 100.0, 200)
        self.agent.pos_state["sh600519"]["holding_days"] = 3
        # 中性价隔离：使退出/加仓逻辑不触发，专测 holding_days 不被 intraday_step 累加
        self.agent._prices = lambda: {c: st["entry_price"]
                                      for c, st in self.agent.pos_state.items()}
        self.agent.intraday_step(date(2026, 9, 21), NOW)
        self.assertEqual(self.agent.pos_state["sh600519"]["holding_days"], 3)

    def test_does_not_reset_daily_traded_same_day(self):
        # 关键回归：当日已交易集合跨盘中调用保留，防信号闪烁重复开仓
        self.agent._daily_traded_day = date(2026, 9, 21).isoformat()
        self.agent._daily_traded = {"sh600519"}
        # 中性价隔离，避免退出逻辑改变 pos_state/_daily_traded 造成误判
        self.agent._prices = lambda: {c: st["entry_price"]
                                      for c, st in self.agent.pos_state.items()}
        self.agent.intraday_step(date(2026, 9, 21), NOW)
        self.assertEqual(self.agent._daily_traded_day, date(2026, 9, 21).isoformat())
        self.assertIn("sh600519", self.agent._daily_traded)

    def test_defensive_mode_blocks_open(self):
        # 制造回撤 → 全面防守；intraday_step 不应开新仓
        self.agent.risk.peak_equity = 100000.0
        self.agent.risk.prev_day_equity = 100000.0
        self.agent.risk.week_start_equity = 100000.0
        self.agent.risk.update(90000.0, date(2026, 9, 21))  # peak dd 10% -> DEFENSE_PEAK
        st = self.agent.intraday_step(date(2026, 9, 21), NOW)
        self.assertEqual(st["mode"], "DEFENSE_PEAK")
        self.assertEqual(len(self.sim.account.positions), 0)

    def test_returns_status_dict(self):
        st = self.agent.intraday_step(date(2026, 9, 21), NOW)
        self.assertIn("mode", st)
        self.assertIn("equity", st)


class TestScreenerParallelWarm(unittest.TestCase):
    def setUp(self):
        self.m = MockProvider()
        self.calls = []

        def warm(codes, max_workers=10, refresh_fund=False, skip_quote=False,
                 **kwargs):
            self.calls.append({"codes": list(codes), "skip_quote": skip_quote,
                               "kwargs": kwargs})
            return len(codes)

        self.m.warm_universe = warm
        self.screener = Screener(StrategyConfig(), self.m)

    def test_build_invokes_warm_universe(self):
        # build() 应并行预热 passing 标的缓存（westock 路径），消除串行冷启动 CLI
        self.screener.build()
        self.assertTrue(self.calls, "build() 应调用 warm_universe 并行预热")

    def test_build_skips_full_market_quote_by_default(self):
        # 回归：盘中/选股预热绝不能逐只重抓全市场 quote（曾卡主循环 18 分钟）
        self.screener.build()
        self.assertTrue(self.calls)
        self.assertTrue(all(c["skip_quote"] for c in self.calls),
                        "选股预热必须 skip_quote=True，不抓实时价")

    def test_build_can_opt_in_quote_when_explicitly_asked(self):
        # 显式要求时仍可抓（保留能力，供未来需要收盘价的路径使用）
        self.screener.build(warm_skip_quote=False)
        self.assertTrue(any(not c["skip_quote"] for c in self.calls))


class TestRefreshQuotesFastMode(unittest.TestCase):
    """盘中成交价刷新：必须抓真实 quote，但受快模式 + 总预算约束。"""

    def test_refresh_quotes_uses_bounded_fast_mode(self):
        calls = []

        def warm(codes, **kwargs):
            calls.append({"codes": list(codes), "kwargs": kwargs})
            return len(codes)

        m = MockProvider()
        m.warm_universe = warm
        with tempfile.TemporaryDirectory() as td:
            sim = Simulator(SimulatorConfig(provider="mock"), market=m,
                            storage=LocalStore(td))
            agent = StrategyAgent(sim, StrategyConfig())
            agent._refresh_quotes(["sh600000", "sh600001"])
        self.assertEqual(len(calls), 1)
        kw = calls[0]["kwargs"]
        self.assertFalse(kw.get("refresh_fund"))
        self.assertLessEqual(kw.get("cli_timeout", 99), 20)
        self.assertGreaterEqual(kw.get("max_seconds", 0), 1)
        self.assertLessEqual(kw.get("max_seconds", 999), 120)

    def test_refresh_quotes_empty_is_noop(self):
        m = MockProvider()
        calls = []
        m.warm_universe = lambda codes, **kw: calls.append(list(codes))
        with tempfile.TemporaryDirectory() as td:
            sim = Simulator(SimulatorConfig(provider="mock"), market=m,
                            storage=LocalStore(td))
            agent = StrategyAgent(sim, StrategyConfig())
            agent._refresh_quotes([])
        self.assertFalse(calls)


class TestWarmUniverseDeadline(unittest.TestCase):
    """warm_universe 的墙钟总预算：超时不得让调用方被长时间独占。"""

    def test_deadline_bounds_total_wait(self):
        import time as _t
        from market.westock_provider import WeStockProvider

        with tempfile.TemporaryDirectory() as td:
            p = WeStockProvider(cli="nonexistent", cache_dir=td)
            # nonexistent CLI + skip_quote=True → 无网络调用，应在预算内快速返回。
            # 主要验证 max_seconds 参数被接受且 wait() 语义正常（不为负/不误超时）。
            t0 = _t.monotonic()
            n = p.warm_universe(["sh600000"], max_workers=2, skip_quote=True,
                                max_seconds=5)
            cost = _t.monotonic() - t0
            self.assertLess(cost, 5)
            self.assertGreaterEqual(n, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
