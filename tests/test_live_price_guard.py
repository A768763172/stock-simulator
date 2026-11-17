"""实时价采集脏价防护单元测试（要求：实时采集到真实行情，绝不落脏值）。

覆盖：
- 源返回 0 / 非正价格 → 校验闸门拦截，绝不直接落库（看板显示缺失而非 0）。
- 源瞬时失败 → 沿用上一次有效价快照（data/live_prices.json），不闪烁成 0。
- 源返回真实有效价 → 正常落库。
"""
import os
import sys
import json
import types
import tempfile
import unittest
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.settings import SimulatorConfig
from core.simulator import Simulator
from core.order import Position
from storage.local_store import LocalStore
from market.mock_provider import MockProvider
from market.base import Quote


class _GuardProvider(MockProvider):
    """可控行情源：mode=ok 返回真实价；zero 返回 0；raise 抛异常。"""
    def __init__(self, mode="ok"):
        super().__init__()
        self.mode = mode

    def get_quote(self, code):
        if self.mode == "zero":
            return Quote(code=code, name=code, price=0.0, prev_close=10.0,
                         open=10.0, high=10.5, low=9.5)
        if self.mode == "raise":
            raise RuntimeError("simulated upstream failure")
        return Quote(code=code, name=code, price=12.34, prev_close=10.0,
                     open=10.2, high=12.5, low=10.0, ceiling=13.0, floor=9.0)

    def get_fundamentals(self, code):
        return types.SimpleNamespace(code=code, name=code, industry="测试")


class TestLivePriceGuard(unittest.TestCase):
    def _sim_with_position(self, mode, code="sh600000"):
        tmp = tempfile.mkdtemp(prefix="lp_guard_")
        sim = Simulator(
            SimulatorConfig(provider="mock", storage_dir=tmp),
            market=_GuardProvider(mode),
            storage=LocalStore(tmp),
        )
        sim.account.positions[code] = Position(
            code=code, name=code, quantity=100, available=100,
            cost_price=10.0,
        )
        return sim, tmp, code

    def test_dirty_zero_not_written(self):
        """源返回 0 价：校验未过且无有效价可回退 → 不落库任何脏值。"""
        sim, tmp, code = self._sim_with_position("zero")
        data = sim.refresh_live_prices()
        self.assertNotIn(code, data, "0 价不应落库")
        with open(os.path.join(tmp, "live_prices.json"), encoding="utf-8") as f:
            lp = json.load(f)
        self.assertNotIn(code, lp, "live_prices.json 不应含 0 价标的")

    def test_source_failure_falls_back_to_valid(self):
        """源瞬时失败：沿用上一次有效价快照，不写成 0。"""
        sim, tmp, code = self._sim_with_position("raise")
        # 先写入一份有效 fallback（模拟此前已成功采集过）
        sim.storage.save_live_prices({
            code: {"price": 9.9, "name": code, "prev_close": 10.0,
                   "industry": "测试",
                   "updated_at": datetime.now().isoformat()},
        })
        data = sim.refresh_live_prices()
        self.assertIn(code, data, "源失败时应用回退价")
        self.assertEqual(data[code]["price"], 9.9)

    def test_valid_price_written(self):
        """源返回真实有效价 → 正常落库。"""
        sim, tmp, code = self._sim_with_position("ok")
        data = sim.refresh_live_prices()
        self.assertIn(code, data)
        self.assertEqual(data[code]["price"], 12.34)
        self.assertEqual(data[code]["industry"], "测试")

    def test_current_prices_uses_valid_or_fallback(self):
        """市值计算：脏价源回退有效价，避免持仓市值被算成 0。"""
        sim, tmp, code = self._sim_with_position("zero")
        sim.storage.save_live_prices({
            code: {"price": 9.9, "name": code, "prev_close": 10.0,
                   "industry": "测试",
                   "updated_at": datetime.now().isoformat()},
        })
        prices = sim.pnl._current_prices()
        self.assertEqual(prices[code], 9.9)


if __name__ == "__main__":
    unittest.main(verbosity=2)
