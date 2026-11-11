"""FileEnvProvider 单元测试（每日资讯摘要文件 → 政策/消息环境输入）。

覆盖：当日摘要激活、跨日窗口衰减、缺文件/坏文件回退中性、
未来日期不生效、mtime 缓存刷新、情绪面委托不受影响。
"""
import json
import os
import sys
import tempfile
import time
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.strategy import StrategyConfig
from strategy.market_env import FileEnvProvider, MarketEnvSensor


def _write_digest(digest_dir: Path, day: str, policy=None, news=None,
                  raw: str = None) -> None:
    digest_dir.mkdir(parents=True, exist_ok=True)
    fp = digest_dir / f"env_digest_{day}.json"
    if raw is not None:
        fp.write_text(raw, encoding="utf-8")
        return
    fp.write_text(json.dumps({
        "day": day, "generated_by": "test",
        "policy": policy or [], "news": news or [],
    }, ensure_ascii=False), encoding="utf-8")


class TestFileEnvProvider(unittest.TestCase):
    def setUp(self):
        self.cfg = StrategyConfig()
        self.tmp = tempfile.mkdtemp(prefix="sim_fep_")
        self.digest_dir = Path(self.tmp)

    def _provider(self):
        return FileEnvProvider(self.cfg, digest_dir=str(self.digest_dir))

    def test_same_day_policy_and_news_active(self):
        _write_digest(self.digest_dir, "2026-10-13",
                      policy=[{"id": "p1", "level": "ministry",
                               "strength": 0.5, "window_days": 12,
                               "tag": "稳市机制"}],
                      news=[{"id": "n1", "nature": "bullish",
                             "magnitude": 0.6, "half_life_days": 4,
                             "tag": "业绩预增"}])
        sensor = MarketEnvSensor(self.cfg, self._provider())
        st = sensor.sense(date(2026, 10, 13))
        self.assertTrue(st.policy_active)
        self.assertAlmostEqual(st.policy_score, 0.5 * 0.6, places=4)  # ministry 权重 0.6
        self.assertEqual(st.policy_tag, "稳市机制")
        self.assertTrue(st.news_active)
        self.assertAlmostEqual(st.news_net_impact, 0.6, places=4)
        self.assertEqual(st.news_nature, "bullish")

    def test_window_decay_across_days(self):
        _write_digest(self.digest_dir, "2026-10-13",
                      policy=[{"id": "p1", "level": "national",
                               "strength": 1.0, "window_days": 10,
                               "tag": "货币宽松"}])
        p = self._provider()
        # age=0 → 衰减 1.0；age=5 → 衰减 1 - 5/11
        d0 = p.active_policies(date(2026, 10, 13))
        d5 = p.active_policies(date(2026, 10, 18))
        self.assertEqual(len(d0), 1)
        self.assertAlmostEqual(d0[0]["window_strength"], 1.0, places=4)
        self.assertAlmostEqual(d5[0]["window_strength"], 1.0 - 5 / 11, places=4)
        # age > window_days → 过期不生效
        self.assertEqual(p.active_policies(date(2026, 11, 1)), [])

    def test_news_half_life_decay(self):
        _write_digest(self.digest_dir, "2026-10-13",
                      news=[{"id": "n1", "nature": "bearish",
                             "magnitude": 0.8, "half_life_days": 2,
                             "tag": "利空"}])
        p = self._provider()
        n0 = p.active_news(date(2026, 10, 13))
        n2 = p.active_news(date(2026, 10, 15))
        self.assertAlmostEqual(n0[0]["impact"], -0.8, places=4)
        # 半衰 2 日：age=2 时冲击减半
        self.assertAlmostEqual(n2[0]["impact"], -0.4, places=4)

    def test_missing_dir_falls_back_neutral(self):
        p = FileEnvProvider(self.cfg, digest_dir=os.path.join(self.tmp, "nope"))
        sensor = MarketEnvSensor(self.cfg, p)
        st = sensor.sense(date(2026, 10, 13))
        self.assertFalse(st.policy_active)
        self.assertFalse(st.news_active)
        self.assertEqual(st.sentiment_index, 0.0)  # market=None 时情绪读数为 0

    def test_corrupt_file_skipped(self):
        _write_digest(self.digest_dir, "2026-10-13", raw="{not valid json")
        _write_digest(self.digest_dir, "2026-10-12",
                      policy=[{"id": "p1", "level": "industry",
                               "strength": 0.5, "window_days": 5,
                               "tag": "行业利好"}])
        p = self._provider()
        pol = p.active_policies(date(2026, 10, 13))
        self.assertEqual(p._skipped_files, 1)
        self.assertEqual(len(pol), 1)  # 坏文件被跳过，好文件仍生效

    def test_future_digest_not_active(self):
        _write_digest(self.digest_dir, "2026-10-20",
                      policy=[{"id": "p1", "level": "national",
                               "strength": 0.9, "window_days": 10,
                               "tag": "未来"}])
        p = self._provider()
        self.assertEqual(p.active_policies(date(2026, 10, 13)), [])

    def test_mtime_cache_refresh(self):
        _write_digest(self.digest_dir, "2026-10-13",
                      policy=[{"id": "p1", "level": "national",
                               "strength": 0.5, "window_days": 10, "tag": "v1"}])
        p = self._provider()
        first = p.active_policies(date(2026, 10, 13))
        self.assertAlmostEqual(first[0]["window_strength"], 0.5, places=4)
        time.sleep(0.01)
        _write_digest(self.digest_dir, "2026-10-13",
                      policy=[{"id": "p1", "level": "national",
                               "strength": 0.8, "window_days": 10, "tag": "v2"}])
        second = p.active_policies(date(2026, 10, 13))
        self.assertAlmostEqual(second[0]["window_strength"], 0.8, places=4)

    def test_sentiment_delegated(self):
        p = self._provider()
        raw = p.sentiment_raw(date(2026, 10, 13), None)
        self.assertIn("turnover", raw)
        self.assertIn("panic_euphoria", raw)


if __name__ == "__main__":
    unittest.main(verbosity=2)
