"""第二步专项测试：情绪结构指标 + 政策/消息行业选股加成 + 学习倍率。

覆盖：RealSentimentProvider 指标口径与降级、sensor 有读数聚合、
EnvState 条目持久化往返、screener 行业命中加成（含多空对冲）、
_dim_ratio 学习倍率对动态适配的调制、摘要脚本 industries 校验。
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.strategy import StrategyConfig
from strategy.market_env import (
    EnvState, RealSentimentProvider, MarketEnvSensor, FileEnvProvider,
)
from strategy.screener import Screener


def _breadth_market(up=6, down=4, limit_up=2, limit_down=0, prev=10.0):
    """构造带市场宽度的 MockProvider：up 只涨、down 只跌，前 limit_up 只涨停。

    用空 data 构造，避免内置样本股污染市场宽度统计。
    """
    from market.mock_provider import MockProvider
    m = MockProvider(data={})
    codes = ["sh6000%02d" % i for i in range(1, up + down + 1)]
    for i, c in enumerate(codes):
        price = prev * (1.05 if i < up else 0.96)
        m.set_price(c, round(price, 2), prev)
    for i in range(limit_up):
        m.set_price(codes[i], round(prev * 1.1, 2), prev)  # ceiling=prev*1.1
    for i in range(limit_down):
        m.set_price(codes[up + i], round(prev * 0.9, 2), prev)
    return m


class TestRealSentiment(unittest.TestCase):
    def setUp(self):
        self.cfg = StrategyConfig()

    def test_breadth_indicators(self):
        m = _breadth_market(up=6, down=4, limit_up=2)
        sensor = MarketEnvSensor(self.cfg, RealSentimentProvider(self.cfg))
        st = sensor.sense(date(2026, 10, 13), m)
        ind = st.sentiment_indicators
        # 涨跌家数：(6-4)/10 = 0.2
        self.assertAlmostEqual(ind["advance_decline"], 0.2, places=3)
        # 涨跌停：(2-0)/10*3 = 0.6
        self.assertAlmostEqual(ind["limit_up_down"], 0.6, places=3)
        # 振幅：带方向（上涨市场为正）
        self.assertGreater(ind["amplitude_rate"], 0.0)
        # 无源项不占位
        self.assertNotIn("northbound", ind)
        self.assertNotIn("margin", ind)
        self.assertNotIn("连板高度", ind)

    def test_down_market_negative_sentiment(self):
        m = _breadth_market(up=3, down=7, limit_up=0, limit_down=2)
        sensor = MarketEnvSensor(self.cfg, RealSentimentProvider(self.cfg))
        st = sensor.sense(date(2026, 10, 13), m)
        self.assertLess(st.sentiment_indicators["advance_decline"], 0)
        self.assertLess(st.sentiment_indicators["limit_up_down"], 0)
        # 下跌市场振幅符号为负（放量下跌=恐慌）
        self.assertLess(st.sentiment_indicators["amplitude_rate"], 0)

    def test_no_breadth_degrades_to_price_only(self):
        sensor = MarketEnvSensor(self.cfg, RealSentimentProvider(self.cfg))
        st = sensor.sense(date(2026, 10, 13), None)  # market=None
        ind = st.sentiment_indicators
        self.assertIn("turnover", ind)
        self.assertNotIn("advance_decline", ind)  # 无市场宽度：仅价格派生

    def test_missing_indicators_not_diluting(self):
        """无读数项的权重应让渡给有读数项（指数不被恒 0 稀释）。"""
        # 只用底层价格派生（无 breadth）：turnover+panic 两项权重 0.30
        sensor = MarketEnvSensor(self.cfg, RealSentimentProvider(self.cfg))
        st = sensor.sense(date(2026, 10, 13), None)
        # 若恒 0 项参与，wsum=1.0；让渡后 wsum=0.30。验证指数等于
        # 两项加权平均（而非被稀释到 1/4 幅度）。
        w = self.cfg.sentiment_indicator_weights
        expect = (st.sentiment_indicators["turnover"] * w["turnover"]
                  + st.sentiment_indicators["panic_euphoria"] * w["panic_euphoria"]) \
            / (w["turnover"] + w["panic_euphoria"])
        self.assertAlmostEqual(st.sentiment_index, expect, places=3)


class TestEnvItemsAndSelection(unittest.TestCase):
    def setUp(self):
        self.cfg = StrategyConfig()
        self.tmp = tempfile.mkdtemp(prefix="sim_envsel_")
        self.digest_dir = Path(self.tmp)

    def _write_digest(self, day, policy=None, news=None):
        (self.digest_dir / f"env_digest_{day}.json").write_text(json.dumps({
            "day": day, "policy": policy or [], "news": news or [],
        }, ensure_ascii=False), encoding="utf-8")

    def _sensor(self):
        prov = FileEnvProvider(self.cfg, digest_dir=str(self.digest_dir))
        return MarketEnvSensor(self.cfg, prov)

    def test_items_roundtrip_persistence(self):
        self._write_digest("2026-10-13",
                           policy=[{"id": "p1", "level": "ministry",
                                    "strength": 0.5, "window_days": 10,
                                    "tag": "稳市", "industries": ["证券"]}],
                           news=[{"id": "n1", "nature": "bullish",
                                  "magnitude": 0.6, "half_life_days": 3,
                                  "tag": "业绩", "industries": ["半导体"]}])
        st = self._sensor().sense(date(2026, 10, 13))
        self.assertEqual(len(st.policy_items), 1)
        self.assertEqual(st.policy_items[0]["industries"], ["证券"])
        self.assertEqual(st.news_items[0]["industries"], ["半导体"])
        # 持久化往返（strategy_state.json 存 env_state）
        st2 = EnvState.from_dict(st.to_dict())
        self.assertEqual(st2.policy_items, st.policy_items)
        self.assertEqual(st2.news_items, st.news_items)

    def test_env_adj_industry_hit(self):
        self._write_digest("2026-10-13",
                           policy=[{"id": "p1", "level": "ministry",
                                    "strength": 0.5, "window_days": 10,
                                    "tag": "稳市",
                                    "industries": ["证券", "银行"]}])
        st = self._sensor().sense(date(2026, 10, 13))
        dw = {"policy": 0.2, "news": 0.15, "sentiment": 0.25, "price": 0.4}
        scr = Screener(self.cfg, None)
        hit = scr._env_adjustment(st, dw, "证券")
        miss = scr._env_adjustment(st, dw, "食品饮料")
        self.assertGreater(hit, 0.0)
        self.assertEqual(miss, 0.0)  # 未命中不加分（保证排序区分度）

    def test_env_adj_bull_bear_offset(self):
        """同一行业被利好+利空同时点名时应对冲。"""
        self._write_digest("2026-10-13",
                           policy=[{"id": "p1", "level": "ministry",
                                    "strength": 0.4, "window_days": 10,
                                    "tag": "利好", "industries": ["汽车整车"]},
                                   {"id": "p2", "level": "ministry",
                                    "strength": -0.4, "window_days": 10,
                                    "tag": "利空", "industries": ["汽车整车"]}])
        st = self._sensor().sense(date(2026, 10, 13))
        dw = {"policy": 0.2, "news": 0.15, "sentiment": 0.25, "price": 0.4}
        scr = Screener(self.cfg, None)
        adj = scr._env_adjustment(st, dw, "汽车整车")
        self.assertAlmostEqual(adj, 0.0, places=6)

    def test_env_adj_none_env_is_zero(self):
        scr = Screener(self.cfg, None)
        self.assertEqual(scr._env_adjustment(None, {"policy": 0.2}, "证券"), 0.0)
        self.assertEqual(scr._env_adjustment(None, {}, "证券"), 0.0)

    def test_dim_ratio_learned_weights(self):
        """学习权重偏离初值 → 倍率偏离 1 → 环境偏移按倍率缩放。"""
        from strategy.learner import Learner
        tmp = tempfile.mkdtemp(prefix="sim_ratio_")
        from config.settings import SimulatorConfig
        from core.simulator import Simulator
        from strategy.agent import StrategyAgent
        sim = Simulator(SimulatorConfig(provider="mock", storage_dir=tmp))
        agent = StrategyAgent(sim, self.cfg, storage=sim.storage)
        learner = Learner(agent, storage=sim.storage)
        agent.attach_learner(learner)

        # 初始：倍率 1
        self.assertAlmostEqual(learner._dim_ratio("policy"), 1.0, places=6)
        # 政策归因持续为负 → 权重被学低 → 倍率 <1
        learner.four_dim_weights["policy"] = 0.10
        self.assertLess(learner._dim_ratio("policy"), 1.0)
        # 政策归因持续为正 → 倍率 >1
        learner.four_dim_weights["policy"] = 0.40
        self.assertGreater(learner._dim_ratio("policy"), 1.0)
        # 极端倍率被 clip 到 [0,3]
        learner.four_dim_weights["policy"] = 1.0
        self.assertLessEqual(learner._dim_ratio("policy"), 3.0)

    def test_dynamic_adaptation_scaled_by_ratio(self):
        """低学习权重下，政策提仓幅度应收敛（由学习决定影响大小）。"""
        from config.settings import SimulatorConfig
        from core.simulator import Simulator
        from strategy.agent import StrategyAgent
        from strategy.learner import Learner
        self._write_digest("2026-10-13",
                           policy=[{"id": "p1", "level": "national",
                                    "strength": 0.8, "window_days": 10,
                                    "tag": "宽松", "industries": []}])
        tmp = tempfile.mkdtemp(prefix="sim_adapt_")
        sim = Simulator(SimulatorConfig(provider="mock", storage_dir=tmp))
        agent = StrategyAgent(sim, self.cfg, storage=sim.storage)
        learner = Learner(agent, storage=sim.storage, env_provider=None)
        # 强制使用文件源
        learner.env_provider = FileEnvProvider(self.cfg, digest_dir=str(self.digest_dir))
        learner.sensor.provider = learner.env_provider
        agent.attach_learner(learner)

        base = learner._anchors["base_total_target"]
        learner.sense_environment(date(2026, 10, 13))
        boost_full = learner.cfg.base_total_target - base
        # 政策权重学低到 0.1（初值 0.2）→ 倍率 0.5 → 提仓减半
        learner.four_dim_weights["policy"] = 0.10
        learner.sense_environment(date(2026, 10, 13))
        boost_half = learner.cfg.base_total_target - base
        self.assertAlmostEqual(boost_half, boost_full * 0.5, places=3)
        self.assertLess(boost_half, boost_full)


class TestDigestScriptIndustries(unittest.TestCase):
    """摘要脚本 industries 字段校验。"""

    def _run(self, payload: dict, day="2026-10-13"):
        root = Path(__file__).resolve().parent.parent
        p = subprocess.run(
            [sys.executable, str(root / "scripts" / "gen_env_digest.py"),
             "--day", day],
            input=json.dumps(payload), capture_output=True, text=True,
            encoding="utf-8", timeout=30)
        return p

    def test_industries_valid(self):
        p = self._run({"policy": [{"id": "p1", "level": "national",
                                   "strength": 0.5, "window_days": 10,
                                   "tag": "t", "industries": ["证券", "银行"]}],
                       "news": []})
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("证券/银行", p.stdout)

    def test_industries_optional(self):
        p = self._run({"policy": [{"id": "p1", "level": "national",
                                   "strength": 0.5, "window_days": 10,
                                   "tag": "t"}],
                       "news": []})
        self.assertEqual(p.returncode, 0, p.stderr)

    def test_industries_wrong_type_rejected(self):
        p = self._run({"policy": [{"id": "p1", "level": "national",
                                   "strength": 0.5, "window_days": 10,
                                   "tag": "t", "industries": "证券"}],
                       "news": []})
        self.assertEqual(p.returncode, 2)
        self.assertIn("industries", p.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
