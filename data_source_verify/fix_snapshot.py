import os, sys
HERE = r"C:\Users\Administrator\WorkBuddy\2026-09-21-17-21-45\stock-simulator"
sys.path.insert(0, HERE)
from config.settings import SimulatorConfig
from core.simulator import Simulator

sim = Simulator(SimulatorConfig(provider="westock"))
ok = sim.load()
print("load ok:", ok, "day:", sim.account.current_day,
      "positions:", len(sim.account.positions), "cash:", round(sim.account.cash, 2))
sim.save()  # 追加一条今日正确快照（持仓/市值/盈亏均来自真实账户）
snap = sim.snapshot()
print("appended snapshot day=%s total=%.2f positions=%d return=%.4f" % (
    snap.get("day"), snap.get("total_assets"), snap.get("position_count"),
    snap.get("total_return_pct")))
