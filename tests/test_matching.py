import sys
import os
import tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import date, datetime
from config.settings import SimulatorConfig
from core.simulator import Simulator
from core.order import OrderType, OrderStatus, Side


def _sim():
    # 初始本金取足以便整手买入成交（本测试验证手续费计算，与策略初始本金无关）。
    # storage_dir 锚定临时目录，避免默认 "data" 污染生产数据。
    return Simulator(SimulatorConfig(provider="mock", initial_capital=100_000_000.0,
                                     storage_dir=tempfile.mkdtemp(prefix="match_")))


def _buy(o, code="sh600519", qty=1000, now=datetime(2026, 9, 21, 10, 0, 0),
         order_type=OrderType.MARKET, price=0.0):
    return o.buy(code, qty, order_type=order_type, price=price, now=now)


def test_fee_calculation():
    sim = _sim()
    sim.set_day(date(2026, 9, 21))
    # 限价单按限价精确成交（不计滑点），用于验证三费计算
    o = _buy(sim, order_type=OrderType.LIMIT, price=1680.0)
    assert o.status == OrderStatus.FILLED
    t = sim.account.trades[-1]
    # 1680 * 1000 = 1,680,000；佣金0.025%=420(>5)；过户费0.001%=16.8；印花税0
    assert t.commission == 420.0
    assert abs(t.transfer_fee - 16.8) < 1e-6
    assert t.stamp_tax == 0.0
    assert abs(t.total_fee - 436.8) < 1e-6
    assert abs(t.cash_flow - (-1_680_436.8)) < 1e-6


def test_lot_reject():
    sim = _sim()
    sim.set_day(date(2026, 9, 21))
    o = sim.buy("sh600519", 150, now=datetime(2026, 9, 21, 10, 0, 0))
    assert o.status == OrderStatus.REJECTED
    assert "100" in o.reject_reason


def test_session_reject():
    sim = _sim()
    sim.set_day(date(2026, 9, 21))
    # 15:01 收盘后
    o = sim.buy("sh600519", 100, now=datetime(2026, 9, 21, 15, 1, 0))
    assert o.status == OrderStatus.REJECTED
    assert "非交易时段" in o.reject_reason


def test_t1_enforced():
    sim = _sim()
    day = date(2026, 9, 21)
    sim.set_day(day)
    sim.buy("sh600519", 1000, order_type=OrderType.LIMIT, price=1680.0,
            now=datetime(2026, 9, 21, 10, 0, 0))

    # 当日卖出 -> T+1 拒绝
    o_same = sim.sell("sh600519", 1000, order_type=OrderType.LIMIT, price=1680.0,
                      now=datetime(2026, 9, 21, 14, 0, 0))
    assert o_same.status == OrderStatus.REJECTED
    assert "T+1" in o_same.reject_reason

    # 下一交易日卖出 -> 成交（限价1680 精确成交，不计滑点）
    nxt = sim.market.next_trading_day(day)
    sim.set_day(nxt)
    o_next = sim.sell("sh600519", 1000, order_type=OrderType.LIMIT, price=1680.0,
                     now=datetime(nxt.year, nxt.month, nxt.day, 14, 0, 0))
    assert o_next.status == OrderStatus.FILLED
    # 成本1680，卖价1680，已实现 = -卖出费用
    # 卖出费用：佣金420 + 印花税1680 + 过户费16.8 = 2116.8
    assert abs(sim.account.realized_pnl - (-2116.8)) < 1e-6


def test_limit_order_queue_and_retry():
    sim = _sim()
    sim.set_day(date(2026, 9, 21))
    # 现价1680，限价买入1600 -> 未触达，挂单
    o = sim.buy("sh600519", 1000, order_type=OrderType.LIMIT, price=1600.0,
                now=datetime(2026, 9, 21, 10, 0, 0))
    assert o.status == OrderStatus.PENDING
    # 改价到1600后重试 -> 成交
    sim.market.set_price("sh600519", 1600.0, prev_close=1680.0)
    sim.engine.retry_pending(now=datetime(2026, 9, 21, 10, 5, 0))
    assert o.status == OrderStatus.FILLED
    assert abs(sim.account.positions["sh600519"].cost_price - 1600.0) < 1e-6


def test_zero_price_sell_rejected():
    """行情源价格为 0 时必须拒单，不得据此撮合。

    回归 2026-10-13 14:31 事故：红利ETF(sh510880) 因行情源中断被以 0 元清仓，
    账面凭空产生 -690.94 伪亏损，并让学习模块误以为每笔交易都亏 100%。
    quote.price=0 时 detect_suspension 不触发（要求 p>0），涨跌停校验在
    ceiling/floor 同为 0 时也会放行，故必须在成交价处显式拦截。
    """
    sim = _sim()
    sim.set_day(date(2026, 9, 21))
    sim.buy("sh600519", 1000, order_type=OrderType.LIMIT, price=1680.0,
            now=datetime(2026, 9, 21, 10, 0, 0))
    cash_before = sim.account.cash
    # 行情源中断：现价归零
    sim.market.set_price("sh600519", 0.0, prev_close=1680.0)
    o = sim.sell("sh600519", 1000, now=datetime(2026, 9, 21, 14, 31, 0))
    assert o.status == OrderStatus.REJECTED
    assert "零价" in (o.reject_reason or "")
    # 持仓 / 现金不得被改动
    assert sim.account.positions["sh600519"].quantity == 1000
    assert abs(sim.account.cash - cash_before) < 1e-6


if __name__ == "__main__":
    for k, v in list(globals().items()):
        if k.startswith("test_") and callable(v):
            v()
            print("OK", k)
