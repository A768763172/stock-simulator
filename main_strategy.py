"""稳定复利策略演示入口。

运行：
  python main_strategy.py                 # 默认 westock（未装回退 mock）
  python main_strategy.py mock            # 强制离线演示
  SIM_PROVIDER=mock python main_strategy.py

演示覆盖：选股池过滤（板块/ST/基本面剔除）→ 多因子排序 → 分批建仓 →
止盈止损 → 回撤风控铁律。所有动作经撮合引擎，受 T+1/整手/涨跌幅/手续费约束。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from datetime import date, datetime, timedelta

from config.settings import SimulatorConfig
from config.strategy import StrategyConfig
from core.simulator import Simulator
from strategy.stock_pool import StockPool
from strategy.agent import StrategyAgent
from strategy.learner import Learner


def _print_pool_rejections(market) -> None:
    pool = StockPool(StrategyConfig(), market)
    print("\n--- 选股池过滤样例（展示剔除原因） ---")
    for code in ["sh688981", "sz300750", "bj830799", "sh600868",
                 "sz000980", "sz000725", "sh600516", "sh601988", "sh600519"]:
        r = pool.screen_code(code)
        tag = "通过" if r["passed"] else "剔除"
        reasons = "；".join(r["reasons"]) or "—"
        print(f"  [{tag}] {code} {r['name']:<8} {reasons}")


def _perturb(sim, rng) -> None:
    """对全市场施加温和随机波动（±2%/日），使止盈止损与学习归因可被触发。"""
    for code in sim.market.get_universe():
        try:
            p = sim.market.get_quote(code).price
            p2 = max(1.0, p * (1 + rng.uniform(-0.02, 0.02)))
            sim.market.set_price(code, round(p2, 3), prev_close=round(p, 3))
        except Exception:
            pass


def run(provider: str = "westock", days: int = 30) -> None:
    cfg = SimulatorConfig(provider=provider)
    sim = Simulator(cfg)
    strat_cfg = StrategyConfig()
    agent = StrategyAgent(sim, strat_cfg, storage=sim.storage)

    print("=" * 60)
    print(" 稳定复利策略演示   初始本金：5.00 万元 (50000 元)   (provider=%s)" % provider)
    print("=" * 60)

    # 1) 选股池过滤样例
    _print_pool_rejections(sim.market)

    # 2) 多因子选股排序（前若干）
    rep = agent.screen_report()
    print(f"\n--- 多因子选股：通过 {rep['count']} 只，目标组合 ---")
    for c in rep["candidates"][:12]:
        f = c["factors"]
        print(f"  {c['code']} {c['name']:<8} 行业={c['industry']:<4} "
              f"得分={c['score']:.3f} 权重={c['target_weight']:.0%} "
              f"[质{f['quality']:.2f} 估{f['valuation']:.2f} "
              f"波{f['low_vol']:.2f} 动{f['momentum']:.2f}]")

    # 3) 多日调度（每日对全市场施加温和随机波动，使止盈/止损可被触发）
    import random
    rng = random.Random(20260921)
    day = date(2026, 9, 21)
    print(f"\n--- 模拟 {days} 个交易日调度 ---")
    for i in range(days):
        if i == 0:
            cur = day
        else:
            cur = sim.market.next_trading_day(cur)
        _perturb(sim, rng)
        st = agent.daily_step(cur)
        if i % 5 == 0 or st["mode"] != "NORMAL":
            print(f"  {cur} 模式={st['mode']:<14} 净值={st['equity']/1e8:.4f}亿 "
                  f"仓位={st['position_fraction']:.1%} 持仓={st['held']} "
                  f"周新开={st['new_opens_week']} 周交易={st['trades_week']}")

    # 4) 终态
    print("\n--- 最终账户 ---")
    for k, v in sim.snapshot().items():
        print(f"  {k}: {v}")
    print("\n--- 持仓明细 ---")
    for p in sim.positions():
        print(" ", p)
    sim.save()
    print("\n数据已落盘本地 data/（含 strategy_state.json），未上传任何服务器。")


def run_learning(provider: str = "westock", days: int = 30) -> None:
    """自学习进化演示：每日交易 + 15:30 自动复盘进化 + 学习进度曲线。"""
    import random
    cfg = SimulatorConfig(provider=provider)
    sim = Simulator(cfg)
    strat_cfg = StrategyConfig()
    agent = StrategyAgent(sim, strat_cfg, storage=sim.storage)
    learner = Learner(agent, storage=sim.storage)
    agent.attach_learner(learner)

    print("=" * 64)
    print(" 交易智能体 · 自学习进化演示   初始本金：5.00 万元 (50000 元)   (provider=%s)" % provider)
    print("=" * 64)
    _print_pool_rejections(sim.market)
    rep = agent.screen_report()
    print(f"\n--- 多因子选股：通过 {rep['count']} 只，目标组合 ---")
    for c in rep["candidates"][:12]:
        f = c["factors"]
        print(f"  {c['code']} {c['name']:<8} 行业={c['industry']:<4} "
              f"得分={c['score']:.3f} 权重={c['target_weight']:.0%} "
              f"[质{f['quality']:.2f} 估{f['valuation']:.2f} "
              f"波{f['low_vol']:.2f} 动{f['momentum']:.2f}]")

    rng = random.Random(20260921)
    day = date(2026, 9, 21)
    print(f"\n--- 模拟 {days} 个交易日（每日 15:30 自动复盘进化） ---")
    for i in range(days):
        cur = day if i == 0 else sim.market.next_trading_day(cur)
        _perturb(sim, rng)
        st = agent.daily_step(cur)
        rv = agent.review(cur)            # 收盘后 15:30 复盘
        fw = rv.get("progress", {}).get("factor_weights", {})
        bt = rv.get("backtest")
        bt_s = (f" 回测→止损{bt['combo'][0]:.0%}/盈{bt['combo'][1]:.0%}"
                f"/{bt['combo'][2]:.0%} 夏普{bt['sharpe']:.2f}"
                if bt else "")
        if i % 5 == 0 or st["mode"] != "NORMAL" or bt:
            print(f"  {cur} 风格={rv.get('regime','-'):<11} 模式={st['mode']:<14} "
                  f"净值={st['equity']/1e8:.4f}亿 仓位={st['position_fraction']:.1%} "
                  f"因子[质{fw.get('quality',0):.2f} 估{fw.get('valuation',0):.2f} "
                  f"波{fw.get('low_vol',0):.2f} 动{fw.get('momentum',0):.2f}]{bt_s}")
        # 盈亏归因摘要（有当日交易时）
        for r in rv.get("reviews", []):
            print(f"     复盘 {r['code']} {r['reason']:<12} "
                  f"盈亏={r['pnl_pct']:+.2%} 归因={r['primary']}")

    print("\n--- 最终账户 ---")
    for k, v in sim.snapshot().items():
        print(f"  {k}: {v}")
    chart = learner.export_progress_chart("data/learning_progress.html")
    print(f"\n学习进度曲线已生成：{chart}（收益/回撤/夏普 + 因子权重演化）")
    print("优化后的参数已写入 data/learned_params.json，下次交易自动加载。")
    sim.save()


if __name__ == "__main__":
    provider = os.environ.get("SIM_PROVIDER", "westock")
    args = sys.argv[1:]
    if args and args[0] in ("learn", "learning"):
        provider = args[1] if len(args) > 1 else provider
        run_learning(provider)
    else:
        if args:
            provider = args[0]
        run(provider)
