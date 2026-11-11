"""全链路闭环深度核查 · 端到端联调测试（要求：二、全链路端到端联调）。

场景（期初 100000 元、无持仓、今日初始化）：
  1) 行情到达 10 元 → 限价单 10 元买入 1000 股 → 撮合成交
  2) 资金清算与持仓更新；勾稽关系校验（恒等式 A / B）
  3) 同日卖出尝试 → 应被 T+1 / 禁止裸卖空 拦截（REJECTED）
  4) 跨日至次日 → 行情上涨至 11 元 → 限价单 11 元卖出 1000 股 → 成交清算
  5) 最终校验：总资金 = 100000 + (11-10)*1000 - 两次手续费
  6) 额外验证：市价单滑点、行情接入闸门（去重/断档/单调性）

运行：python tests/test_e2e_closed_loop.py
"""
import os
import sys
import unittest
from datetime import date, datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.settings import SimulatorConfig, FeeConfig
from core.order import OrderType, OrderStatus, Side
from core.simulator import Simulator
from market.mock_provider import MockProvider
from market.feed import MarketFeed, Tick, validate_quote
from core.pnl import PnLStatistics


def _approx(a, b, tol=0.01):
    return abs(a - b) <= tol


class ClosedLoopTest(unittest.TestCase):
    def setUp(self):
        # 使用 MockProvider 精确控价；slippage 仅作用于市价单，限价单为 0
        self.mkt = MockProvider()
        self.cfg = SimulatorConfig(initial_capital=100_000.0)
        self.sim = Simulator(config=self.cfg, market=self.mkt, storage=None)

    # ---------- 主链路 ----------
    def test_full_closed_loop(self):
        INIT = 100_000.0
        day1 = date(2026, 1, 5)   # 周一
        day2 = date(2026, 1, 6)   # 周二

        # 初始化
        self.sim.set_day(day1)
        self.assertEqual(self.sim.account.current_day, day1)
        self.assertEqual(self.sim.account.cash, INIT)
        self.assertEqual(len(self.sim.account.positions), 0)

        # ① 行情到达 10 元
        self.mkt.set_price("sh600001", 10.0, prev_close=10.0)
        # ② 限价单 10 元买入 1000 股（交易时段内）
        now = datetime(2026, 1, 5, 10, 0, 0)
        o = self.sim.buy("sh600001", 1000, OrderType.LIMIT, 10.0, now)
        self.assertEqual(o.status, OrderStatus.FILLED, f"买入应成交，实际 {o.status}:{o.reject_reason}")

        # 成交价应为 10（限价单不计滑点）
        t_buy = self.sim.account.trades[-1]
        self.assertTrue(_approx(t_buy.price, 10.0), f"买入成交价应=10，实际 {t_buy.price}")
        self.assertEqual(t_buy.quantity, 1000)

        # ③ 资金清算与持仓更新
        buy_fee = t_buy.total_fee
        snap = self.sim.snapshot()
        # 勾稽 A：总资金 = 可用 + 冻结 + 市值
        self.assertTrue(_approx(snap["total_assets"],
                                 snap["cash"] + snap["frozen_cash"] + snap["market_value"]),
                        "买入后勾稽恒等式 A 不成立")
        # 恒等式「现有资金 = 期初 + 累计平仓盈亏 - 手续费 - 冻结」
        self.assertTrue(_approx(snap["total_assets"], INIT + 0.0 - buy_fee - snap["frozen_cash"]),
                        "买入后余额计算逻辑不成立")
        # T+1：当日买入 available=0
        pos = self.sim.account.get_position("sh600001")
        self.assertEqual(pos.available, 0)
        self.assertEqual(pos.quantity, 1000)
        self.assertTrue(_approx(pos.cost_price, 10.0))

        # ④ 同日卖出尝试 → 应被拒绝（禁止裸卖空 / T+1）
        o_rej = self.sim.sell("sh600001", 1000, OrderType.MARKET, 0.0, now)
        self.assertEqual(o_rej.status, OrderStatus.REJECTED,
                         f"同日卖出应被拒，实际 {o_rej.status}")
        self.assertIn("可卖数量", o_rej.reject_reason)

        # ⑤ 跨日：行情上涨至 11 元 → 卖出
        self.sim.set_day(day2)  # 释放 T+1 可卖
        self.mkt.set_price("sh600001", 11.0, prev_close=11.0)
        now2 = datetime(2026, 1, 6, 10, 0, 0)
        o2 = self.sim.sell("sh600001", 1000, OrderType.LIMIT, 11.0, now2)
        self.assertEqual(o2.status, OrderStatus.FILLED, f"卖出应成交，实际 {o2.status}:{o2.reject_reason}")

        t_sell = self.sim.account.trades[-1]
        self.assertTrue(_approx(t_sell.price, 11.0), f"卖出成交价应=11，实际 {t_sell.price}")
        sell_fee = t_sell.total_fee

        # ⑥ 最终校验：总资金 = 100000 + 1000 - 两次手续费
        final = self.sim.snapshot()
        expected = INIT + (11.0 - 10.0) * 1000 - buy_fee - sell_fee
        self.assertTrue(_approx(final["total_assets"], expected, 0.02),
                        f"总资金应={expected:.2f}，实际 {final['total_assets']:.2f}")
        # 无持仓
        self.assertEqual(len(self.sim.account.positions), 0)
        # 已实现盈亏 = 毛利 - 卖出费
        self.assertTrue(_approx(self.sim.account.realized_pnl,
                                1000.0 - sell_fee, 0.02))

        # 勾稽双恒等式
        rec = self.sim.pnl.reconcile()
        self.assertTrue(rec["identity_a_ok"], f"恒等式A失败: {rec}")
        self.assertTrue(rec["identity_b_ok"],
                        f"恒等式B失败 delta_b={rec['delta_b']}")
        print(f"[主链路] 通过 ✅ 总资金={final['total_assets']:.2f} "
              f"(期望 {expected:.2f}) 买费={buy_fee:.2f} 卖费={sell_fee:.2f} "
              f"已实现={self.sim.account.realized_pnl:.2f}")

    # ---------- 市价单滑点 ----------
    def test_market_order_slippage(self):
        cfg = SimulatorConfig(initial_capital=100_000.0,
                              fee=FeeConfig(slippage_rate=0.0005))
        sim = Simulator(config=cfg, market=MockProvider(), storage=None)
        sim.set_day(date(2026, 1, 5))
        sim.market.set_price("sh600001", 10.0, prev_close=10.0)
        now = datetime(2026, 1, 5, 10, 0, 0)
        ob = sim.buy("sh600001", 100, OrderType.MARKET, 0.0, now)
        self.assertEqual(ob.status, OrderStatus.FILLED)
        tb = sim.account.trades[-1]
        # 市价买入应含 5bps 滑点：10 * 1.0005 = 10.005（成交价在 Trade 上）
        self.assertTrue(_approx(tb.price, 10.005, 1e-6), f"市价买入滑点错误: {tb.price}")
        os_ = sim.sell("sh600001", 100, OrderType.MARKET, 0.0, now)  # 同日 T+1 拦截
        self.assertEqual(os_.status, OrderStatus.REJECTED)
        # 用次日释放后测卖出滑点
        sim.set_day(date(2026, 1, 6))
        sim.market.set_price("sh600001", 10.0, prev_close=10.0)
        os2 = sim.sell("sh600001", 100, OrderType.MARKET, 0.0,
                       datetime(2026, 1, 6, 10, 0, 0))
        self.assertEqual(os2.status, OrderStatus.FILLED)
        ts = sim.account.trades[-1]
        # 市价卖出滑点：10 * 0.9995 = 9.995
        self.assertTrue(_approx(ts.price, 9.995, 1e-6), f"市价卖出滑点错误: {ts.price}")
        print(f"[滑点] 通过 ✅ 买价={ob.price} 卖价={os2.price}")

    # ---------- 限价单未触达不成交 ----------
    def test_limit_not_triggered_stays_pending(self):
        sim = Simulator(config=self.cfg, market=self.mkt, storage=None)
        sim.set_day(date(2026, 1, 5))
        # 市价 11，高于限价买 10 → 限价买「≤10 才成交」，故不触达应挂单
        sim.market.set_price("sh600001", 11.0, prev_close=11.0)
        now = datetime(2026, 1, 5, 10, 0, 0)
        o = sim.buy("sh600001", 1000, OrderType.LIMIT, 10.0, now)
        self.assertEqual(o.status, OrderStatus.PENDING, "限价未触达应挂单")
        # 冻结资金应已记录
        self.assertTrue(sim.account.frozen_cash > 0, "挂单应冻结保证金")
        # 撤单后冻结释放
        sim.engine.cancel_order(o.order_id)
        self.assertEqual(o.status, OrderStatus.CANCELLED)
        self.assertTrue(_approx(sim.account.frozen_cash, 0.0), "撤单应释放冻结资金")
        # 行情跌至 10 → 限价买成交（≤10）
        sim.market.set_price("sh600001", 10.0, prev_close=10.0)
        o2 = sim.buy("sh600001", 1000, OrderType.LIMIT, 10.0, now)
        self.assertEqual(o2.status, OrderStatus.FILLED)
        r = sim.engine.cancel_order(o2.order_id)
        self.assertEqual(r.status, OrderStatus.FILLED, "已成交订单不可撤单")
        print("[限价/撤单] 通过 ✅ 未触达挂单、撤单释放冻结、已成交不可撤")

    # ---------- 行情接入闸门 ----------
    def test_market_feed_validation(self):
        feed = MarketFeed(expected_interval_sec=3.0, gap_threshold_sec=60.0)
        base = datetime(2026, 1, 5, 9, 30, 0)
        # 正常序列
        feed.push(Tick("sh600001", 10.0, base, prev_close=10.0))
        feed.push(Tick("sh600001", 10.0, base, prev_close=10.0))  # 同时间戳同价 → 重复
        feed.push(Tick("sh600001", 10.2,
                       base.replace(second=31), prev_close=10.0))
        # 时间戳回退
        feed.push(Tick("sh600001", 10.3,
                       base.replace(second=30), prev_close=10.0))
        # 断档（>60s）
        feed.push(Tick("sh600001", 10.4,
                       base.replace(second=30, minute=33), prev_close=10.0))
        rep = feed.report()
        self.assertTrue(rep["duplicates"] >= 1, "应检测到重复行情")
        self.assertFalse(rep["timestamp_monotonic"], "应检测到时间戳回退")
        self.assertTrue(rep["gaps"] >= 1, "应检测到断档")
        # 价格校验
        ok, _ = validate_quote(10.0, 10.0); self.assertTrue(ok)
        ok2, r2 = validate_quote(-1.0, 10.0); self.assertFalse(ok2)
        ok3, r3 = validate_quote(10.0, 10.0, ceiling=9.0, floor=11.0)
        self.assertFalse(ok3, "涨跌停倒置应被拒")
        print(f"[行情闸门] 通过 ✅ 重复={rep['duplicates']} 断档={rep['gaps']} "
              f"单调={rep['timestamp_monotonic']}")


if __name__ == "__main__":
    suite = unittest.TestLoader().loadTestsFromTestCase(ClosedLoopTest)
    runner = unittest.TextTestRunner(verbosity=2)
    res = runner.run(suite)
    sys.exit(0 if res.wasSuccessful() else 1)
