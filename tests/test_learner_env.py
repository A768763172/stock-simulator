"""四维自学习模块单元测试（量价 + 政策 + 消息 + 情绪）。

覆盖：四维盈亏归因、四维权重学习、动态适配（政策/情绪/消息）、
策略类型自动切换、风控联动升级、全场景回测验证（优化前 vs 优化后）。
"""
import os
import sys
import tempfile
import unittest
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.settings import SimulatorConfig
from config.strategy import StrategyConfig
from core.simulator import Simulator
from strategy.agent import StrategyAgent
from strategy.learner import (
    Learner, attribute_trade, scenario_backtest,
    STOP_LOSS, TAKE_PROFIT, HOLD_TIMEOUT, RISK_REDUCE,
)
from strategy.market_env import EnvState, MockEnvProvider


def _make(provider=None, cfg=None):
    tmp = tempfile.mkdtemp(prefix="sim_env_")
    sim = Simulator(SimulatorConfig(provider="mock", storage_dir=tmp))
    agent = StrategyAgent(sim, cfg or StrategyConfig(), storage=sim.storage)
    learner = Learner(agent, storage=sim.storage, env_provider=provider)
    agent.attach_learner(learner)
    return sim, agent, learner


class TestFourDimAttribution(unittest.TestCase):
    def test_policy_bull_profit_attributed_to_policy(self):
        # 政策利好窗口盈利 → 政策维度贡献应显著、量价补差小
        entry = EnvState(policy_active=True, policy_score=0.9,
                         policy_tag="货币宽松", sentiment_index=0.1)
        exit = EnvState()
        r = attribute_trade(0.10, 0.01, TAKE_PROFIT, {"quality": 0.7}, entry, exit)
        fd = r["four_dim"]
        self.assertGreater(fd["policy"], 0.4)
        self.assertLess(fd["price"], 0.6)
        self.assertAlmostEqual(sum(fd.values()), 1.0, 6)

    def test_panic_loss_attributed_to_sentiment(self):
        # 情绪恐慌期亏损 → 情绪维度贡献最高
        entry = EnvState(sentiment_phase="恐慌", sentiment_index=-0.7)
        exit = EnvState()
        r = attribute_trade(-0.08, -0.05, STOP_LOSS, {"quality": 0.5}, entry, exit)
        fd = r["four_dim"]
        self.assertGreater(fd["sentiment"], fd["price"])
        self.assertGreater(fd["sentiment"], 0.3)

    def test_no_env_falls_back_to_price(self):
        # 无环境快照 → 全部归量价，避免凭空虚增政策/情绪贡献
        r = attribute_trade(0.05, 0.0, TAKE_PROFIT, {"quality": 0.6})
        fd = r["four_dim"]
        self.assertEqual(fd["price"], 1.0)
        self.assertEqual(fd["policy"], 0.0)

    def test_reversal_env_no_misattribute(self):
        # 政策利好但个股仍亏（反向环境）→ 不归政策，归量价（选股/技术）
        entry = EnvState(policy_active=True, policy_score=0.8,
                         policy_tag="产业扶持")
        exit = EnvState()
        r = attribute_trade(-0.04, 0.02, STOP_LOSS, {"quality": 0.4}, entry, exit)
        fd = r["four_dim"]
        self.assertEqual(fd["policy"], 0.0)
        self.assertGreater(fd["price"], 0.0)


class TestDynamicAdaptation(unittest.TestCase):
    def test_policy_bull_lifts_position(self):
        day = date(2026, 9, 21)
        prov = MockEnvProvider(StrategyConfig(),
                               force_policy_days=[day.isoformat()])
        _, _, learner = _make(provider=prov)
        base = learner.cfg.base_total_target
        learner.sense_environment(day)
        self.assertGreater(learner.cfg.base_total_target, base,
                           "政策利好应提升总仓中枢")
        self.assertTrue(learner.env_state.policy_active)

    def test_panic_cuts_position_and_tightens_stop(self):
        day = date(2026, 9, 21)
        prov = MockEnvProvider(StrategyConfig(), force_sentiment=-0.6)
        _, _, learner = _make(provider=prov)
        base = learner.cfg.base_total_target
        sl0 = learner.cfg.stop_loss_pct
        learner.sense_environment(day)
        self.assertLess(learner.cfg.base_total_target, base,
                        "情绪恐慌应降低总仓中枢")
        self.assertGreater(learner.cfg.stop_loss_pct, sl0,
                           "情绪恐慌应收紧止损")
        self.assertEqual(learner.env_state.sentiment_phase, "冰点")

    def test_major_bearish_news_cuts_frequency(self):
        day = date(2026, 9, 21)
        prov = MockEnvProvider(StrategyConfig(), force_news_days=[day.isoformat()])
        _, _, learner = _make(provider=prov)
        freq0 = learner.cfg.max_new_open_per_week
        learner.sense_environment(day)
        self.assertLess(learner.cfg.max_new_open_per_week, freq0,
                        "重磅利空应降低新开仓频率")
        self.assertGreaterEqual(learner.env_state.news_magnitude, 0.8)

    def test_adaptation_idempotent(self):
        day = date(2026, 9, 21)
        prov = MockEnvProvider(StrategyConfig(), force_sentiment=0.6)
        _, _, learner = _make(provider=prov)
        learner.sense_environment(day)
        a = learner.cfg.base_total_target
        learner.sense_environment(day)  # 再次感知，不应累积放大
        self.assertAlmostEqual(learner.cfg.base_total_target, a, 6)


class TestStrategySwitch(unittest.TestCase):
    def test_policy_bull_favors_trend_follow_only_when_effective(self):
        day = date(2026, 9, 21)
        prov = MockEnvProvider(StrategyConfig(),
                               force_policy_days=[day.isoformat()])
        _, _, learner = _make(provider=prov)
        # 政策驱动期：仅当趋势跟踪「历史有效」时才顺从环境加仓；
        # 无历史有效性时不应盲从环境标签把权重推高（此前会一路推到 0.777 却持续亏损）
        learner.strategy_eff = {"trend_follow": 0.0, "reversal": 0.0, "grid": 0.0}
        learner.review(day)
        self.assertEqual(learner.strategy_weights["trend_follow"], 0.50,
                         "无历史有效性时，政策标签不应盲推趋势跟踪")
        # 给定趋势跟踪历史有效，则顺从环境加仓
        _, _, learner2 = _make(provider=prov)
        learner2.strategy_eff = {"trend_follow": 0.3, "reversal": 0.0, "grid": 0.0}
        learner2.review(day)
        self.assertGreater(learner2.strategy_weights["trend_follow"], 0.50,
                          "趋势跟踪历史有效时，政策驱动期应倾斜趋势跟踪")


class TestRiskEscalation(unittest.TestCase):
    def test_extreme_sentiment_escalates(self):
        day = date(2026, 9, 21)
        prov = MockEnvProvider(StrategyConfig(), force_sentiment=0.6)  # 亢奋
        _, _, learner = _make(provider=prov)
        dd0 = learner.cfg.dd_daily_max
        learner.sense_environment(day)
        self.assertTrue(learner.risk_escalated, "极端情绪应触发风控升级")
        self.assertLess(learner.cfg.dd_daily_max, dd0,
                        "升级后回撤阈值应更敏感（数值更小）")

    def test_escalation_recovers_when_calm(self):
        day = date(2026, 9, 21)
        prov = MockEnvProvider(StrategyConfig(), force_sentiment=0.6)
        _, _, learner = _make(provider=prov)
        learner.sense_environment(day)
        self.assertTrue(learner.risk_escalated)
        # 切换到中性环境
        prov2 = MockEnvProvider(StrategyConfig(), force_sentiment=0.0)
        learner.env_provider = prov2
        learner.sensor.provider = prov2
        learner.sense_environment(day)
        self.assertFalse(learner.risk_escalated, "环境回稳应解除升级")


class TestScenarioBacktest(unittest.TestCase):
    def test_scenario_compare_shape_and_verdict(self):
        sc = scenario_backtest()
        self.assertEqual(len(sc["scenarios"]), 4)
        for s in sc["scenarios"]:
            self.assertIn("price_only", s)
            self.assertIn("four_dim", s)
            for m in (s["price_only"], s["four_dim"]):
                self.assertIn("return", m)
                self.assertIn("mdd", m)
                self.assertIn("win_rate", m)
                self.assertIn("profit_factor", m)
                self.assertIn("sharpe", m)
        # 四维策略在风险可控下至少 3/4 场景不劣于纯量价
        self.assertGreaterEqual(sc["summary"]["four_dim_wins"], 3)

    def test_review_produces_env_and_scenario(self):
        day = date(2026, 9, 21)
        prov = MockEnvProvider(StrategyConfig(),
                               force_policy_days=[day.isoformat()])
        _, agent, learner = _make(provider=prov)
        # 预置带环境快照的退出流水，验证四维归因进入 learner
        agent.exit_log = [{
            "eid": "e1", "code": "sh600519", "reason": TAKE_PROFIT,
            "qty": 100, "price": 75.0, "day": day.isoformat(),
            "pnl_pct": 0.10,
            "entry_factors": {"quality": 0.7, "valuation": 0.6,
                              "low_vol": 0.7, "momentum": 0.5},
            "entry_env": EnvState(policy_active=True, policy_score=0.9,
                                  policy_tag="货币宽松").to_dict(),
            "exit_env": EnvState().to_dict(),
        }]
        rep = learner.review(day)
        self.assertIn("policy_score", rep["progress"])
        self.assertTrue(rep["progress"]["policy_active"])
        # 第 5 个交易日应产出全场景回测验证
        d = day
        last = None
        for i in range(5):
            d = day if i == 0 else agent.sim.market.next_trading_day(d)
            last = learner.review(d)
        self.assertIsNotNone(last["scenario"], "应产出全场景回测验证")
        self.assertEqual(len(last["scenario"]["scenarios"]), 4)


if __name__ == "__main__":
    unittest.main(verbosity=2)
