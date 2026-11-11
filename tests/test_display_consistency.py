"""交易规则与行情显示一致性专项整改 · 验证单元测试。

覆盖 P0–P3 全部修复点：
- P0.1 快照频率 3s 对齐（scheduler.REFRESH_SEC == 3）
- P0.2 空快照不回退虚构价（resolve_display_price NOT_READY）
- P1.1 核心行情指标落盘（open/high/low/volume/change_pct/ceiling/floor + _meta）
- P1.2 涨跌停状态展示（limit_hit UP/DOWN）
- P1.3 停牌检测（is_suspended 落盘 + 撮合拒单）
- P2.1 交易阶段标签（9:00–9:30 → 盘前 / 集合竞价；非午休误标）
- P2.3 滑点已在 README/看板文案声明（此处校验规则 slippage_rate 存在）
- P3 README 口径（按股票名称是否含 st 判涨跌停）
"""
import os
import sys
import json
import tempfile
import unittest
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.settings import SimulatorConfig, FeeConfig
from core.simulator import Simulator
from core.order import Position, OrderType, OrderStatus, Side
from core.trading_rules import TradingRules
from storage.local_store import LocalStore
from market.mock_provider import MockProvider
from market.base import Quote
from market.feed import detect_suspension
from market.display import resolve_display_price, market_phase_label

import scheduler  # noqa: E402  (验证 REFRESH_SEC)


class TestP0SourceConsistency(unittest.TestCase):
    def test_scheduler_refresh_3s(self):
        self.assertEqual(scheduler.REFRESH_SEC, 3,
                         "盘中快照刷新须与看板重绘对齐为 3s")

    def test_empty_snapshot_not_ready_no_fake_price(self):
        d = resolve_display_price({}, "sh600519")
        self.assertEqual(d["status"], "NOT_READY")
        self.assertIsNone(d["price"], "空快照严禁回退虚构价")
        self.assertEqual(d["warn"], "行情数据未就绪")

    def test_ready_snapshot_carries_meta(self):
        live = {"sh600519": {
            "price": 1680.0, "name": "贵州茅台", "prev_close": 1700.0,
            "open": 1690.0, "high": 1695.0, "low": 1680.0,
            "volume": 2_000_000, "change_pct": -1.18,
            "ceiling": 1870.0, "floor": 1530.0, "industry": "白酒",
            "is_suspended": False, "limit_hit": "NONE",
            "updated_at": "2026-09-23T10:00:00", "source": "westock",
        }}
        d = resolve_display_price(live, "sh600519")
        self.assertEqual(d["status"], "READY")
        self.assertEqual(d["price"], 1680.0)
        self.assertEqual(d["open"], 1690.0)
        self.assertEqual(d["high"], 1695.0)
        self.assertEqual(d["low"], 1680.0)
        self.assertEqual(d["volume"], 2_000_000)
        self.assertAlmostEqual(d["change_pct"], -1.18)
        self.assertEqual(d["ceiling"], 1870.0)
        self.assertEqual(d["floor"], 1530.0)
        self.assertEqual(d["source"], "westock")
        self.assertEqual(d["updated_at"], "2026-09-23T10:00:00")


class TestP1FieldsAndStatus(unittest.TestCase):
    def _sim(self, code="sh600519", suspended=False):
        tmp = tempfile.mkdtemp(prefix="disp_")
        mp = MockProvider()
        if suspended:
            mp.set_suspended(code, True)
        sim = Simulator(
            SimulatorConfig(provider="mock", storage_dir=tmp),
            market=mp, storage=LocalStore(tmp),
        )
        sim.account.positions[code] = Position(
            code=code, name=mp.get_quote(code).name, quantity=100,
            available=100, cost_price=10.0,
        )
        return sim, tmp, code

    def test_live_prices_writes_full_fields_and_meta(self):
        sim, tmp, code = self._sim()
        data = sim.refresh_live_prices()
        self.assertIn(code, data)
        e = data[code]
        for f in ("price", "prev_close", "open", "high", "low",
                  "volume", "change_pct", "ceiling", "floor",
                  "industry", "is_suspended", "limit_hit",
                  "updated_at", "source"):
            self.assertIn(f, e, f"落盘缺少字段 {f}")
        self.assertFalse(e["is_suspended"])
        self.assertIn("_meta", data)
        self.assertEqual(data["_meta"]["source"], "mock")
        self.assertEqual(data["_meta"]["positions"], 1)
        # 涨跌停阈值由名称含 st 判定（主板 ±10%）；茅台 prev_close=1700 → ceiling=1870
        self.assertAlmostEqual(e["ceiling"], round(1700.0 * 1.1, 2), 2)
        self.assertAlmostEqual(e["floor"], round(1700.0 * 0.9, 2), 2)

    def test_live_prices_suspended_flag(self):
        sim, tmp, code = self._sim(suspended=True)
        data = sim.refresh_live_prices()
        self.assertIn(code, data)
        self.assertTrue(data[code]["is_suspended"])
        self.assertEqual(data[code]["limit_hit"], "NONE")

    def test_limit_hit_up(self):
        live = {"x": {"price": 11.0, "prev_close": 10.0, "ceiling": 11.0,
                      "floor": 9.0, "is_suspended": False, "limit_hit": "UP"}}
        self.assertEqual(resolve_display_price(live, "x")["limit_hit"], "UP")

    def test_suspended_display_status(self):
        live = {"x": {"price": 10.0, "is_suspended": True, "limit_hit": "NONE"}}
        d = resolve_display_price(live, "x")
        self.assertEqual(d["status"], "SUSPENDED")
        self.assertTrue(d["is_suspended"])


class TestP1SuspensionMatching(unittest.TestCase):
    def test_matching_rejects_suspended(self):
        tmp = tempfile.mkdtemp(prefix="susp_")
        mp = MockProvider()
        mp.set_suspended("sh600519", True)
        sim = Simulator(
            SimulatorConfig(provider="mock", storage_dir=tmp),
            market=mp, storage=LocalStore(tmp),
        )
        order = sim.buy("sh600519", 100, OrderType.MARKET,
                        now=datetime(2026, 9, 23, 10, 0, 0))
        self.assertEqual(order.status, OrderStatus.REJECTED)
        self.assertIn("停牌", order.reject_reason)

    def test_detect_suspension(self):
        self.assertTrue(detect_suspension(10.0, 10.0, 0.0, suspended=True))
        self.assertTrue(detect_suspension(10.0, 10.0, 0.0, suspended=False))
        self.assertFalse(detect_suspension(12.34, 10.0, 1_000_000, False))


class TestP2PhaseLabel(unittest.TestCase):
    def test_pre_open_label(self):
        # 9:00–9:15 → 盘前；9:15–9:30 → 集合竞价（均不应是「午休」）
        ph = market_phase_label(datetime(2026, 9, 23, 9, 5, 0))
        self.assertEqual(ph["state"], "PRE")
        self.assertEqual(ph["label"], "盘前")
        ph2 = market_phase_label(datetime(2026, 9, 23, 9, 15, 0))
        self.assertEqual(ph2["state"], "PRE")
        self.assertEqual(ph2["label"], "集合竞价")

    def test_trading_label(self):
        ph = market_phase_label(datetime(2026, 9, 23, 10, 0, 0))
        self.assertEqual(ph["state"], "LIVE")
        self.assertEqual(ph["label"], "交易中 · 连续竞价")

    def test_noon_break_label(self):
        ph = market_phase_label(datetime(2026, 9, 23, 12, 0, 0))
        self.assertEqual(ph["label"], "午休")

    def test_weekend_closed(self):
        ph = market_phase_label(datetime(2026, 9, 26, 10, 0, 0))  # 周六
        self.assertEqual(ph["state"], "CLOSED")


class TestP3RulesDoc(unittest.TestCase):
    def test_price_limit_by_name_st(self):
        rules = TradingRules(SimulatorConfig().rules)
        # 名称含 st → ±5%
        self.assertAlmostEqual(rules.price_limit_pct("sh600868", "ST云城"), 0.05)
        # 主板 → ±10%
        self.assertAlmostEqual(rules.price_limit_pct("sh600519", "贵州茅台"), 0.10)

    def test_readme_st_wording(self):
        readme_path = os.path.join(os.path.dirname(
            os.path.dirname(os.path.abspath(__file__))), "README.md")
        with open(readme_path, encoding="utf-8") as f:
            readme = f.read()
        self.assertIn("按股票名称是否含 'st'", readme,
                      "README 须声明按名称含 st 判定涨跌停")
        self.assertNotIn("按代码前缀回退计算", readme,
                         "README 旧口径（按代码前缀）须已移除")

    def test_slippage_rate_present(self):
        self.assertGreater(FeeConfig().slippage_rate, 0.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
