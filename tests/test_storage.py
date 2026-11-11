import sys
import os
import tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import date, datetime
from config.settings import SimulatorConfig
from core.simulator import Simulator
from storage.local_store import LocalStore


def test_save_load_roundtrip():
    tmp = tempfile.mkdtemp()
    # 初始本金取足以便整手买入成交（本测试验证存取一致性，与策略初始本金无关）
    cfg = SimulatorConfig(provider="mock", storage_dir=tmp,
                          initial_capital=100_000_000.0)
    sim = Simulator(cfg)
    sim.set_day(date(2026, 9, 21))
    sim.buy("sh600519", 1000, now=datetime(2026, 9, 21, 10, 0, 0))
    sim.save()

    # 重新加载
    sim2 = Simulator(cfg)
    ok = sim2.load()
    assert ok is True
    assert abs(sim2.account.cash - sim.account.cash) < 1e-6
    assert "sh600519" in sim2.account.positions
    assert len(sim2.account.trades) == len(sim.account.trades)


def test_reset():
    tmp = tempfile.mkdtemp()
    cfg = SimulatorConfig(provider="mock", storage_dir=tmp,
                          initial_capital=100_000_000.0)
    sim = Simulator(cfg)
    sim.set_day(date(2026, 9, 21))
    sim.buy("sh600519", 1000, now=datetime(2026, 9, 21, 10, 0, 0))
    sim.save()
    sim.reset()
    assert os.path.exists(os.path.join(tmp, "account.json")) is False
    assert abs(sim.account.cash - cfg.initial_capital) < 1e-6


if __name__ == "__main__":
    for k, v in list(globals().items()):
        if k.startswith("test_") and callable(v):
            v()
            print("OK", k)
