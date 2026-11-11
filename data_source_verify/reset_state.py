import os, json, sys
HERE = r"C:\Users\Administrator\WorkBuddy\2026-09-21-17-21-45\stock-simulator"
DATA = os.path.join(HERE, "data")
sys.path.insert(0, HERE)
from datetime import date
from config.settings import SimulatorConfig
from core.simulator import Simulator

TODAY = "2026-09-23"

# 1) account.json: 仅把 current_day 从未来日期改回今日（保留 50000 / 0 持仓）
ap = os.path.join(DATA, "account.json")
acc = json.load(open(ap, encoding="utf-8"))
acc["current_day"] = TODAY
acc["cash"] = float(acc.get("cash", 50000.0))
acc["initial_capital"] = 50000.0
acc["positions"] = {}
acc["realized_pnl"] = 0.0
json.dump(acc, open(ap, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
print("account.json -> current_day=%s cash=%.2f positions=%d" % (
    acc["current_day"], acc["cash"], len(acc["positions"])))

# 2) 清空历史运行产物（让明日起步干净；保留 learned_params 学习权重）
for fn in ["trades.jsonl", "risk_log.jsonl", "learning_progress.jsonl"]:
    open(os.path.join(DATA, fn), "w", encoding="utf-8").close()
    print("cleared", fn)
# live_prices.json 置空
json.dump({}, open(os.path.join(DATA, "live_prices.json"), "w", encoding="utf-8"))
print("cleared live_prices.json")

# 3) snapshots.jsonl: 清空后追加一条今日正确快照
open(os.path.join(DATA, "snapshots.jsonl"), "w", encoding="utf-8").close()
sim = Simulator(SimulatorConfig(provider="westock"))
assert sim.load()
sim.account.current_day = date.fromisoformat(TODAY)
sim.save()
snap = sim.snapshot()
print("appended snapshot day=%s total=%.2f positions=%d return=%.4f" % (
    snap.get("day"), snap.get("total_assets"), snap.get("position_count"),
    snap.get("total_return_pct")))

# 4) last_run_day.txt 保持今日（明天 9-24 才会触发新跑）
open(os.path.join(DATA, "last_run_day.txt"), "w", encoding="utf-8").write(TODAY + "\n")
print("last_run_day.txt ->", TODAY)
