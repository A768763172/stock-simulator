"""策略层单元测试：选股池 / 多因子 / 仓位 / 风控 / 智能体调度。

运行：python tests/test_strategy.py
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import date, datetime

from config.strategy import StrategyConfig
from market.mock_provider import MockProvider
from strategy.stock_pool import StockPool
from strategy.factors import (quality_score, valuation_score, low_vol_score,
                               momentum_score, annualized_vol, max_drawdown,
                               compute_factor_scores)
from strategy.position import PositionPlanner
from strategy.risk import RiskController, RiskMode
from config.settings import SimulatorConfig
from core.simulator import Simulator
from strategy.agent import StrategyAgent


class TestStockPool(unittest.TestCase):
    def setUp(self):
        self.m = MockProvider()
        self.pool = StockPool(StrategyConfig(), self.m)

    def test_board_exclusion(self):
        for code in ["sh688981", "sz300750", "bj830799"]:
            r = self.pool.screen_code(code)
            self.assertFalse(r["passed"], code)
            self.assertIn("非沪深主板", r["reasons"][0])

    def test_st_exclusion(self):
        r = self.pool.screen_code("sh600868")
        self.assertFalse(r["passed"])
        self.assertTrue(any("ST" in x for x in r["reasons"]))

    def test_loss_exclusion(self):
        r = self.pool.screen_code("sz000725")
        self.assertFalse(r["passed"])
        self.assertTrue(any("亏损" in x for x in r["reasons"]))

    def test_pledge_exclusion(self):
        r = self.pool.screen_code("sh600516")
        self.assertFalse(r["passed"])
        self.assertTrue(any("质押" in x for x in r["reasons"]))

    def test_low_turnover_exclusion(self):
        r = self.pool.screen_code("sh601988")
        self.assertFalse(r["passed"])
        self.assertTrue(any("成交额" in x for x in r["reasons"]))

    def test_main_board_passes(self):
        r = self.pool.screen_code("sh600519")
        self.assertTrue(r["passed"])


class TestETF(unittest.TestCase):
    """ETF 可选池：纳入种子 ETF、过池仅做流动性过滤、因子中性打分、可关闭。"""
    def setUp(self):
        self.m = MockProvider()

    def test_etf_in_universe(self):
        pool = StockPool(StrategyConfig(), self.m)
        uni = pool.candidate_universe()
        for etf in StrategyConfig().etf_seeds:
            self.assertIn(etf, uni)

    def test_etf_passes_pool(self):
        pool = StockPool(StrategyConfig(), self.m)
        r = pool.screen_code("sh510300")
        self.assertTrue(r["passed"], r.get("reasons"))
        self.assertEqual(r.get("fundamentals").industry, "ETF")

    def test_etf_skips_company_fundamentals(self):
        # 即便行情源给 ETF 返回了"亏损/高质押"等公司字段，也不应因此被剔除
        pool = StockPool(StrategyConfig(), self.m)
        r = pool.screen_code("sh510880")
        self.assertTrue(r["passed"], r.get("reasons"))

    def test_etf_factor_score_neutral(self):
        fs = compute_factor_scores("sh510300", self.m, StrategyConfig())
        self.assertEqual(fs["quality"], 0.5)
        self.assertEqual(fs["valuation"], 0.5)
        self.assertGreaterEqual(fs["low_vol"], 0.0)
        self.assertGreaterEqual(fs["momentum"], 0.0)
        # 综合分应高于纯公司的 0 质量/估值底线
        self.assertGreater(fs["score"], 0.0)

    def test_include_etfs_false_excludes(self):
        cfg = StrategyConfig(include_etfs=False)
        pool = StockPool(cfg, self.m)
        self.assertFalse(pool.is_etf("sh510300"))
        uni = pool.candidate_universe()
        for etf in StrategyConfig().etf_seeds:
            self.assertNotIn(etf, uni)


class TestFactors(unittest.TestCase):
    def setUp(self):
        self.cfg = StrategyConfig()
        self.m = MockProvider()

    def test_weight_sum(self):
        self.assertAlmostEqual(sum(self.cfg.factor_weights.values()), 1.0, places=6)

    def test_quality_full(self):
        from market.base import Fundamentals
        f = Fundamentals(code="x", name="x", roe_3y=[0.15, 0.14, 0.13],
                         ocf=120.0, net_profit=100.0, net_profit_ttm=100.0,
                         gross_margin_series=[0.4, 0.41, 0.39])
        self.assertAlmostEqual(quality_score(f, self.cfg), 1.0, places=6)

    def test_quality_zero_on_loss_roe(self):
        from market.base import Fundamentals
        f = Fundamentals(code="x", name="x", roe_3y=[0.05, 0.04, 0.03],
                         ocf=10.0, net_profit=100.0, net_profit_ttm=100.0,
                         gross_margin_series=[0.4, 0.5, 0.2])
        self.assertAlmostEqual(quality_score(f, self.cfg), 0.0, places=6)

    def test_valuation_full_when_low(self):
        from market.base import Fundamentals
        f = Fundamentals(code="x", name="x", pe_pct_3y=0.10, pb_pct_3y=0.20)
        self.assertAlmostEqual(valuation_score(f, self.cfg), 1.0, places=6)

    def test_low_vol_drawdown_flag(self):
        # 波动率为0且回撤小 → 满分
        self.assertAlmostEqual(low_vol_score(0.0, 0.5, 0.01, self.cfg), 1.0, places=6)
        # 低波动但回撤<15% → 0.7*0.8 + 0.3 = 0.86
        self.assertAlmostEqual(low_vol_score(0.1, 0.5, 0.01, self.cfg), 0.86, places=6)
        # 回撤>15% 时 drawdown 子项归零 → 0.7*0.8 = 0.56
        s = low_vol_score(0.1, 0.5, 0.20, self.cfg)
        self.assertAlmostEqual(s, 0.56, places=6)

    def test_momentum_no_chase(self):
        self.assertAlmostEqual(momentum_score(0.20, 0.0, self.cfg), 0.0, places=6)
        self.assertGreater(momentum_score(0.05, 0.0, self.cfg), 0.0)

    def test_vol_and_drawdown(self):
        closes = [100 * (1 + 0.02 * i) for i in range(20)]
        self.assertGreater(annualized_vol(closes), 0)
        self.assertAlmostEqual(max_drawdown(closes), 0.0, places=6)


class TestPosition(unittest.TestCase):
    def setUp(self):
        self.cfg = StrategyConfig()
        self.p = PositionPlanner(self.cfg)

    def test_to_lot(self):
        self.assertEqual(self.p.to_lot(150), 100)
        self.assertEqual(self.p.to_lot(200), 200)

    def test_target_shares(self):
        # 1亿 * 5% = 500万；价100 -> 5万股
        self.assertEqual(self.p.target_shares(100_000_000, 0.05, 100.0), 50_000)

    def test_batch_plan(self):
        self.assertEqual(self.p.batch_shares(100_000, 1), 40_000)  # 40%
        self.assertEqual(self.p.batch_shares(100_000, 2), 30_000)  # 30%
        self.assertEqual(self.p.batch_shares(100_000, 3), 30_000)  # 30%

    def test_pullback_threshold(self):
        self.assertAlmostEqual(self.p.pullback_threshold(100.0, 2), 98.0, places=3)
        self.assertAlmostEqual(self.p.pullback_threshold(100.0, 3), 96.0, places=3)


class TestRisk(unittest.TestCase):
    def setUp(self):
        self.cfg = StrategyConfig()
        self.m = MockProvider()
        self.rc = RiskController(self.cfg, self.m)

    def test_peak_defense(self):
        self.rc.update(100_000_000, date(2026, 9, 21))
        mode = self.rc.update(94_000_000, date(2026, 9, 22))  # 回撤6%>5%
        self.assertEqual(mode, RiskMode.DEFENSE_PEAK)
        self.assertAlmostEqual(self.rc.position_cap(), self.cfg.dd_peak_pos_cap)
        self.assertFalse(self.rc.allow_open(date(2026, 9, 22)))

    def test_weekly_defense(self):
        self.rc.update(100_000_000, date(2026, 9, 21))
        mode = self.rc.update(96_000_000, date(2026, 9, 22))  # 回撤4%>3%
        self.assertEqual(mode, RiskMode.DEFENSE_WEEKLY)
        self.assertAlmostEqual(self.rc.position_cap(), self.cfg.dd_weekly_pos_cap)
        self.assertTrue(self.rc.sell_only())

    def test_daily_defense_and_pause(self):
        self.rc.update(100_000_000, date(2026, 9, 21))
        d2 = date(2026, 9, 22)
        mode = self.rc.update(98_000_000, d2)  # 回撤2%>1.5%
        self.assertEqual(mode, RiskMode.DEFENSE_DAILY)
        self.assertAlmostEqual(self.rc.position_cap(), self.cfg.dd_daily_reduce_to)
        # 次日暂停开仓
        d3 = self.m.next_trading_day(d2)
        self.assertFalse(self.rc.allow_open(d3))
        # 再下一日恢复
        d4 = self.m.next_trading_day(d3)
        self.assertTrue(self.rc.allow_open(d4))

    def test_normal(self):
        self.rc.update(100_000_000, date(2026, 9, 21))
        mode = self.rc.update(100_500_000, date(2026, 9, 22))
        self.assertEqual(mode, RiskMode.NORMAL)
        self.assertTrue(self.rc.allow_open(date(2026, 9, 22)))


class TestAgent(unittest.TestCase):
    def setUp(self):
        self.m = MockProvider()
        # 必须用临时目录：daily_step/daily_step→save() 会落盘，
        # 默认 storage_dir="data" 会污染生产数据（历史事故：演示成交混入真实流水）。
        self.tmp = tempfile.mkdtemp(prefix="strat_agent_")
        self.sim = Simulator(SimulatorConfig(provider="mock",
                                             storage_dir=self.tmp),
                             market=self.m)
        self.agent = StrategyAgent(self.sim, StrategyConfig(),
                                   storage=self.sim.storage)

    def _run(self, n):
        day = date(2026, 9, 21)
        for i in range(n):
            if i > 0:
                day = self.sim.market.next_trading_day(day)
            self.agent.daily_step(day)

    def test_screen_produces_candidates(self):
        rep = self.agent.screen_report()
        self.assertGreater(rep["count"], 0)
        for c in rep["candidates"]:
            self.assertLessEqual(c["target_weight"], self.agent.cfg.single_stock_max + 1e-9)

    def test_opens_and_respects_caps(self):
        self._run(20)
        snap = self.sim.snapshot()
        total = snap["total_assets"]
        # 有效总仓上限：自适应模式下为风控应急上限（NORMAL=1.0）
        ceiling = (self.agent.cfg.total_pos_max
                   if self.agent.cfg.total_pos_max is not None else 1.0)
        self.assertLessEqual(snap["market_value"] / total, ceiling + 1e-9)
        # 现金下限：自适应时由目标仓位反推，否则取硬性 min_cash_reserve
        floor = max(self.agent.cfg.min_cash_reserve,
                    1.0 - self.agent.cfg.base_total_target)
        self.assertGreaterEqual(self.sim.account.cash, floor * total - 1.0)
        # 单股≤单股上限
        prices = {c: self.m.get_quote(c).price for c in self.sim.account.positions}
        for c, pos in self.sim.account.positions.items():
            w = pos.market_value(prices[c]) / total
            self.assertLessEqual(w, self.agent.cfg.single_stock_max + 1e-9)

    def test_persistence_roundtrip(self):
        self._run(10)
        self.agent.save()
        # 新建 agent 从本地恢复（同临时目录，不触碰生产 data/）
        sim2 = Simulator(SimulatorConfig(provider="mock",
                                         storage_dir=self.tmp),
                         market=self.m)
        sim2.load()
        agent2 = StrategyAgent(sim2, StrategyConfig(), storage=sim2.storage)
        self.assertEqual(len(agent2.pos_state), len(self.agent.pos_state))


if __name__ == "__main__":
    unittest.main(verbosity=2)
