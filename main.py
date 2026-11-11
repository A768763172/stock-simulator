"""A股模拟炒股 —— 演示入口。

运行：
  python main.py                # 默认使用 westock（未安装则自动回退 mock）
  python main.py mock           # 强制离线演示
  SIM_PROVIDER=mock python main.py

演示覆盖：买入、T+1 拒卖、整手校验、跨日卖出、资产快照、本地落盘。
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from datetime import date, datetime

from config.settings import SimulatorConfig
from core.simulator import Simulator


def run_demo(provider: str = "westock") -> None:
    cfg = SimulatorConfig(provider=provider)
    sim = Simulator(cfg)

    day = date(2026, 9, 21)                 # 周一，交易日
    now = datetime(2026, 9, 21, 10, 0, 0)   # 交易时段内
    sim.set_day(day)

    print("=" * 56)
    print(" A股模拟炒股演示   初始本金：5.00 万元 (50000 元)   (provider=%s)" % provider)
    print("=" * 56)
    print(f"交易日: {day}   茅台(sh600519) 现价≈1680")

    o1 = sim.buy("sh600519", 1000, now=now)
    print(f"[1] 买入 1000 股  -> {o1.status.value}  {o1.reject_reason}")

    o2 = sim.sell("sh600519", 1000, now=now)
    print(f"[2] 当日卖出 1000 -> {o2.status.value}  {o2.reject_reason} (T+1)")

    o3 = sim.buy("sh600519", 150, now=now)
    print(f"[3] 买入 150 股   -> {o3.status.value}  {o3.reject_reason} (整手)")

    nxt = sim.market.next_trading_day(day)
    sim.set_day(nxt)
    now2 = datetime(nxt.year, nxt.month, nxt.day, 14, 0, 0)
    print(f"--- 切换下一交易日 {nxt} ---")

    o4 = sim.sell("sh600519", 1000, now=now2)
    print(f"[4] 次日卖出 1000 -> {o4.status.value}  {o4.reject_reason}")

    snap = sim.snapshot()
    print("\n--- 资产快照 ---")
    for k, v in snap.items():
        print(f"  {k}: {v}")

    print("\n--- 持仓明细 ---")
    for p in sim.positions():
        print(" ", p)

    print("\n--- 成交记录 ---")
    for t in sim.account.trades:
        print(f"  {t.trade_id} {t.side.value} {t.code} "
              f"{t.quantity}@{t.price} 费={t.total_fee:.2f} 已实现={t.realized_pnl:.2f}")

    sim.save()
    print("\n数据已落盘本地 data/（account.json / trades.jsonl / snapshots.jsonl），未上传任何服务器。")


if __name__ == "__main__":
    provider = os.environ.get("SIM_PROVIDER", "westock")
    if len(sys.argv) > 1:
        provider = sys.argv[1]
    run_demo(provider)
