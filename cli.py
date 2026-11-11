"""A股模拟炒股 —— 命令行交互。

子命令：
  demo                运行演示场景
  status              查看账户快照与持仓
  buy  <code> <qty> [price]   买入（price 省略为市价）
  sell <code> <qty> [price]   卖出
  next-day <YYYY-MM-DD>        切换到下一交易日（释放 T+1）
  reset               清空本地数据，重置账户

示例：
  python cli.py demo
  python cli.py buy sh600519 1000
  python cli.py sell sh600519 1000 1700
  python cli.py status
"""
import sys
import os
import argparse
from datetime import date, datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config.settings import SimulatorConfig
from config.strategy import StrategyConfig
from core.simulator import Simulator
from core.order import OrderType
from strategy.agent import StrategyAgent
from strategy.learner import Learner
from datetime import date as _date


def _sim(provider: str) -> Simulator:
    sim = Simulator(SimulatorConfig(provider=provider))
    sim.load()
    return sim


def _intraday_now() -> datetime:
    """模拟盘中时刻（CLI 演示用，确保落在交易时段内）。"""
    return datetime.now().replace(hour=10, minute=0, second=0, microsecond=0)


def cmd_demo(args):
    from main import run_demo
    run_demo(args.provider)


def cmd_status(args):
    sim = _sim(args.provider)
    print("--- 资产快照 ---")
    for k, v in sim.snapshot().items():
        print(f"  {k}: {v}")
    print("--- 持仓 ---")
    for p in sim.positions():
        print(" ", p)


def cmd_buy(args):
    sim = _sim(args.provider)
    if sim.account.current_day is None:
        sim.set_day(date.today())
    o = sim.buy(args.code, args.qty,
                OrderType.LIMIT if args.price else OrderType.MARKET,
                args.price or 0.0, _intraday_now())
    print(f"买入 {args.code} {args.qty} -> {o.status.value} {o.reject_reason}")
    sim.save()


def cmd_sell(args):
    sim = _sim(args.provider)
    if sim.account.current_day is None:
        sim.set_day(date.today())
    o = sim.sell(args.code, args.qty,
                 OrderType.LIMIT if args.price else OrderType.MARKET,
                 args.price or 0.0, _intraday_now())
    print(f"卖出 {args.code} {args.qty} -> {o.status.value} {o.reject_reason}")
    sim.save()


def cmd_next_day(args):
    sim = _sim(args.provider)
    sim.set_day(date.fromisoformat(args.day))
    print(f"已切换到交易日 {args.day}（T+1 持仓已释放）")
    sim.save()


def cmd_reset(args):
    sim = _sim(args.provider)
    sim.reset()
    print("已重置账户，本地数据清空。")


def cmd_screen(args):
    sim = _sim(args.provider)
    agent = StrategyAgent(sim, StrategyConfig(), storage=sim.storage)
    rep = agent.screen_report()
    print(f"多因子选股通过 {rep['count']} 只：")
    for c in rep["candidates"]:
        f = c["factors"]
        print(f"  {c['code']} {c['name']:<8} {c['industry']:<4} "
              f"得分={c['score']:.3f} 权重={c['target_weight']:.0%} "
              f"[质{f['quality']:.2f} 估{f['valuation']:.2f} "
              f"波{f['low_vol']:.2f} 动{f['momentum']:.2f}]")


def cmd_strategy(args):
    sim = _sim(args.provider)
    agent = StrategyAgent(sim, StrategyConfig(), storage=sim.storage)
    day = sim.account.current_day or _date.today()
    for i in range(args.days):
        if i > 0:
            day = sim.market.next_trading_day(day)
        st = agent.daily_step(day)
        if i % 5 == 0 or st["mode"] != "NORMAL":
            print(f"  {day} 模式={st['mode']:<14} 净值={st['equity']/1e8:.4f}亿 "
                  f"仓位={st['position_fraction']:.1%} 持仓={st['held']}")
    sim.save()
    print("策略调度完成，状态已保存。")


def cmd_learn(args):
    from main_strategy import run_learning
    run_learning(args.provider, args.days)


def cmd_progress(args):
    sim = _sim(args.provider)
    agent = StrategyAgent(sim, StrategyConfig(), storage=sim.storage)
    learner = Learner(agent, storage=sim.storage)
    agent.attach_learner(learner)
    path = learner.export_progress_chart("data/learning_progress.html")
    print(f"学习进度曲线已导出：{path}")


def main():
    p = argparse.ArgumentParser(description="A股模拟炒股 CLI")
    p.add_argument("--provider", default=os.environ.get("SIM_PROVIDER", "westock"),
                   choices=["westock", "mock"], help="行情数据源")
    sub = p.add_subparsers(dest="cmd")

    sub.add_parser("demo").set_defaults(func=cmd_demo)
    sub.add_parser("status").set_defaults(func=cmd_status)
    sub.add_parser("reset").set_defaults(func=cmd_reset)

    ps = sub.add_parser("screen", help="多因子选股排序")
    ps.set_defaults(func=cmd_screen)
    pst = sub.add_parser("strategy", help="运行策略调度 N 个交易日")
    pst.add_argument("--days", type=int, default=20)
    pst.set_defaults(func=cmd_strategy)

    pl = sub.add_parser("learn", help="自学习进化演示（每日15:30复盘+进度曲线）")
    pl.add_argument("--days", type=int, default=30)
    pl.set_defaults(func=cmd_learn)
    pp = sub.add_parser("progress", help="由已存进度导出学习曲线 HTML")
    pp.set_defaults(func=cmd_progress)

    pb = sub.add_parser("buy"); pb.add_argument("code"); pb.add_argument("qty", type=int)
    pb.add_argument("price", type=float, nargs="?"); pb.set_defaults(func=cmd_buy)

    ps = sub.add_parser("sell"); ps.add_argument("code"); ps.add_argument("qty", type=int)
    ps.add_argument("price", type=float, nargs="?"); ps.set_defaults(func=cmd_sell)

    pn = sub.add_parser("next-day"); pn.add_argument("day"); pn.set_defaults(func=cmd_next_day)

    args = p.parse_args()
    if not getattr(args, "cmd", None):
        p.print_help()
        return
    # demo 子命令也需要 provider 参数透传
    if args.cmd == "demo":
        args.provider = args.provider
    args.func(args)


if __name__ == "__main__":
    main()
