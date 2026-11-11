"""自学习进化引擎单元测试（纯函数 + 集成）。"""
import os
import sys
import tempfile
import unittest
from datetime import date, datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.settings import SimulatorConfig
from config.strategy import StrategyConfig
from core.simulator import Simulator
from strategy.agent import StrategyAgent
from strategy.learner import (
    Learner, attribute_trade, simulate_backtest, detect_regime,
    STOP_LOSS, TAKE_PROFIT, HOLD_TIMEOUT, RISK_REDUCE,
)
from strategy.metrics import (
    sharpe_ratio, max_drawdown, win_rate, cumulative_return, rank_key,
)


class TestMetrics(unittest.TestCase):
    def test_sharpe_zero_when_flat(self):
        self.assertEqual(sharpe_ratio([1.0, 1.0, 1.0]), 0.0)

    def test_max_drawdown(self):
        self.assertAlmostEqual(max_drawdown([1.0, 1.2, 0.9, 1.1]), (1.2 - 0.9) / 1.2, 6)

    def test_win_rate(self):
        self.assertAlmostEqual(win_rate([0.1, -0.2, 0.05]), 2 / 3, 6)

    def test_cumulative_return(self):
        self.assertAlmostEqual(cumulative_return([1.0, 1.1], 1.0), 0.1, 6)

    def test_rank_key_prefers_sharpe_then_drawdown(self):
        # 高夏普、高回撤 应优于 低夏普、低回撤
        a = rank_key(2.0, 0.20, 0.5, 0.1)
        b = rank_key(1.0, 0.05, 0.5, 0.1)
        self.assertGreater(a, b)
        # 同夏普下，回撤更小者更优
        c = rank_key(1.5, 0.10, 0.5, 0.1)
        d = rank_key(1.5, 0.30, 0.5, 0.1)
        self.assertGreater(c, d)


class TestAttribution(unittest.TestCase):
    def test_stop_loss_in_down_market(self):
        r = attribute_trade(-0.05, -0.03, STOP_LOSS, {"quality": 0.2})
        self.assertEqual(r["primary"], "止损触发")
        self.assertGreater(r["breakdown"]["systematic"], 0.0)

    def test_selection_error_high_quality_loss(self):
        r = attribute_trade(-0.06, 0.0, STOP_LOSS, {"quality": 0.9})
        self.assertEqual(r["primary"], "选股错误")
        self.assertGreaterEqual(r["breakdown"]["selection"], 0.45)

    def test_take_profit(self):
        r = attribute_trade(0.12, 0.01, TAKE_PROFIT, {"quality": 0.8})
        self.assertEqual(r["primary"], "止盈触发")
        self.assertGreater(r["breakdown"]["rule_trigger"], 0.0)

    def test_hold_timeout_in_down_market(self):
        r = attribute_trade(-0.02, -0.04, HOLD_TIMEOUT, {"quality": 0.3})
        self.assertEqual(r["primary"], "系统性风险")

    def test_risk_reduce(self):
        r = attribute_trade(-0.01, 0.02, RISK_REDUCE, {"quality": 0.4})
        self.assertEqual(r["primary"], "风控降仓")


class TestBacktest(unittest.TestCase):
    def test_simulate_triggers_stop(self):
        # 单调下跌行情应触发止损并录得负收益
        closes = [100, 98, 95, 90, 85]
        curve, trades = simulate_backtest(closes, 0.03, 0.08, 0.15)
        self.assertTrue(len(curve) >= 2)
        self.assertTrue(any(t < 0 for t in trades))

    def test_simulate_take_profit(self):
        closes = [100, 105, 112, 110, 108]
        curve, trades = simulate_backtest(closes, 0.03, 0.08, 0.15)
        self.assertTrue(any(t > 0 for t in trades))


class TestRegime(unittest.TestCase):
    def test_detect_regime_returns_valid(self):
        sim = Simulator(SimulatorConfig(provider="mock"))
        cfg = StrategyConfig()
        self.assertIn(detect_regime(sim.market, cfg),
                      ("BULL", "OSCILLATION", "BEAR"))

    def test_no_false_bear_on_minor_dip(self):
        """长横盘高位后小幅回踩均线，不应误判 BEAR（此前用 OR 条件会误判）。"""
        cfg = StrategyConfig()
        # 55 根 100 + 末 5 根微跌：末值 98 略低于 60 日均线(~99.95)，
        # 但近 5 日仅 -2%（非明显走弱）。旧逻辑 price<ma → 误判 BEAR。
        closes = [100.0] * 55 + [100.0, 100.0, 99.0, 100.0, 98.0]
        self.assertEqual(len(closes), 60)
        class _M:
            def get_close_history(self, idx, window):
                return closes[-window:] if len(closes) >= window else closes
        self.assertEqual(detect_regime(_M(), cfg), "OSCILLATION")

    def test_clear_bull_and_bear(self):
        cfg = StrategyConfig()
        bull = list(range(80, 110)) + [108, 110, 112, 114, 116]  # 近 5 日走强且站上均线
        class _M:
            def get_close_history(self, idx, window):
                return bull[-window:] if len(bull) >= window else bull
        self.assertEqual(detect_regime(_M(), cfg), "BULL")
        bear = list(range(120, 90, -1)) + [92, 90, 88, 86, 84]  # 近 5 日走弱且跌破均线
        class _M2:
            def get_close_history(self, idx, window):
                return bear[-window:] if len(bear) >= window else bear
        self.assertEqual(detect_regime(_M2(), cfg), "BEAR")


class TestStrategyEffLearning(unittest.TestCase):
    """strategy_eff 此前从未被累计，导致策略权重只会盲从环境标签。修复后须能累计。"""

    def _make(self):
        self._tmp = tempfile.mkdtemp(prefix="sim_seff_")
        sim = Simulator(SimulatorConfig(provider="mock", storage_dir=self._tmp))
        agent = StrategyAgent(sim, StrategyConfig(), storage=sim.storage)
        learner = Learner(agent, storage=sim.storage)
        agent.attach_learner(learner)
        return sim, agent, learner

    def test_accumulate_effect_distributes_by_entry_weights(self):
        _, _, learner = self._make()
        e = {"pnl_pct": 0.10,
             "entry_factors": {"momentum": 0.5, "quality": 0.0,
                               "valuation": 0.0, "low_vol": 0.0},
             "entry_strategy_weights": {"trend_follow": 0.5,
                                        "reversal": 0.25, "grid": 0.25}}
        learner._accumulate_effect(
            e, {"price": 1.0, "policy": 0.0, "news": 0.0, "sentiment": 0.0})
        # 按开仓策略权重把 0.10 分摊：trend 0.05 / reversal 0.025 / grid 0.025
        self.assertAlmostEqual(learner.strategy_eff["trend_follow"], 0.05, 6)
        self.assertAlmostEqual(learner.strategy_eff["reversal"], 0.025, 6)
        self.assertAlmostEqual(learner.strategy_eff["grid"], 0.025, 6)
        self.assertAlmostEqual(learner.factor_eff["momentum"], 0.05, 6)
        # 总分摊 == 单笔盈亏
        self.assertAlmostEqual(sum(learner.strategy_eff.values()), 0.10, 6)

    def test_review_accumulates_strategy_eff(self):
        sim, agent, learner = self._make()
        day = date(2026, 9, 21)
        agent.exit_log = [{
            "eid": "s1", "code": "sh600519", "reason": TAKE_PROFIT, "qty": 100,
            "price": 75.0, "day": day.isoformat(), "pnl_pct": 0.10,
            "entry_factors": {"quality": 0.7, "valuation": 0.6,
                              "low_vol": 0.7, "momentum": 0.5},
            "entry_strategy_weights": {"trend_follow": 0.5,
                                       "reversal": 0.25, "grid": 0.25},
        }]
        learner.review(day)
        self.assertGreater(learner.strategy_eff["trend_follow"], 0.0)
        self.assertAlmostEqual(sum(learner.strategy_eff.values()), 0.10, 6)

    def test_switch_strategy_does_not_pump_losing_pref(self):
        """偏好策略历史亏损时，环境标签分支不应再盲目加仓（修复前会推到 0.777）。"""
        import types as _t
        _, _, learner = self._make()
        learner.sensor.strategy_env_tag = lambda env: "policy_bull"  # 偏好 trend_follow 0.70
        learner.strategy_eff = {"trend_follow": -0.5, "reversal": 0.1, "grid": 0.05}
        before = dict(learner.strategy_weights)
        learner._switch_strategy(_t.SimpleNamespace(day="2026-11-10"))
        after = learner.strategy_weights
        # trend_follow 历史亏损 -> gate=0 -> 不被加仓
        self.assertLessEqual(after["trend_follow"], before["trend_follow"] + 1e-9)
        self.assertAlmostEqual(sum(after.values()), 1.0, 6)

    def test_switch_strategy_pumps_winning_pref(self):
        import types as _t
        _, _, learner = self._make()
        learner.sensor.strategy_env_tag = lambda env: "policy_bull"
        learner.strategy_eff = {"trend_follow": 0.5, "reversal": 0.0, "grid": 0.0}
        before = dict(learner.strategy_weights)
        learner._switch_strategy(_t.SimpleNamespace(day="2026-11-10"))
        after = learner.strategy_weights
        self.assertGreater(after["trend_follow"], before["trend_follow"])
        self.assertAlmostEqual(sum(after.values()), 1.0, 6)


class TestLearnerIntegration(unittest.TestCase):
    def _make(self):
        # 隔离存储，避免 data/ 残留状态污染（base_weights 等会跨测试加载）
        self._tmp = tempfile.mkdtemp(prefix="sim_learner_")
        sim = Simulator(SimulatorConfig(provider="mock", storage_dir=self._tmp))
        agent = StrategyAgent(sim, StrategyConfig(), storage=sim.storage)
        learner = Learner(agent, storage=sim.storage)
        agent.attach_learner(learner)
        return sim, agent, learner

    def test_review_updates_factor_weights_and_persists(self):
        sim, agent, learner = self._make()
        day = date(2026, 9, 21)
        # 预置两笔当日退出：一亏（高质量因子）一盈
        agent.exit_log = [
            {"eid": "x1", "code": "sh600519", "reason": STOP_LOSS, "qty": 100,
             "price": 1600.0, "day": day.isoformat(), "pnl_pct": -0.06,
             "entry_factors": {"quality": 0.9, "valuation": 0.5,
                               "low_vol": 0.6, "momentum": 0.3}},
            {"eid": "x2", "code": "sz000333", "reason": TAKE_PROFIT, "qty": 100,
             "price": 75.0, "day": day.isoformat(), "pnl_pct": 0.10,
             "entry_factors": {"quality": 0.7, "valuation": 0.6,
                               "low_vol": 0.7, "momentum": 0.5}},
        ]
        rep = learner.review(day)
        self.assertEqual(len(rep["progress"]["factor_weights"]), 4)
        wsum = sum(rep["progress"]["factor_weights"].values())
        self.assertAlmostEqual(wsum, 1.0, 3)
        # 自适应模式不设上下限：权重仅保证非负且∈[0,1]、归一化（和为1）
        for v in rep["progress"]["factor_weights"].values():
            self.assertGreaterEqual(v, 0.0)
            self.assertLessEqual(v, 1.0)
        # 进度与持久化
        self.assertEqual(len(learner.progress), 1)
        self.assertTrue(os.path.exists(os.path.join(self._tmp, "learned_params.json")))
        self.assertTrue(os.path.exists(os.path.join(self._tmp, "learning_progress.jsonl")))

    def test_idempotent_on_rerun_same_day(self):
        sim, agent, learner = self._make()
        day = date(2026, 9, 21)
        agent.exit_log = [{"eid": "y1", "code": "sh600519", "reason": TAKE_PROFIT,
                           "qty": 100, "price": 75.0, "day": day.isoformat(),
                           "pnl_pct": 0.08,
                           "entry_factors": {"quality": 0.7, "valuation": 0.6,
                                             "low_vol": 0.7, "momentum": 0.5}}]
        learner.review(day)
        eff_before = dict(learner.factor_eff)
        learner.review(day)  # 同日重跑，processed 应阻止重复累计
        self.assertEqual(learner.factor_eff, eff_before)
        self.assertEqual(len(learner.progress), 1)

    def test_backtest_runs_every_5_days(self):
        sim, agent, learner = self._make()
        day = date(2026, 9, 21)
        for i in range(6):
            d = day if i == 0 else sim.market.next_trading_day(d)
            rep = learner.review(d)
            if learner.trading_day_count % 5 == 0:
                self.assertIsNotNone(rep["backtest"])
            else:
                self.assertIsNone(rep["backtest"])

    def test_export_chart(self):
        sim, agent, learner = self._make()
        day = date(2026, 9, 21)
        agent.exit_log = [{"eid": "z1", "code": "sh600519", "reason": TAKE_PROFIT,
                           "qty": 100, "price": 75.0, "day": day.isoformat(),
                           "pnl_pct": 0.08,
                           "entry_factors": {"quality": 0.7, "valuation": 0.6,
                                             "low_vol": 0.7, "momentum": 0.5}}]
        learner.review(day)
        path = learner.export_progress_chart("data/learning_progress.html")
        self.assertTrue(os.path.exists(path))
        with open(path, encoding="utf-8") as f:
            html = f.read()
        self.assertIn("学习进度曲线", html)
        self.assertIn("<svg", html)

    def test_style_check_returns_list(self):
        sim, agent, learner = self._make()
        drifts = agent.style_check()
        self.assertIsInstance(drifts, list)


if __name__ == "__main__":
    unittest.main(verbosity=2)
