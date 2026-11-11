"""Mock 模式联动演示守护（临时演示用，不影响生产调度 scheduler.py）。

目的：在 westock 行情源不可达时，用 MockProvider 演示完整联动链路
  MockProvider(行情接入) → Simulator.refresh_live_prices(落盘) → dashboard(看板展示)
落盘路径与 scheduler.py 完全一致（data/live_prices.json），看板只读该文件，
因此演示与真实链路同源、同时效、同展示逻辑。

运行：python demo_mock_daemon.py
退出：Ctrl+C（演示进程）
"""
import os
import sys
import time
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from config.settings import SimulatorConfig
from core.simulator import Simulator
from core.position import Position
from market.mock_provider import MockProvider
from storage.local_store import LocalStore

DATA = os.path.join(HERE, "data")


def main() -> None:
    mp = MockProvider()
    # 控几只样本股，构造可区分的 OHLC（验证字段透传完整性）
    samples = {
        "sh600519": ("贵州茅台", 1705.0, 1700.0),
        "sz300750": ("宁德时代", 265.0, 260.0),
        "sh601318": ("中国平安", 52.0, 51.5),
    }
    for code, (nm, price, prev) in samples.items():
        mp.set_price(code, price, prev)

    sim = Simulator(SimulatorConfig(provider="mock", storage_dir=DATA),
                    market=mp, storage=LocalStore(DATA))
    sim.load()

    # 空仓时注入模拟持仓，便于演示「持仓实时行情→看板」链路
    if not sim.account.positions:
        sim.account.positions["sh600519"] = Position(
            code="sh600519", name="贵州茅台", quantity=100,
            available=100, cost_price=1700.0)
        sim.account.positions["sz300750"] = Position(
            code="sz300750", name="宁德时代", quantity=200,
            available=200, cost_price=255.0)
        sim.account.positions["sh601318"] = Position(
            code="sh601318", name="中国平安", quantity=500,
            available=500, cost_price=50.0)
        sim.save()
        print("[demo] 已注入模拟持仓（贵州茅台/宁德时代/中国平安）", flush=True)

    print("[demo] Mock 调度守护启动：每 3s 刷新 data/live_prices.json", flush=True)
    while True:
        try:
            data = sim.refresh_live_prices()
            meta = data.get("_meta", {})
            n = len([k for k in data if k != "_meta"])
            ts = datetime.now().strftime("%H:%M:%S")
            print(f"[{ts}] 刷新 {n} 只持仓 | generated_at={meta.get('generated_at')} "
                  f"source={meta.get('source')} positions={meta.get('positions')}",
                  flush=True)
        except Exception as e:
            print(f"[{datetime.now().strftime('%H:%M:%S')}] 刷新异常: {e}", flush=True)
        time.sleep(3)


if __name__ == "__main__":
    main()
