"""stock-simulator · 持续运行调度守护进程（量化交易专家调度）。

职责（满足「每个交易日持续运行、无需人工干预、看板实时同步」）：
  - 交易日 09:30–15:00：每 3s 刷新持仓实时价快照（live_prices.json），
    看板据此展示盘中实时盈亏，且只读不改写任何交易状态；刷新频率与看板
    重绘频率（交易时段 3s）对齐，消除展示价与成交判定价时效差。
  - 盘中实时决策（双层）：
      高频（每 ~10s，纯本地）：基于刚刷新的实时价扫描止损/止盈/持仓到期，
        命中即模拟卖出（不触网，毫秒级）。
      低频（每 5min）：调用 agent.intraday_step 跑完整决策（含选股开仓），
        与收盘 daily_step 同源但盘中不重复累计持仓周期、当日已交易集合跨调用
        保留以免疫信号闪烁重复开仓。选股器经并行预热，冷缓存下单次决策由数分钟
        级降至十几秒级。
  - 交易日 08:45（贴合开机窗口）：先以独立子进程限时采集盘前资讯
    （pre_market_news.py：政策要闻 / 公司公告 / 财经热文，失败不阻塞），
    再由 pre_market.py 汇总全市场快照的涨跌分布 / 异动 / 行业强弱（纯本地、零网络），
    落盘 data/pre_market.json 供看板「盘前速览」展示。
  - 盘前检索按「交易日一次」而非「固定时刻一次」：调度器无论在 08:45、09:10
    还是 10:00 才启动（开机晚），启动后都立即补做当日盘前检索，绝不会漏一天。
  - 交易日 15:35（收盘后）：调用 live_run.py 执行当日完整流程
    「行情获取 → 交易决策 → 模拟撮合 → 收盘复盘 → 策略优化」，
    落盘本地 data/ 并输出当日总结。
  - 非交易日：休眠至下一交易日 08:45 再评估。
  - 进程异常自动重启（外层 while + try/except），保证长期存活。

用法：
  python scheduler.py            # 前台常驻（建议后台静默启动）
  python scheduler.py --once     # 仅执行一次当日收盘流程后退出（调试用）

说明：
  - live_run.py 自带幂等标记（last_run_day.txt），同日重复触发不会重复交易。
  - 看板（dashboard.py）独立进程，本守护进程不影响其运行。
"""
import atexit
import os
import sys
import time
import socket
import subprocess
from datetime import datetime, time as dtime, date, timedelta
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)


# 单实例端口：用 TCP 绑定做进程级互斥，比 PID 文件更可靠，且免疫
# 「PID 被系统回收给无关进程」造成的假阳性（曾导致调度器误以为自己已运行而
# 拒绝启动，或双开两引擎并发写 data/ 重复成交）。dashboard 用 8501，这里用 8502。
SCHEDULER_SINGLE_PORT = 8502
_SCHED_SOCK = [None]


def _acquire_lock() -> bool:
    """单实例锁：双开调度器会对同一 data/ 并发写（重复成交、状态互踩）。

    优先用 TCP 端口绑定（127.0.0.1:SCHEDULER_SINGLE_PORT）做进程级互斥——
    任何平台都可靠、且不受 PID 回收影响；PID 文件仅作人类排查用途。
    """
    lock = Path(HERE) / "data" / "scheduler.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    # 1) 端口级互斥（权威）：绑定失败即说明已有实例在跑，本次启动退出
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    # SO_REUSEADDR=1：允许在上一实例被强杀后的 TIME_WAIT 残留期内重新绑定，
    # 避免「误判已有实例而退出、调度器起不来」。对正在 LISTEN 的端口 bind 仍会失败，单实例不变。
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind(("127.0.0.1", SCHEDULER_SINGLE_PORT))
        sock.listen(1)
    except OSError:
        print(f"[scheduler] 检测到已有实例在运行（端口 {SCHEDULER_SINGLE_PORT} 被占用），"
              f"本次启动退出。", flush=True)
        return False
    _SCHED_SOCK[0] = sock
    # 2) 兼容：写 PID 文件便于人类排查
    try:
        lock.write_text(str(os.getpid()), encoding="utf-8")
    except Exception:
        pass

    def _release():
        try:
            if _SCHED_SOCK[0] is not None:
                _SCHED_SOCK[0].close()
        except Exception:
            pass
        try:
            if lock.exists() and lock.read_text(encoding="utf-8").strip() == str(os.getpid()):
                lock.unlink()
        except Exception:
            pass
    atexit.register(_release)
    return True

from market.westock_provider import WeStockProvider

WESTOCK_DEFAULT = "C:/Users/Administrator/.workbuddy/binaries/westock/westock.exe"
PY = "C:/Users/Administrator/.workbuddy/binaries/python/versions/3.13.12/python.exe"
LIVE_RUN = os.path.join(HERE, "live_run.py")
PRE_MARKET = os.path.join(HERE, "pre_market.py")
PRE_MARKET_NEWS = os.path.join(HERE, "pre_market_news.py")
SCHED_LOG = os.path.join(HERE, "data", "scheduler.log")

INTRADAY_START = dtime(9, 30, 0)
INTRADAY_END = dtime(15, 0, 0)
CLOSE_RUN = dtime(15, 35, 0)
# 08:45：用户的电脑一般 08:30–08:45 才开机，定时设太早没有意义；
# 真正保证不漏跑的是「启动即补做」状态机（见 main），这里只是「最早几点开始」。
# A 股 09:15 才有集合竞价，盘前行情检索仍走本地全市场快照（零网络）。
PRE_MARKET_START = dtime(8, 45, 0)   # 盘前信息检索最早开始时刻（交易日开盘前）
NEWS_CUTOFF = dtime(9, 20, 0)        # 晚于此时补做则跳过资讯联网采集，避免拖累 09:30
NEWS_TIMEOUT = 120                   # 资讯采集子进程硬超时（秒）
REFRESH_SEC = 3                       # 盘中快照刷新频率（与看板重绘对齐，消除时效性偏差）
EXIT_CHECK_SEC = 10                   # 盘中高频退出监控间隔（纯本地，实时价来自刚刷新的 live_prices.json）
INTRADAY_DECISION_SEC = 300           # 盘中低频完整决策间隔（含选股开仓，默认 5 分钟）


def log(msg: str) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    try:
        os.makedirs(os.path.dirname(SCHED_LOG), exist_ok=True)
        with open(SCHED_LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def make_market():
    cli = WESTOCK_DEFAULT if os.path.exists(WESTOCK_DEFAULT) else "westock"
    return WeStockProvider(cli=cli)


_refresh_count = 0


def refresh_intraday(market: WeStockProvider) -> None:
    """盘中每 3s 刷新持仓实时价（看板用），不触发任何交易。

    增强：刷新计数 + 心跳日志；持仓非空却 0 只成功刷新时告警（源可能异常）；
    捕获连接重置(10054)等异常并标记断连告警后继续，不退出主循环（自愈）。
    """
    global _refresh_count
    try:
        from config.settings import SimulatorConfig
        from core.simulator import Simulator
        sim = Simulator(SimulatorConfig(provider="westock"), market=market)
        if sim.load():
            data = sim.refresh_live_prices()
            n = len([k for k in data if k != "_meta"])
            _refresh_count += 1
            if n == 0:
                if len(sim.account.positions) > 0:
                    log("盘中实时价刷新：持仓存在但 0 只成功刷新"
                        "（源可能异常），已沿用上次有效价")
                elif _refresh_count % 10 == 0:
                    log(f"心跳：盘中刷新第 {_refresh_count} 次（当前无持仓，等待建仓）")
            else:
                if _refresh_count % 10 == 0:
                    log(f"心跳：盘中刷新第 {_refresh_count} 次，持仓 {n} 只实时价已更新")
                else:
                    log(f"盘中实时价刷新：{n} 只持仓")
    except Exception as e:
        msg = str(e)
        tag = " [连接重置/断连]" if any(
            k in msg for k in ("10054", "远程主机", "Connection", "reset", "ECONNRESET")
        ) else ""
        log(f"盘中刷新异常（自愈重试）{tag}: {msg[:200]}")
    try:
        market.save_cache()
    except Exception:
        pass


_intraday_engine = None


def get_intraday_engine(market: WeStockProvider):
    """懒加载并会话级复用盘中实时决策引擎（sim+agent+learner+storage 同实例）。

    引擎在首个盘中 tick 构建一次，后续 tick 复用同一内存实例，保证 pos_state /
    当日已交易集合 / 风控状态跨调用连续；其落盘与看板/收盘流程共用 data/ 目录。
    """
    global _intraday_engine
    if _intraday_engine is not None:
        return _intraday_engine
    try:
        from config.settings import SimulatorConfig
        from config.strategy import StrategyConfig
        from core.simulator import Simulator
        from strategy.agent import StrategyAgent
        from strategy.learner import Learner
        cfg = SimulatorConfig(provider="westock")
        sim = Simulator(cfg, market=market)   # storage 默认 LocalStore（落盘 data/）
        if not sim.load():
            log("盘中引擎：无历史账户，按初始本金新建。")
        # 盘前先把账户推进到当日：引擎从磁盘恢复的 current_day 仍是上一交易日，
        # 若等到 intraday_step 才 set_day，开盘后首批成交会被打上「上一交易日」的
        # trading_day，且昨日买入的持仓在 09:30 时仍不可卖（T+1 误判）。
        try:
            sim.set_day(date.today())
        except Exception as e:
            log(f"盘中引擎推进交易日失败（忽略）: {e}")
        agent = StrategyAgent(sim, StrategyConfig(), storage=sim.storage)
        learner = Learner(agent, storage=sim.storage)
        agent.attach_learner(learner)
        agent._daily_traded_day = date.today().isoformat()
        # 注：全市场 fund/close 缓存由每日 15:35 的 live_run.warm_universe 预热，
        # 选股标的缓存由 Screener._warm 在 build() 内按需并行补齐；此处不串行/阻塞预热，
        # 保证引擎加载与盘中循环永不被重网络初始化拖垮。
        _intraday_engine = (agent, sim)
        log(f"盘中实时决策引擎已加载（持仓 {len(sim.account.positions)} 只，"
            f"风控 {agent.risk.mode.value}）。")
    except Exception as e:
        log(f"盘中引擎加载失败（暂不启用实时决策）: {e}")
        return (None, None)
    return _intraday_engine


def run_intraday_exit(agent) -> int:
    """高频退出监控：读刚刷新的 live_prices.json（实时价），纯本地扫描并即时卖出。"""
    now = datetime.now()
    live = {}
    try:
        live = (agent.sim.storage.load_live_prices() or {}) if agent.sim.storage else {}
    except Exception:
        live = {}
    prices = {c: (live.get(c) or {}).get("price", 0.0)
              for c in agent.sim.account.positions}
    return agent.intraday_monitor(now, prices)


def run_intraday_decision(agent, market: WeStockProvider) -> dict:
    """低频完整决策（含选股开仓）；委托 agent.intraday_step。"""
    day = date.today()
    now = datetime.now()
    return agent.intraday_step(day, now)


def run_daily(market: WeStockProvider) -> None:
    """收盘后触发完整当日流程（live_run.py 自带幂等）。"""
    log("触发当日收盘流程：python live_run.py")
    try:
        r = subprocess.run([PY, LIVE_RUN], cwd=HERE,
                            capture_output=False, text=True, timeout=3600)
        log(f"live_run 退出码={r.returncode}")
    except Exception as e:
        log(f"live_run 调用失败: {e}")
    try:
        market.save_cache()
    except Exception:
        pass


def run_pre_market(market: WeStockProvider, force: bool = False) -> None:
    """盘前信息检索（全市场快照汇总、研判风格、生成盘前摘要）。脚本自带按日幂等。"""
    cmd = [PY, PRE_MARKET]
    if force:
        cmd.append("--force")
    log(f"触发盘前信息检索（全市场）：python pre_market.py{' --force' if force else ''}")
    try:
        r = subprocess.run(cmd, cwd=HERE,
                            capture_output=False, text=True, timeout=1800)
        log(f"pre_market 退出码={r.returncode}")
    except Exception as e:
        log(f"pre_market 调用失败: {e}")


def run_pre_market_news() -> None:
    """盘前资讯采集：独立子进程 + 硬超时，任何失败/超时都不影响 09:30 开盘。"""
    if datetime.now().time() >= NEWS_CUTOFF:
        log("临近/已过开盘，跳过盘前资讯联网采集（避免拖累 09:30），沿用本地资讯缓存")
        return
    log("触发盘前资讯采集（政策/公告/热文）：python pre_market_news.py")
    try:
        r = subprocess.run([PY, PRE_MARKET_NEWS], cwd=HERE,
                            capture_output=False, text=True, timeout=NEWS_TIMEOUT)
        log(f"pre_market_news 退出码={r.returncode}")
    except Exception as e:
        log(f"pre_market_news 调用失败或超时（忽略，不影响开盘）: {e}")


def pre_market_action(now: datetime, trading_day: date, pre_done_day) -> str:
    """盘前阶段状态机（纯函数，便于单测覆盖「开机晚」场景）。
      sleep —— 未到 08:45，等到点再跑；
      run   —— 到点即跑；开机晚（含 09:30 之后才开机）同样是 run，保证不漏一天；
      skip  —— 本交易日已完成。
    """
    if pre_done_day == trading_day:
        return "skip"
    if now.time() < PRE_MARKET_START:
        return "sleep"
    return "run"


def sleep_until(target: datetime) -> None:
    secs = (target - datetime.now()).total_seconds()
    if secs <= 0:
        return
    # 单次睡眠封顶 12h，到点后重新评估，避免长睡期间时钟/状态漂移
    while secs > 0:
        chunk = min(secs, 12 * 3600)
        time.sleep(chunk)
        secs = (target - datetime.now()).total_seconds()


def next_run_time(market: WeStockProvider, today: date) -> datetime:
    if market.is_trading_day(today):
        return datetime.combine(today, CLOSE_RUN)
    nd = market.next_trading_day(today)
    return datetime.combine(nd, PRE_MARKET_START)


def main(once: bool = False) -> None:
    market = make_market()
    log("调度守护进程启动（实盘数据 / westock）。")
    pre_done_day = None   # 已完成盘前检索的交易日（进程内状态，主循环异常重试不重置）
    while True:
        try:
            now = datetime.now()
            today = now.date()
            if market.is_trading_day(today):
                # —— 盘前：按「交易日一次」做，开机晚也启动即补做 ——
                action = pre_market_action(datetime.now(), today, pre_done_day)
                if action == "sleep":
                    log(f"盘前准备：休眠至 {today} "
                        f"{PRE_MARKET_START.strftime('%H:%M')} 触发全市场盘前检索")
                    sleep_until(datetime.combine(today, PRE_MARKET_START))
                    continue
                if action == "run":
                    if datetime.now().time() >= INTRADAY_START:
                        log("开机较晚（已过开盘时刻），立即补做当日盘前检索"
                            "（基准以实际快照为准，不虚构盘前行情）")
                    try:
                        run_pre_market_news()
                    except Exception as e:
                        log(f"盘前资讯采集异常（忽略）: {e}")
                    try:
                        run_pre_market(market)
                    except Exception as e:
                        log(f"盘前检索异常（忽略）: {e}")
                    pre_done_day = today
                    if datetime.now().time() < INTRADAY_START:
                        log(f"盘前检索完成，休眠至 {today} "
                            f"{INTRADAY_START.strftime('%H:%M')}（等待开盘）")
                        sleep_until(datetime.combine(today, INTRADAY_START))
                        continue
                # 盘中实时价刷新（仅 09:30–15:00）+ 双层实时决策
                last_decision = datetime.min   # 开盘即触发一次完整决策
                while (INTRADAY_START <= datetime.now().time() <= INTRADAY_END
                       and market.is_trading_day(datetime.now().date())):
                    refresh_intraday(market)
                    # —— 高频（每 ~10s）：纯本地退出监控，实时价来自刚刷新的 live_prices.json ——
                    try:
                        agent, _ = get_intraday_engine(market)
                        if agent is not None:
                            n = run_intraday_exit(agent)
                            if n:
                                log(f"盘中实时退出触发 {n} 笔（已落盘）")
                    except Exception as e:
                        log(f"盘中退出监控异常（忽略）: {e}")
                    # —— 低频（每 5min）：完整决策（含选股开仓）——
                    if (datetime.now() - last_decision).total_seconds() >= INTRADAY_DECISION_SEC:
                        try:
                            agent, _ = get_intraday_engine(market)
                            if agent is not None:
                                st = run_intraday_decision(agent, market)
                                log(f"盘中决策: 模式={st['mode']} 净值={st['equity']:.0f} "
                                    f"仓位={st['position_fraction']:.1%} 持仓={st['held']}")
                                last_decision = datetime.now()
                        except Exception as e:
                            log(f"盘中决策异常（忽略）: {e}")
                            last_decision = datetime.now()
                    time.sleep(REFRESH_SEC)
                # 收盘边界收尾：盘中循环退出（>=15:00）后补一次快照刷新，
                # 确保 15:00 最后一笔交易行情被落盘，避免 15:00–15:35 空窗丢失末值；
                # 该刷新与后续 15:35 收盘流程（live_run）按依赖顺序串行：先落盘末值，后收盘。
                try:
                    refresh_intraday(market)
                except Exception as e:
                    log(f"收盘边界补刷异常（忽略）: {e}")
                # 收盘流程（15:35 后）
                if datetime.now().time() >= CLOSE_RUN:
                    run_daily(market)
                    if once:
                        log("单次模式：当日流程已完成，退出。")
                        return
                    # 休眠至下一交易日 08:45（盘前检索）
                    nd = market.next_trading_day(today)
                    sleep_until(datetime.combine(nd, PRE_MARKET_START))
                else:
                    # 刚过 15:00 但不到 15:35：短等
                    sleep_until(datetime.combine(today, CLOSE_RUN))
            else:
                nd = market.next_trading_day(today)
                log(f"非交易日，休眠至 {nd} "
                    f"{PRE_MARKET_START.strftime('%H:%M')}。")
                sleep_until(datetime.combine(nd, PRE_MARKET_START))
        except Exception as e:
            log(f"主循环异常（10s 后重试）: {e}")
            time.sleep(10)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="仅执行一次当日收盘流程后退出")
    args = ap.parse_args()
    if not _acquire_lock():
        sys.exit(0)
    main(once=args.once)
