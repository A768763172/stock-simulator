"""stock-simulator · 每日自动运行入口（实盘数据驱动）。

由量化交易专家调度：每个交易日自动执行
  「行情获取 → 交易决策 → 模拟撮合 → 收盘复盘 → 策略优化」
完整流程，落盘本地 data/，看板只读这些文件，数据实时同步。

依赖：westock-data 技能（westock CLI 已安装）。行情通过 WeStockProvider 调用，
统一走本地 CLI，不产生任何对外写操作。

用法：
  python live_run.py                 # 运行当日流程（已运行则幂等跳过）
  python live_run.py --force         # 忽略幂等标记，强制重跑当日
  python live_run.py --date 2026-09-22

说明：
  - 仅处理「交易日」；非交易日（周末/休市）直接跳过，不影响看板。
  - 同日在 last_run_day.txt 已记录时跳过，避免自动化重复触发导致重复交易。
  - 行情接口带本地缓存（data/.westock_cache.json），首跑建立全市场缓存，
    后续每日仅刷新当日行情，显著降低网络请求与速率限制风险。
"""
import os
import sys
import json
import shutil
import time as _time
import subprocess
import argparse
from datetime import date, datetime, time as dtime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)


def _pid_alive(pid):
    """判断指定 PID 的进程是否仍在运行（跨平台）。"""
    if os.name == "nt":
        try:
            out = subprocess.run(
                ["tasklist", "/FI", "PID eq %d" % pid],
                capture_output=True, text=True, errors="ignore", timeout=5,
            ).stdout
            return str(pid) in out
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _acquire_run_lock(data_dir):
    """防止多个 live_run 引擎并发运行（杜绝重复引擎抢跑/重复交易）。

    返回释放函数；若已被其他进程持有则返回 None（调用方应直接退出）。
    陈旧锁（持有进程已不在）会被自动覆盖。
    """
    import atexit
    lock = os.path.join(data_dir, ".live_run.pid")
    cur = os.getpid()
    if os.path.exists(lock):
        try:
            old = int((open(lock, encoding="utf-8").read().strip() or "0"))
        except Exception:
            old = 0
        if old and _pid_alive(old):
            print("[live_run] 拒绝重复启动：已有 live_run 引擎在运行 (PID %d)。" % old)
            return None
    with open(lock, "w", encoding="utf-8") as f:
        f.write(str(cur))

    def _release():
        try:
            if (os.path.exists(lock)
                    and open(lock, encoding="utf-8").read().strip() == str(cur)):
                os.remove(lock)
        except Exception:
            pass
    atexit.register(_release)
    return _release


from config.settings import SimulatorConfig, INITIAL_CAPITAL
from config.strategy import StrategyConfig
from core.simulator import Simulator
from strategy.agent import StrategyAgent
from strategy.learner import Learner
from market.westock_provider import WeStockProvider

WESTOCK_DEFAULT = "C:/Users/Administrator/.workbuddy/binaries/westock/westock.exe"


def resolve_westock() -> str:
    for cand in (
        os.environ.get("WESTOCK_BIN"),
        WESTOCK_DEFAULT,
        shutil.which("westock"),
    ):
        if cand and os.path.exists(cand):
            return cand
    raise RuntimeError(
        "未找到 westock CLI。请先安装 westock-data 技能："
        "node scripts/setup.cjs（或设置环境变量 WESTOCK_BIN 指向可执行文件）"
    )


def read_snapshots(data_dir: str) -> list:
    path = os.path.join(data_dir, "snapshots.jsonl")
    out = []
    if not os.path.exists(path):
        return out
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except Exception:
            pass
    return out


def summarize(sim, agent, day, data_dir) -> str:
    snap = sim.snapshot()
    total = snap.get("total_assets", 0.0)
    snaps = read_snapshots(data_dir)
    days = []
    for s in snaps:
        d = s.get("day")
        if d and (not days or days[-1][0] != d):
            days.append((d, s.get("total_assets", 0.0)))
    prev = days[-2][1] if len(days) >= 2 else INITIAL_CAPITAL
    daily_pnl = total - prev
    daily_pct = (daily_pnl / prev) if prev else 0.0
    cum_ret = (total - INITIAL_CAPITAL) / INITIAL_CAPITAL
    curve = [v for _, v in days if v]
    max_dd = 0.0
    peak = curve[0] if curve else 0.0
    for v in curve:
        if v > peak:
            peak = v
        if peak > 0:
            max_dd = max(max_dd, (peak - v) / peak)

    today_trades = [t for t in sim.account.trades
                    if (t.trading_day and t.trading_day.isoformat() == day.isoformat())]
    positions = sim.positions()

    L = []
    L.append("=" * 64)
    L.append(f"  stock-simulator 每日总结  {day.isoformat()}"
             f"  （实盘数据 / westock）")
    L.append("=" * 64)
    L.append(f"  总权益        : {total:,.2f} 元")
    L.append(f"  可用资金      : {snap.get('cash',0):,.2f} 元")
    L.append(f"  持仓总市值    : {snap.get('market_value',0):,.2f} 元")
    L.append(f"  当日盈亏      : {daily_pnl:+,.2f} 元  ({daily_pct:+.2%})")
    L.append(f"  累计收益率    : {cum_ret:+.2%}")
    L.append(f"  历史最大回撤  : {max_dd:.2%}")
    L.append(f"  当前总仓位    : {snap.get('market_value',0)/total:.2%}"
             if total else "  当前总仓位    : 0.00%")
    L.append(f"  智能体状态    : {agent.risk.mode.value}"
             if agent and getattr(agent, 'risk', None) else "（仅重算总结）")
    L.append("-" * 64)
    L.append(f"  持仓明细（{len(positions)} 只）：")
    if positions:
        for p in positions:
            mv = p.get('market_value', 0) or 0.0
            w = (mv / total) if total else 0.0
            L.append(f"    {p.get('code','')} {p.get('name','')} "
                     f"数量={p.get('quantity',0):,} 成本={p.get('cost_price',0):.3f} "
                     f"现价={p.get('current_price',0):.3f} 市值={mv:,.2f} "
                     f"浮盈={p.get('unrealized_pnl',0):+,.2f} "
                     f"({p.get('unrealized_pct',0):+.2%}) 权重={w:.2%}")
    else:
        L.append("    （无持仓）")
    L.append("-" * 64)
    L.append(f"  当日交易记录（{len(today_trades)} 笔）：")
    if today_trades:
        for t in today_trades:
            side = "买入" if str(t.side) == "Side.BUY" else "卖出"
            L.append(f"    {t.datetime} {side} {t.code} {t.name} "
                     f"价={t.price:.3f} 量={t.quantity:,} 额={t.amount:,.2f} "
                     f"费={t.total_fee:,.2f} 已实现={t.realized_pnl:+,.2f}")
    else:
        L.append("    （当日无成交）")
    L.append("=" * 64)
    return "\n".join(L)


def summary_only(day, data_dir) -> None:
    """仅重算当日总结：加载现有账户，刷新实时价，重写 last_summary.txt。

    不触发任何交易 / 不改动持仓。用于修复因开盘行情抖动导致现价写成 0 的总结，
    或进程被杀后补出当日总结。
    """
    cli = resolve_westock()
    print(f"[live_run] westock = {cli}")
    print(f"[live_run] --summary-only：重算 {day.isoformat()} 当日总结（不交易）")
    cfg = SimulatorConfig(provider="westock")
    market = WeStockProvider(cli=cli)
    sim = Simulator(cfg, market=market, storage=None)
    if not sim.load():
        print("[live_run] 无账户数据，无法生成总结。")
        return
    # 用最新实时价刷新快照（收盘后取当日收盘价），并落盘 live_prices.json
    try:
        sim.refresh_live_prices()
        market.save_cache()
    except Exception as e:
        print(f"[live_run] 实时价刷新异常（继续用兜底）: {e}")
    summary = summarize(sim, None, day, data_dir)
    print(summary)
    with open(os.path.join(data_dir, "last_summary.txt"), "w", encoding="utf-8") as f:
        f.write(summary + "\n")
    print(f"[live_run] 已重写 {day.isoformat()} 当日总结（data/last_summary.txt）。")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="忽略幂等标记强制重跑")
    ap.add_argument("--summary-only", action="store_true",
                    help="仅基于现有持仓+最新实时价重算当日总结，不做任何交易")
    ap.add_argument("--date", default="", help="指定交易日 YYYY-MM-DD（默认今天）")
    args = ap.parse_args()

    day = (date.fromisoformat(args.date) if args.date
           else date.today())
    data_dir = os.path.join(HERE, "data")

    # 并发锁：防止直接/重复调用 live_run 起多个引擎（重复引擎→重复交易）。
    if _acquire_run_lock(data_dir) is None:
        return

    # 运行日志落盘：stdout 同时镜像写入 data/live_run.log，便于观察「是否在运行/思考」
    try:
        _log_path = os.path.join(data_dir, "live_run.log")
        _logf = open(_log_path, "a", encoding="utf-8")

        class _Tee:
            def __init__(self, *streams):
                self._streams = streams

            def write(self, s):
                for st in self._streams:
                    try:
                        st.write(s)
                    except Exception:
                        pass
                return len(s)

            def flush(self):
                for st in self._streams:
                    try:
                        st.flush()
                    except Exception:
                        pass

        import sys as _sys
        _sys.stdout = _Tee(_sys.stdout, _logf)
    except Exception:
        pass

    if args.summary_only:
        return summary_only(day, data_dir)

    marker = os.path.join(data_dir, "last_run_day.txt")
    if not args.force and os.path.exists(marker):
        last = open(marker, encoding="utf-8").read().strip()
        if last == day.isoformat():
            print(f"[live_run] {day.isoformat()} 已运行，幂等跳过。"
                  f"如需重跑请加 --force。")
            return

    cli = resolve_westock()
    print(f"[live_run] westock = {cli}")
    print(f"[live_run] 运行交易日 {day.isoformat()} ...")

    cfg = SimulatorConfig(provider="westock")
    market = WeStockProvider(cli=cli)
    sim = Simulator(cfg, market=market, storage=None)
    # 加载历史账户（持仓/现金/盈亏），保证组合跨日持久化、自学习基于真实盈亏迭代
    if not sim.load():
        print(f"[live_run] 无历史账户，按初始本金 {cfg.initial_capital:,.0f} 元新建。")
    strat_cfg = StrategyConfig()
    agent = StrategyAgent(sim, strat_cfg, storage=sim.storage)
    learner = Learner(agent, storage=sim.storage)
    agent.attach_learner(learner)

    if not market.is_trading_day(day):
        print(f"[live_run] {day.isoformat()} 非交易日（周末/休市），跳过交易。")
        market.save_cache()
        open(marker, "w", encoding="utf-8").write(day.isoformat())
        return

    # 0) 预热 / 刷新全市场缓存（并行抓取，缓解高延迟与 429）
    #     基本面(fund) 仅在缺失 / 超 30 天时抓取；quote 每日刷新；
    #     close 近 6 天未更新才补抓。首跑建立全市场缓存，日常仅刷新行情。
    try:
        union = list(dict.fromkeys(
            market.get_index_constituents("sh000300")
            + market.get_index_constituents("sz399905")))
        print(f"[live_run] 预热全市场缓存（{len(union)} 只，并行）...")
        t0 = _time.time()
        n = market.warm_universe(union, max_workers=6)
        print(f"[live_run] 缓存预热完成 {n} 只（{_time.time()-t0:.0f}s）。")
    except Exception as e:
        print(f"[live_run] 缓存预热异常（忽略，将按需抓取）: {e}")

    # 成交时间戳：盘中手动运行（处于交易时段内）用真实当前时刻，更贴近实际；
    # 若为盘后/非交易时段运行则固定 15:00（当日收盘流程语义）。
    cur_t = datetime.now().time()
    if dtime(9, 30, 0) <= cur_t <= dtime(15, 0, 0):
        now = datetime.combine(day, cur_t)
    else:
        now = datetime.combine(day, dtime(15, 0, 0))
    # 1) 行情获取 → 交易决策 → 模拟撮合
    st = agent.daily_step(day, now=now)
    print(f"[live_run] daily_step: 模式={st['mode']} 净值={st['equity']/1e8:.4f}亿 "
          f"仓位={st['position_fraction']:.1%} 持仓={st['held']}")
    # 2) 收盘复盘 → 盈亏归因 → 策略优化（参数/因子/风格）
    rv = agent.review(day, now=now)
    regime = rv.get("regime")
    bt = rv.get("backtest")
    if regime:
        print(f"[live_run] review: 风格={regime} 因子权重="
              f"{agent.cfg.factor_weights}")
    if bt:
        print(f"[live_run] 回测优化: 止损{bt['combo'][0]:.0%}/"
              f"盈{bt['combo'][1]:.0%}&{bt['combo'][2]:.0%} "
              f"夏普{bt['sharpe']:.2f} 回撤{bt['mdd']:.2%} 胜率{bt['wr']:.1%}")

    # 落盘学习进度曲线 + 行情缓存
    try:
        learner.export_progress_chart(os.path.join(data_dir,
                                                    "learning_progress.html"))
    except Exception as e:
        print(f"[live_run] 学习曲线导出失败（忽略）: {e}")
    market.save_cache()
    sim.save()

    # 3) 当日总结
    summary = summarize(sim, agent, day, data_dir)
    print(summary)
    with open(os.path.join(data_dir, "last_summary.txt"), "w", encoding="utf-8") as f:
        f.write(summary + "\n")
    open(marker, "w", encoding="utf-8").write(day.isoformat())
    print(f"[live_run] 完成 {day.isoformat()}。看板数据已同步（data/）。")


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:
        import traceback
        traceback.print_exc()
        sys.exit(1)
    sys.exit(0)
