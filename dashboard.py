"""交易智能体 · 本地实时交易监控看板（Streamlit）。

设计原则（量化交易专家 + 数据可视化专家协同）：
- 完全只读消费引擎落盘的本地文件（data/ 下的 JSON / JSONL），
  不调用任何撮合/下单接口，因此**不会影响智能体正常交易**。
- 数据实时同步：引擎每笔成交/每日调度都会 save() 落盘，调度守护交易时段每 3s 刷新快照，
  看板轮询读取，刷新频率与重绘频率对齐（交易时段 3s），消除展示价与成交判定价时效差。
- 当前价来自 live_prices.json（引擎 save 时写入，与智能体同源）；
  行情快照为空 / 异常时**显式提示「行情数据未就绪」**，绝不回退虚构演示价（杜绝两套数据）。
- 每项展示价旁携带 updated_at 时间戳与 source 数据源状态；涨跌停 / 停牌状态显式标识。
- 刷新规则：交易时段 3s；非交易时段 1min；休市自动切换"收盘总结"视图。
- 支持回看任意历史交易日的账户数据与成交记录。

运行：
    streamlit run dashboard.py --server.port 8501
"""
import json
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

import streamlit as st
from market.display import resolve_display_price, market_phase_label

# ---------- 路径 ----------
ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"

st.set_page_config(page_title="A股模拟交易 · 实时看板", layout="wide",
                   initial_sidebar_state="expanded")

# ---------- 颜色（中国习惯：涨红跌绿；成交买绿卖红按需求） ----------
C_BUY = "#1a7a3a"      # 买入标注：绿
C_SELL = "#c0392b"     # 卖出标注：红
C_UP = "#c0392b"       # 盈利/上涨：红
C_DOWN = "#1a7a3a"     # 亏损/下跌：绿
C_BG_HI = "#fff6cc"    # 最新成交高亮
C_MUTED = "#718096"


# ============ 数据读取（只读） ============
def load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def load_jsonl(path: Path) -> List[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except Exception:
                pass
    return out


def current_price(code: str, live: dict) -> Optional[float]:
    """返回展示价；快照为空 / 异常价返回 None（由调用方显式提示「行情未就绪」）。

    严禁回退 MockProvider 静态价，避免展示价与成交判定价出现两套数据偏差。
    """
    return resolve_display_price(live, code)["price"]


# ============ 计算 ============
def calc_holding_days(open_date: str, ref_day: str) -> Optional[int]:
    """持仓天数（含首日与末日的自然日口径，故 +1）。

    open_date / ref_day 取 YYYY-MM-DD（或带时分秒的整串，仅取前 10 位）。
    建仓日即 ref_day 时为 1 天；跨日持有按自然日差 +1。返回 None 表示无法计算。
    """
    if not open_date or not ref_day:
        return None
    try:
        d0 = datetime.strptime(str(open_date)[:10], "%Y-%m-%d").date()
        d1 = datetime.strptime(str(ref_day)[:10], "%Y-%m-%d").date()
    except Exception:
        return None
    return (d1 - d0).days + 1


def first_buy_by_code(trades: List[dict], ref_day: str = None) -> Dict[str, str]:
    """每只标的的建仓日（首次买入日期 YYYY-MM-DD）。

    成交流水中取该标的最早一笔 BUY 的日期。ref_day 给定时只统计该日及之前的
    买入（历史回看时，建仓日不应超过所选交易日）。
    """
    out: Dict[str, str] = {}
    for t in trades:
        if t.get("side") != "BUY":
            continue
        c = t.get("code")
        if not c:
            continue
        d = str(t.get("datetime", ""))[:10]
        if not d:
            continue
        if ref_day and d > ref_day:
            continue
        if c not in out or d < out[c]:
            out[c] = d
    return out


def compute_overview(account: dict, live: dict, snaps: List[dict],
                      learned: dict, strat: dict,
                      live_mode: bool = True) -> dict:
    snaps = [s for s in snaps if s.get("day")]
    latest = snaps[-1] if snaps else {}
    # 当日盈亏的基准：前一交易日收盘总权益。从全部快照倒序找第一个与参考日
    # 不同的交易日的收盘 total_assets（容错：跳过无 total_assets 的占位快照）。
    # 注意：当日盈亏必须在「实时/历史」分支内、用与「总权益」同源的 total_assets
    # 计算，否则快照口径与实时口径对不上，会出现「累计正、当日负」的矛盾。
    def _prev_close(ref_day):
        for s in reversed(snaps):
            d = s.get("day")
            if d and d != ref_day:
                ta = s.get("total_assets")
                if ta:
                    return ta
        return None

    # 历史最大回撤（基于全部快照总资产曲线）
    curve = [s.get("total_assets", 0.0) for s in snaps if s.get("total_assets")]
    max_dd = 0.0
    peak = curve[0] if curve else 0.0
    for v in curve:
        if v > peak:
            peak = v
        if peak > 0:
            max_dd = max(max_dd, (peak - v) / peak)

    if live_mode:
        # 实时口径：账户 + 实时行情快照（与③持仓明细同源），
        # 不使用历史收盘快照的市值，杜绝盘中「概览与持仓两套数据」。
        cash = account.get("cash", 0.0) or 0.0
        frozen = account.get("frozen_cash", 0.0) or 0.0
        mv = 0.0
        cost_amt = 0.0
        for code, p in (account.get("positions") or {}).items():
            qty = p.get("quantity", 0) or 0
            mv += (current_price(code, live) or 0.0) * qty
            cost_amt += (p.get("cost_price", 0.0) or 0.0) * qty
        total_assets = cash + frozen + mv
        init_cap = account.get("initial_capital", 0.0) or 0.0
        # 总盈亏（净）= 总权益 - 期初：现金已含全部手续费，此口径包含手续费，
        # 与「总权益」「累计收益率」勾稽一致（修复此前漏算买入费：旧口径用
        # realized + (mv - cost_amt)，cost_price 不含买费，导致少计买入费）。
        total_pnl = total_assets - init_cap
        total_return_pct = (total_pnl / init_cap * 100.0) if init_cap else 0.0
        current_day = account.get("current_day") or latest.get("day")
        # 实时口径：当日盈亏 = 实时总权益 − 前一交易日收盘总权益，
        # 与「总权益 / 累计盈亏」完全同源（均用实时 total_assets），
        # 杜绝此前「快照口径 49989 vs 实时口径 50009」造成的正负矛盾。
        pc = _prev_close(current_day)
        daily_pnl = (total_assets - pc) if pc is not None else 0.0
        daily_pct = (daily_pnl / pc) if pc else 0.0
    else:
        # 历史回看口径：使用所选交易日最后一次收盘快照
        total_assets = latest.get("total_assets", 0.0) or (
            account.get("cash", 0.0) + account.get("frozen_cash", 0.0))
        cash = latest.get("cash", account.get("cash", 0.0))
        frozen = latest.get("frozen_cash", account.get("frozen_cash", 0.0))
        mv = latest.get("market_value", 0.0)
        if not mv and account.get("positions"):
            for code, p in account["positions"].items():
                mv += (current_price(code, live) or 0.0) * p["quantity"]
        init_cap = account.get("initial_capital", 0.0) or 0.0
        # 与实时口径一致：累计盈亏 = 总权益 - 期初（含全部手续费），避免回看时
        # 沿用历史快照中可能滞后的 total_return_pct。
        total_pnl = total_assets - init_cap
        total_return_pct = (total_pnl / init_cap * 100.0) if init_cap else 0.0
        current_day = latest.get("day")
        # 历史回看口径：当日盈亏 = 所选交易日收盘总权益 − 前一交易日收盘总权益，
        # 与上一分支同源（均用对应分支的 total_assets 减前一交易日收盘）。
        pc = _prev_close(current_day)
        daily_pnl = (total_assets - pc) if pc is not None else 0.0
        daily_pct = (daily_pnl / pc) if pc else 0.0
    pos_frac = (mv / total_assets) if total_assets else 0.0

    # 资金勾稽校验：总资金 == 可用 + 冻结 + 持仓市值（容差 0.01 元）
    reconcile_ok = abs(total_assets - (cash + frozen + mv)) < 0.01
    # 异常容错联动：部分标的最新价不可得（行情源与快照回退均失败）时，
    # 总资产按已知价估算，须在看板显式告警，杜绝「总额悄悄少算」与持仓
    # 「行情未就绪」之间的数据割裂。
    price_ready = bool(latest.get("price_ready", True))

    # 模式名须与 strategy/risk.py 的 RiskMode 枚举字面量严格一致
    # （NORMAL / DEFENSE_DAILY / DEFENSE_WEEKLY / DEFENSE_PEAK），
    # 否则会掉进兜底而误显示「无智能体」。
    risk_mode = (strat.get("risk", {}) or {}).get("mode", "NORMAL")
    status = {
        "NORMAL": "运行中（正常）",
        "DEFENSE_DAILY": "防守（日回撤）",
        "DEFENSE_WEEKLY": "防守（周回撤）",
        "DEFENSE_PEAK": "全面防守",
    }.get(risk_mode, f"未知模式({risk_mode})")
    learning_on = bool(learned)

    return {
        "total_assets": total_assets, "cash": cash, "frozen_cash": frozen,
        "market_value": mv,
        "total_pnl": total_pnl,
        "daily_pnl": daily_pnl, "daily_pct": daily_pct,
        "total_return_pct": total_return_pct,
        "max_dd": max_dd, "pos_frac": pos_frac,
        "trading_days": len({s["day"] for s in snaps}),
        "status": status, "learning_on": learning_on,
        "current_day": current_day,
        "reconcile_ok": reconcile_ok,
        "price_ready": price_ready,
    }


def compute_positions(account: dict, live: dict, total_assets: float,
                      fee_by_code: dict = None,
                      open_date_by_code: dict = None,
                      ref_day: str = None) -> List[dict]:
    rows = []
    for code, p in account.get("positions", {}).items():
        qty = p["quantity"]
        if qty <= 0:
            continue
        d = resolve_display_price(live, code)
        price = d["price"] or 0.0
        cost = p["cost_price"]
        # 持仓天数：建仓日→今（或历史回看所选交易日）的自然日差 +1（含首尾）。
        # 建仓日取自成交流水首次买入（open_date_by_code），无成交记录则无法统计。
        od = (open_date_by_code or {}).get(code)
        holding_days = calc_holding_days(od, ref_day) if od else None
        mv = price * qty
        # 当日浮动盈亏（净）：以昨收为基准，衡量「今天」赚了多少
        prev_close = float(d.get("prev_close") or 0.0)
        day_upnl = ((price - prev_close) * qty
                    if (d["ready"] and prev_close > 0) else None)
        day_pct = ((price / prev_close - 1.0)
                   if (d["ready"] and prev_close > 0) else None)
        # 浮动盈亏（净）：市价盈亏 − 该标的累计买入费，与账户累计盈亏(净)口径一致，
        # 避免「持仓明细浮动盈亏(不含费) 与 账户累计盈亏(含费) 对不上」。
        fee = (fee_by_code or {}).get(code, 0.0) or 0.0
        upnl = (price - cost) * qty - fee
        cost_amt = cost * qty
        pct = (upnl / cost_amt) if cost_amt else 0.0
        weight = (mv / total_assets) if total_assets else 0.0
        rows.append({
            "code": code, "name": p.get("name", code),
            "qty": qty, "available": p.get("available", 0),
            "cost_price": round(cost, 3), "price": d["price"],
            "prev_close": (d.get("prev_close") or None),
            "ready": d["ready"], "status": d["status"],
            "market_value": (round(mv, 2) if d["ready"] else None),
            "upnl": (round(upnl, 2) if d["ready"] else None),
            "pct": (pct if d["ready"] else 0.0),
            "day_upnl": (round(day_upnl, 2) if day_upnl is not None else None),
            "day_pct": day_pct,
            "weight": weight,
            "holding_days": holding_days,
            "industry": d["industry"],
            # 核心行情指标（与撮合基准价同源）
            "open": d["open"], "high": d["high"], "low": d["low"],
            "volume": d["volume"], "change_pct": d["change_pct"],
            "ceiling": d["ceiling"], "floor": d["floor"],
            "limit_hit": d["limit_hit"], "is_suspended": d["is_suspended"],
            "updated_at": d["updated_at"], "source": d["source"],
            "warn": d["warn"],
        })
    rows.sort(key=lambda r: r["pct"], reverse=True)
    return rows


def aggregate_day_pnl(pos_rows: List[dict]):
    """汇总持仓明细的「当日浮动盈亏」，供账户概览复用，确保与持仓明细同口径。

    返回 (daily_pnl, daily_pct)：
    - daily_pnl = Σ(现价−昨收)×数量，仅累加行情就绪且昨收>0 的持仓；
    - daily_pct = daily_pnl / 昨日持仓市值（= Σ昨收×数量）。

    这样账户概览的「当日盈亏 / 当日涨跌幅」与「持仓明细·当日浮动盈亏」完全一致
    （A股券商通用口径：账户当日盈亏即持仓市值较昨收的变动）。今日买卖的已实现
    盈亏/手续费不计入此处，只影响「累计盈亏 / 总权益」。
    """
    s = 0.0
    prev_mv = 0.0
    for r in pos_rows:
        du = r.get("day_upnl")
        if du is not None:
            s += du
            prev_mv += (r.get("prev_close") or 0.0) * r["qty"]
    return round(s, 2), (s / prev_mv) if prev_mv else 0.0


def industry_dist(rows: List[dict], total_assets: float) -> List[dict]:
    agg: Dict[str, float] = {}
    for r in rows:
        agg[r["industry"]] = agg.get(r["industry"], 0.0) + r["market_value"]
    out = [{"industry": k, "mv": round(v, 2),
            "weight": (v / total_assets) if total_assets else 0.0}
           for k, v in agg.items()]
    out.sort(key=lambda x: x["mv"], reverse=True)
    return out


def compute_trades(trades: List[dict], side: str, name_kw: str,
                   date_from: str, date_to: str) -> List[dict]:
    rows = []
    for t in trades:
        if side != "ALL" and t.get("side") != side:
            continue
        nm = (t.get("name", "") or "")
        if name_kw and name_kw not in nm and name_kw not in t.get("code", ""):
            continue
        dt = t.get("datetime", "")
        d = dt[:10]
        if date_from and d and d < date_from:
            continue
        if date_to and d and d > date_to:
            continue
        rows.append(t)
    rows.sort(key=lambda x: x.get("datetime", ""), reverse=True)
    return rows


def compute_closed_positions(trades: List[dict],
                             open_codes=None) -> List[dict]:
    """历史持仓（已清仓）汇总：以成交流水为唯一数据源。

    对每个「有过卖出、且当前不在持仓中」的标的，统计建仓→清仓全程：
    建仓日 / 清仓日 / 累计买入股数 / 加权成本 / 末次清仓价 / 累计已实现盈亏 /
    盈亏比例（已实现盈亏 ÷ 已清仓部分的加权成本额）。
    """
    open_codes = open_codes or set()
    agg: Dict[str, dict] = {}
    for t in trades:
        c = t.get("code")
        if not c:
            continue
        a = agg.setdefault(c, {
            "code": c, "name": t.get("name", c), "buy_qty": 0, "buy_amt": 0.0,
            "sell_qty": 0, "realized": 0.0, "fee": 0.0,
            "first_buy": "", "last_sell": "", "last_sell_price": None,
            "bad_price": False,
        })
        # 名称：优先取非代码形式（历史记录可能退化成代码）
        if t.get("name") and t["name"] != c:
            a["name"] = t["name"]
        dt = str(t.get("datetime", ""))
        if t.get("side") == "BUY":
            a["buy_qty"] += t.get("quantity", 0)
            a["buy_amt"] += (t.get("amount", 0.0) or 0.0)
            if not a["first_buy"] or dt < a["first_buy"]:
                a["first_buy"] = dt
        else:
            a["sell_qty"] += t.get("quantity", 0)
            a["realized"] += (t.get("realized_pnl", 0.0) or 0.0)
            px = float(t.get("price", 0.0) or 0.0)
            if not a["last_sell"] or dt > a["last_sell"]:
                a["last_sell"] = dt
                a["last_sell_price"] = px
            if px <= 0:
                a["bad_price"] = True
    rows = []
    for c, a in agg.items():
        if c in open_codes or a["sell_qty"] <= 0:
            continue
        sold_cost = (a["buy_amt"] / a["buy_qty"] * a["sell_qty"]) if a["buy_qty"] else 0.0
        a["sold_cost"] = sold_cost
        a["pnl_pct"] = (a["realized"] / sold_cost) if sold_cost else 0.0
        # 持仓天数：建仓日→清仓日自然日差 +1（含首尾）；无建仓/清仓记录则无法统计
        a["holding_days"] = calc_holding_days(a["first_buy"], a["last_sell"])
        rows.append(a)
    rows.sort(key=lambda r: r["last_sell"], reverse=True)
    return rows


def closed_positions_html(rows: List[dict]) -> str:
    if not rows:
        return '<p style="color:#a0aec0">暂无已清仓标的</p>'
    head = ("<tr><th>代码</th><th>名称</th><th>建仓日</th><th>清仓日</th>"
            "<th>持仓天数</th><th>累计买入</th><th>累计卖出</th><th>末次清仓价</th>"
            "<th>已实现盈亏</th><th>盈亏比例</th></tr>")
    body = ""
    for r in rows:
        rpc = _color(r["realized"])
        px = r["last_sell_price"]
        px_cell = (f'{px:.3f} <span class="badge" style="background:#c0392b">行情故障</span>'
                   if r["bad_price"] else (f"{px:.3f}" if px is not None else "—"))
        hold_cell = (f"{r['holding_days']} 天"
                     if r.get("holding_days") is not None else "—")
        body += (f"<tr><td>{r['code']}</td><td>{r['name']}</td>"
                 f"<td>{(r['first_buy'] or '')[:10]}</td>"
                 f"<td>{(r['last_sell'] or '')[:10]}</td>"
                 f"<td style='text-align:right'>{hold_cell}</td>"
                 f"<td>{r['buy_qty']:,}股</td><td>{r['sell_qty']:,}股</td>"
                 f"<td>{px_cell}</td>"
                 f"<td style='color:{rpc}'>{_fmt_money(r['realized'])}</td>"
                 f"<td style='color:{rpc}'>{r['pnl_pct']:+.2%}</td></tr>")
    # 合计行：已实现盈亏直接相加；盈亏比例按「总盈亏 ÷ 已清仓部分总成本」加权，
    # 直接把各笔百分比相加无经济含义
    tot_realized = sum(r["realized"] for r in rows)
    tot_cost = sum(r.get("sold_cost", 0.0) for r in rows)
    tot_pct = (tot_realized / tot_cost) if tot_cost else 0.0
    tc = _color(tot_realized)
    body += (f"<tr style='font-weight:700;border-top:2px solid #cbd5e0'>"
             f"<td colspan=8 style='text-align:right'>合计（{len(rows)} 只）</td>"
             f"<td style='color:{tc}'>{_fmt_money(tot_realized)}</td>"
             f"<td style='color:{tc}'>{tot_pct:+.2%}</td></tr>")
    return (f'<table class="grid"><thead>{head}</thead>'
            f'<tbody>{body}</tbody></table>')


def decision_feed(strat: dict, progress: List[dict]) -> List[dict]:
    feed = []
    for e in strat.get("decision_log", []):
        feed.append({
            "ts": e.get("ts", ""), "type": e.get("type", ""),
            "code": e.get("code", ""), "name": e.get("name", ""),
            "detail": e.get("detail", ""),
        })
    # 学习进度作为"策略参数优化记录"补充
    for r in progress:
        if r.get("backtest"):
            bt = r["backtest"]
            sl, tp1, tp2, base, stock = bt["combo"]
            feed.append({
                "ts": f"{r['day']} 15:30",
                "type": "PARAM_OPT", "code": "", "name": "",
                "detail": (f"第{r['iter']}日 历史回测优化：止损{sl:.0%}/"
                           f"止盈{tp1:.0%}&{tp2:.0%}/单股上限{stock:.0%} "
                           f"→ 夏普{bt['sharpe']:.2f} 回撤{bt['mdd']:.2%} "
                           f"胜率{bt['wr']:.1%}"),
            })
    feed.sort(key=lambda x: x["ts"], reverse=True)
    return feed


# ============ HTML 渲染辅助 ============
def _fmt_money(v) -> str:
    try:
        return f"{v:,.2f}"
    except Exception:
        return str(v)


def _fmt_px(v) -> str:
    """价格展示：无有效行情（None / 0 / 非数值）显示 '—'，避免触发格式异常。

    历史缺陷：open/high/low 单元格曾误将三元表达式写进 f-string 的格式说明符内
    （f"{r['open']:.3f' if ... else '—'}"），导致 ValueError 崩溃看板并使自动刷新中断。
    """
    try:
        fv = float(v)
    except (TypeError, ValueError):
        return "—"
    return f"{fv:.3f}" if fv else "—"


def _color(v: float, up=C_UP, down=C_DOWN) -> str:
    return up if v >= 0 else down


def fmt_trade_date(v) -> str:
    """成交流水「日期」列：统一输出「YYYY年M月D日」。

    与仅显示当日的开关配合：取消勾选查看历史时，能直接看到每笔成交
    属于哪个交易日，不必再靠时间列倒推。
    """
    s = str(v or "").strip().replace("T", " ")
    d = s.split(" ")[0].split(".")[0] if s else ""
    parts = d.split("-") if "-" in d else []
    if len(parts) == 3 and all(p.isdigit() for p in parts):
        return f"{int(parts[0])}年{int(parts[1])}月{int(parts[2])}日"
    return d or "—"


def fmt_trade_time(v) -> str:
    """成交流水「时间」列：统一输出 HH:MM:SS。

    历史成交的 datetime 形如 `2026-10-19 09:30:08.772186`（带微秒，部分旧记录
    带 ISO 的 T 分隔符或只到分钟）。旧实现直接取字符串末 8 位，遇到微秒会截成
    `8.772186`，故改为按字段解析，兼容各种历史格式。
    """
    s = str(v or "").strip().replace("T", " ")
    if not s:
        return "—"
    parts = s.split()
    t = parts[1] if len(parts) > 1 else parts[0]
    t = t.split(".")[0]          # 去掉微秒
    if len(t) == 8:              # HH:MM:SS
        return t
    if len(t) == 5:              # HH:MM → 补秒
        return t + ":00"
    return s                     # 只有日期等异常格式：原样展示，不猜测


def trades_html(rows: List[dict], live: dict = None,
                open_codes=None) -> str:
    if not rows:
        return '<p style="color:#a0aec0">暂无成交记录</p>'
    head = ("<tr><th>日期</th><th>时间</th><th>代码</th><th>名称</th><th>方向</th>"
            "<th>价格</th><th>数量</th><th>金额</th><th>费用</th>"
            "<th>已实现盈亏</th><th>浮动盈亏</th></tr>")
    body = ""
    for i, t in enumerate(rows):
        side = t.get("side")
        color = C_BUY if side == "BUY" else C_SELL
        side_txt = "买入" if side == "BUY" else "卖出"
        bg = f' style="background:{C_BG_HI}"' if i == 0 else ""
        rp = t.get("realized_pnl", 0.0) or 0.0
        rpc = _color(rp)
        # 浮动盈亏：仅对当前仍持仓的买入交易，按实时价估算「净」浮动
        # （市价盈亏 − 买入费），使交易流水的盈亏能与账户累计盈亏勾稽
        # （账户累计=已实现+浮动净；持仓未平仓时已实现=0，账户盈亏=浮动净）。
        fp = None
        if (side == "BUY" and open_codes and t.get("code") in open_codes
                and live):
            d = resolve_display_price(live, t["code"])
            if d["ready"] and d["price"]:
                fp = ((d["price"] - t["price"]) * t["quantity"]
                      - (t.get("total_fee", 0.0) or 0.0))
        if fp is None:
            fpcell = "—"
            fpstyle = ""
        else:
            fpcell = _fmt_money(fp)
            fpstyle = f' style="color:{_color(fp)}"'
        body += (f"<tr{bg}><td>{fmt_trade_date(t.get('datetime'))}</td>"
                 f"<td>{fmt_trade_time(t.get('datetime'))}</td>"
                 f"<td>{t.get('code','')}</td><td>{t.get('name','')}</td>"
                 f"<td style='color:{color};font-weight:600'>{side_txt}</td>"
                 f"<td>{t.get('price',0):.3f}</td><td>{t.get('quantity',0):,}</td>"
                 f"<td>{_fmt_money(t.get('amount',0))}</td>"
                 f"<td>{_fmt_money(t.get('total_fee',0))}</td>"
                 f"<td style='color:{rpc}'>{_fmt_money(rp)}</td>"
                 f"<td{fpstyle}>{fpcell}</td></tr>")
    return (f'<table class="grid"><thead>{head}</thead>'
            f'<tbody>{body}</tbody></table>')


def positions_html(rows: List[dict]) -> str:
    if not rows:
        return '<p style="color:#a0aec0">当前无持仓</p>'
    head = ("<tr><th>代码</th><th>名称</th><th>持仓天数</th><th>行业</th><th>数量</th>"
            "<th>可用</th><th>成本价</th><th>昨收</th><th>现价</th>"
            "<th>开盘</th><th>最高</th><th>最低</th>"
            "<th>成交量</th><th>涨跌幅</th><th>涨跌停</th>"
            "<th>市值</th><th>当日浮动盈亏</th><th>当日盈亏比例</th>"
            "<th>浮动盈亏</th><th>盈亏比例</th><th>仓位占比</th>"
            "<th>时效</th></tr>")
    body = ""
    for r in rows:
        pc = _color(r["pct"])
        # 现价列：停牌 / 未就绪 显式标识，不展示虚构价
        if r.get("is_suspended"):
            price_cell = ('<span class="badge" style="background:#718096">停牌</span>'
                          f'<br><span style="color:#a0aec0">{r["price"]:.3f}</span>')
        elif not r.get("ready"):
            price_cell = ('<span class="badge" style="background:#c0392b">行情未就绪</span>')
        else:
            price_cell = f'{r["price"]:.3f}'
        # 涨跌停状态标识
        if r.get("limit_hit") == "UP":
            limit_cell = (f'<span class="badge" style="background:#c0392b">涨停</span>'
                          f'<br><span style="color:#a0aec0">{r["ceiling"]:.3f}</span>')
        elif r.get("limit_hit") == "DOWN":
            limit_cell = (f'<span class="badge" style="background:#1a7a3a">跌停</span>'
                          f'<br><span style="color:#a0aec0">{r["floor"]:.3f}</span>')
        else:
            limit_cell = "—"
        chg = r.get("change_pct", 0.0)
        chg_cell = (f'<span style="color:{_color(chg)}">{chg:+.2f}%</span>'
                    if r.get("ready") else "—")
        vol = r.get("volume", 0.0)
        vol_cell = f'{vol/10000:,.1f}万' if vol else "—"
        src = r.get("source") or "—"
        upd = (r.get("updated_at") or "").replace("T", " ")
        ts_cell = (f'{src}<br><span style="color:#a0aec0">{upd}</span>'
                   if r.get("ready") else "—")
        mv_cell = _fmt_money(r["market_value"]) if r.get("ready") else "—"
        # 当日盈亏：无昨收（新上市/行情缺失）显式标—not applicable，不虚构 0
        if r.get("day_upnl") is None:
            dupnl_cell, dpct_cell = "—", "—"
        else:
            dc = _color(r["day_upnl"])
            dupnl_cell = f'<span style="color:{dc}">{_fmt_money(r["day_upnl"])}</span>'
            dpct_cell = (f'<span style="color:{dc};font-weight:600">'
                         f'{r["day_pct"]:+.2%}</span>')
        upnl_cell = (f'<span style="color:{pc}">{_fmt_money(r["upnl"])}</span>'
                     if r.get("ready") else "—")
        pct_cell = (f'<span style="color:{pc};font-weight:600">{r["pct"]:+.2%}</span>'
                    if r.get("ready") else "—")
        # 开盘/最高/最低：先格式化再拼接（不把三元表达式塞进格式说明符）
        open_cell = _fmt_px(r["open"])
        high_cell = _fmt_px(r["high"])
        low_cell = _fmt_px(r["low"])
        hold_cell = (f"{r['holding_days']} 天"
                     if r.get("holding_days") is not None else "—")
        body += (f"<tr><td>{r['code']}</td><td>{r['name']}</td>"
                 f"<td style='text-align:right'>{hold_cell}</td>"
                 f"<td>{r['industry']}</td><td>{r['qty']:,}</td>"
                 f"<td>{r['available']:,}</td><td>{r['cost_price']:.3f}</td>"
                 f"<td>{_fmt_px(r.get('prev_close'))}</td>"
                 f"<td>{price_cell}</td>"
                 f"<td>{open_cell}</td><td>{high_cell}</td><td>{low_cell}</td>"
                 f"<td>{vol_cell}</td><td>{chg_cell}</td><td>{limit_cell}</td>"
                 f"<td>{mv_cell}</td><td>{dupnl_cell}</td><td>{dpct_cell}</td>"
                 f"<td>{upnl_cell}</td><td>{pct_cell}</td>"
                 f"<td>{r['weight']:.2%}</td><td>{ts_cell}</td></tr>")
    # 合计行（与历史持仓口径一致）：总市值 = 各持仓市值之和；当日浮动盈亏 / 浮动盈亏
    # 直接相加；当日盈亏比例按「合计当日盈亏 ÷ 合计昨收市值」加权（百分比直加无经济含义）。
    # 持仓明细共 22 列：标签跨前 15 列（至「涨跌停」），其后依次接 市值 / 当日浮动盈亏 /
    # 当日盈亏比例 / 浮动盈亏，其余列（盈亏比例 / 仓位占比 / 时效）留空。
    tot_mv = sum((r["market_value"] or 0.0) for r in rows)
    tot_dupnl = sum((r["day_upnl"] or 0.0)
                    for r in rows if r.get("day_upnl") is not None)
    tot_prev = sum((r.get("prev_close") or 0.0) * r["qty"]
                   for r in rows if r.get("day_upnl") is not None)
    tot_dpct = (tot_dupnl / tot_prev) if tot_prev else 0.0
    tot_upnl = sum((r["upnl"] or 0.0) for r in rows if r.get("ready"))
    dc = _color(tot_dupnl)
    uc = _color(tot_upnl)
    body += (f"<tr style='font-weight:700;border-top:2px solid #cbd5e0'>"
             f"<td colspan=15 style='text-align:right'>合计（{len(rows)} 只）</td>"
             f"<td>{_fmt_money(tot_mv)}</td>"
             f"<td style='color:{dc}'>{_fmt_money(tot_dupnl)}</td>"
             f"<td style='color:{dc};font-weight:600'>{tot_dpct:+.2%}</td>"
             f"<td style='color:{uc}'>{_fmt_money(tot_upnl)}</td>"
             f"<td>—</td><td>—</td><td>—</td></tr>")
    return (f'<table class="grid"><thead>{head}</thead>'
            f'<tbody>{body}</tbody></table>')


def feed_html(feed: List[dict], limit: int = 120) -> str:
    if not feed:
        return '<p style="color:#a0aec0">暂无决策日志</p>'
    style = {
        "OPEN": ("#1a7a3a", "新开仓"), "ADD": ("#2f855a", "加仓"),
        "止损": ("#c0392b", "止损"), "止盈": ("#1a7a3a", "止盈"),
        "持有到期": ("#718096", "到期"), "风控降仓": ("#dd6b20", "风控"),
        "RISK_TRIGGER": ("#c0392b", "风控触发"),
        "REGIME": ("#2b6cb0", "风格"), "PARAM_OPT": ("#2b6cb0", "参数优化"),
    }
    body = ""
    for e in feed[:limit]:
        col, tag = style.get(e["type"], ("#4a5568", e["type"]))
        who = f"{e['code']} {e['name']}".strip()
        # 时间戳统一成「日期 时分秒」，避免 ISO 的 T 分隔符影响阅读
        ts = str(e.get("ts", "")).replace("T", " ")
        body += (f'<div class="log"><span class="tag" style="background:{col}">'
                 f'{tag}</span><span class="ts">{ts}</span> '
                 f'<span class="who">{who}</span> {e["detail"]}</div>')
    return f'<div class="logs">{body}</div>'


# ============ 主界面 ============
def _render():
    account = load_json(DATA / "account.json", {})
    live = load_json(DATA / "live_prices.json", {})
    snaps = load_jsonl(DATA / "snapshots.jsonl")
    trades = load_jsonl(DATA / "trades.jsonl")
    trades_all = trades   # 保留未过滤全集，供派生建仓日（持仓天数）使用
    strat = load_json(DATA / "strategy_state.json", {})
    learned = load_json(DATA / "learned_params.json", {})
    progress = load_jsonl(DATA / "learning_progress.jsonl")

    st.markdown(
        "<style>" + """
        .big{font-size:13px;color:#2d3748;}
        .metric .label{font-size:12px;color:#718096;}
        .metric .val{font-size:22px;font-weight:700;margin:2px 0;}
        .grid{border-collapse:collapse;width:100%;font-size:12.5px;}
        .grid th{background:#edf2f7;color:#4a5568;text-align:right;
                 padding:5px 8px;position:sticky;top:0;}
        .grid td{border-bottom:1px solid #edf2f7;padding:4px 8px;text-align:right;}
        .grid th:nth-child(1),.grid td:nth-child(1),
        .grid th:nth-child(2),.grid td:nth-child(2),
        .grid th:nth-child(3),.grid td:nth-child(3),
        .grid th:nth-child(4),.grid td:nth-child(4){text-align:left;}
        .logs{font-size:12.5px;line-height:1.5;max-height:520px;overflow:auto;}
        .log{padding:3px 0;border-bottom:1px dashed #edf2f7;}
        .tag{color:#fff;border-radius:4px;padding:1px 6px;font-size:11px;
             margin-right:6px;}
        .ts{color:#a0aec0;font-size:11px;margin-right:6px;}
        .who{font-weight:600;margin-right:4px;}
        .badge{padding:2px 8px;border-radius:10px;font-size:12px;color:#fff;}
        .summary{background:#ebf8ff;border:1px solid #bee3f8;border-radius:8px;
                 padding:10px 12px;color:#2c5282;font-size:13px;margin-bottom:8px;}
        </style>""",
        unsafe_allow_html=True,
    )

    # ---------- 顶栏 ----------
    st_now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    phase = market_phase_label()
    phase_state = phase["state"]   # 绑定阶段状态，供顶栏徽标与休市视图统一引用
    badge = ({"LIVE": ("#1a7a3a", phase["label"]),
              "PRE": ("#2b6cb0", phase["label"]),
              "PAUSE": ("#dd6b20", phase["label"]),
              "CLOSED": ("#718096", phase["label"])}[phase["state"]])
    meta = live.get("_meta", {}) or {}
    meta_src = meta.get("source", "—")
    meta_gen = (meta.get("generated_at") or "").replace("T", " ")
    st.markdown(
        f'<div style="display:flex;justify-content:space-between;'
        f'align-items:center">'
        f'<h2 style="margin:0">A股模拟交易 · 实时看板</h2>'
        f'<span><span class="badge" style="background:{badge[0]}">{badge[1]}</span>'
        f' &nbsp;<span style="color:#a0aec0;font-size:12px">{st_now}</span></span>'
        f'</div>', unsafe_allow_html=True)
    # 数据源状态 + 时效戳（杜绝两套数据：展示价旁必带 updated_at 与 source）
    st.markdown(
        f'<div style="font-size:12px;color:#4a5568;margin:2px 0 6px">'
        f'数据源：<b>{meta_src}</b> ｜ 快照更新：<b>{meta_gen or "—"}</b>'
        f' ｜ 刷新频率：<b>{phase["refresh"]}</b>'
        f'（页面每 3s 重绘，行情快照每 {phase["refresh"]} 刷新）</div>',
        unsafe_allow_html=True)
    # 行情就绪告警：有持仓却无有效快照 → 显式提示，禁止虚构演示价
    # （空仓账户无行情需求，不误报「未就绪」）
    if account.get("positions") and (not meta or meta.get("positions", 0) == 0):
        st.markdown(
            '<div style="background:#fff5f5;border:1px solid #fc8181;'
            'border-radius:8px;padding:8px 12px;color:#9b2c2c;font-size:13px">'
            '⚠ <b>行情数据未就绪</b>：实时价快照为空（无持仓或首笔刷新未生成），'
            '看板不展示任何虚构演示价；持仓建立后由调度守护每 3s 刷新快照。</div>',
            unsafe_allow_html=True)

    # ---------- 模式选择：实时 / 历史 ----------
    mode = st.sidebar.radio("视图模式", ["实时看板", "历史交易日"],
                            help="历史模式回看任意交易日账户与成交，不自动刷新")
    st.session_state["view_mode"] = mode   # 供自动刷新保障（finally 阶段）读取
    hist_day = None
    if mode == "历史交易日":
        avail = sorted({s["day"] for s in snaps if s.get("day")}, reverse=True)
        if avail:
            hist_day = st.sidebar.selectbox("选择交易日", avail)
        else:
            st.sidebar.warning("暂无历史快照数据，请先运行策略调度。")
            mode = "实时看板"

    if phase_state == "CLOSED" and mode == "实时看板":
        st.markdown('<div class="summary">当前为休市时段，已切换为'
                    '<b>收盘总结视图</b>：展示当日账户、成交与决策汇总。'
                    '交易时段将自动恢复实时刷新。</div>', unsafe_allow_html=True)

    # 历史模式：用选定日的快照覆盖 latest
    if mode == "历史交易日" and hist_day:
        day_snaps = [s for s in snaps if s.get("day") == hist_day]
        snaps = day_snaps  # 概览按该日
        trades = [t for t in trades if (t.get("trading_day") or "") == hist_day]
        strat = {**strat,
                 "decision_log": [e for e in strat.get("decision_log", [])
                                  if e.get("day") == hist_day]}

    ov = compute_overview(account, live, snaps, learned, strat,
                          live_mode=(mode == "实时看板"))
    total_assets = ov["total_assets"]
    # 各标的累计买入费（用于持仓浮动盈亏净额，与账户累计盈亏口径一致）
    fee_by_code = {}
    for t in trades:
        if t.get("side") == "BUY":
            fee_by_code[t.get("code")] = (fee_by_code.get(t.get("code"), 0.0)
                                          + (t.get("total_fee", 0.0) or 0.0))
    # 持仓天数基准日：实时=今日；历史回看=所选交易日。建仓日取自全量成交（不
    # 受历史模式对 trades 的交易日过滤影响），历史回看时再按 hist_day 截断。
    today_str = datetime.now().strftime("%Y-%m-%d")
    ref_day = hist_day if (mode == "历史交易日" and hist_day) else today_str
    cap = hist_day if mode == "历史交易日" else None
    open_date_by_code = first_buy_by_code(trades_all, cap)
    pos_rows = compute_positions(account, live, total_assets, fee_by_code,
                                 open_date_by_code, ref_day)
    # 账户概览「当日盈亏 / 当日涨跌幅」与「持仓明细·当日浮动盈亏」统一口径：
    # 均以「昨收」为基准、对当前持仓做市值变动求和（= Σ(现价−昨收)×数量），
    # 使概览与明细严格一致（A股券商通用口径：账户当日盈亏即持仓市值较昨收的变动）。
    # 注意：今日买卖的已实现盈亏/手续费不计入此「当日盈亏」，只影响「累计盈亏/总权益」。
    ov["daily_pnl"], ov["daily_pct"] = aggregate_day_pnl(pos_rows)
    ind = industry_dist(pos_rows, total_assets)

    # 部分标的行情未就绪（持仓存在但快照缺该标的有效价）→ 显式提示，禁止虚构价
    if pos_rows and any((not r.get("ready")) for r in pos_rows):
        st.markdown(
            '<div style="background:#fffaf0;border:1px solid #f6ad55;'
            'border-radius:8px;padding:8px 12px;color:#9c4221;font-size:13px">'
            '⚠ <b>部分标的行情未就绪</b>：持仓明细中标红「行情未就绪」项缺少有效行情快照，'
            '已禁止展示虚构价，请等待下一周期刷新或由源恢复。</div>',
            unsafe_allow_html=True)

    # ========== 1) 账户概览 ==========
    st.subheader("① 账户概览")
    c = st.columns(5)
    def metric(col, label, val, color=None, suffix=""):
        v = f'<span style="color:{color}">{val}</span>' if color else val
        col.markdown(f'<div class="metric"><div class="label">{label}</div>'
                     f'<div class="val">{v}{suffix}</div></div>',
                     unsafe_allow_html=True)
    metric(c[0], "总权益(元)", _fmt_money(total_assets))
    metric(c[1], "可用资金(元)", _fmt_money(ov["cash"]))
    metric(c[2], "冻结资金(元)", _fmt_money(ov["frozen_cash"]))
    dp = ov["daily_pnl"]
    metric(c[3], "当日盈亏(元)", _fmt_money(dp), _color(dp))
    metric(c[4], "当日涨跌幅", f'{ov["daily_pct"]:+.2%}', _color(dp))
    c = st.columns(6)
    metric(c[0], "累计盈亏(元)", _fmt_money(ov["total_pnl"]),
           _color(ov["total_pnl"]))
    metric(c[1], "累计收益率", f'{ov["total_return_pct"]:+.2f}%',
           _color(ov["total_return_pct"]))
    metric(c[2], "历史最大回撤", f'-{ov["max_dd"]:.2%}', C_DOWN)
    metric(c[3], "已运行交易日", str(ov["trading_days"]))
    metric(c[4], "当前总仓位", f'{ov["pos_frac"]:.2%}',
           C_DOWN if ov["pos_frac"] > 0.8 else None)
    sc = "#c0392b" if "防守" in ov["status"] else "#1a7a3a"
    metric(c[5], "智能体状态", ov["status"], sc)
    st.caption(f"当前交易日：{ov['current_day'] or '—'} ｜ 自学习："
               f"{'已启用' if ov['learning_on'] else '未启用'} ｜ "
               f"「当日盈亏」= 持仓市值较昨收的变动（与持仓明细同口径），"
               f"今日买卖的已实现盈亏计入「累计盈亏」")

    # 资金勾稽异常告警（总资金 ≠ 可用+冻结+市值 或 派生恒等式不成立时高亮）
    if not ov.get("reconcile_ok"):
        st.markdown(
            '<div style="background:#fff5f5;border:1px solid #fc8181;'
            'border-radius:8px;padding:10px 12px;color:#9b2c2c;'
            f'font-size:13px;margin-bottom:8px">'
            f'⚠ <b>资金勾稽异常</b>：检测到总资金与「可用资金+冻结资金+持仓市值」'
            f'不一致，请核查清算模块。等式 A 差额见上方「冻结资金/持仓市值」。</div>',
            unsafe_allow_html=True)

    # ========== 2) 成交流水 ==========
    st.subheader("② 实时交易流水")
    today = datetime.now().strftime("%Y-%m-%d")
    fc1, fc2, fc3, fc4, fc5 = st.columns([1, 2, 1, 1, 1])
    side_sel = fc1.selectbox("方向", ["ALL", "BUY", "SELL"])
    name_kw = fc2.text_input("名称/代码筛选", "", placeholder="如 茅台 / sh600519")
    today_only = fc3.checkbox("仅显示当日", value=True,
                              help="勾选后只看今天；取消勾选可按下方起止日查全部历史")
    df = fc4.text_input("起始日", "" if today_only else "", placeholder="YYYY-MM-DD",
                        disabled=today_only)
    dt = fc5.text_input("截止日", "" if today_only else "", placeholder="YYYY-MM-DD",
                        disabled=today_only)
    if today_only:
        df = dt = today
    trade_rows = compute_trades(trades, side_sel, name_kw.strip(),
                                 df.strip(), dt.strip())
    open_codes = set((account.get("positions") or {}).keys())
    st.markdown(trades_html(trade_rows, live, open_codes),
                unsafe_allow_html=True)
    st.caption(f"共 {len(trade_rows)} 笔（绿色=买入，红色=卖出，"
               f"首行高亮为最新成交；浮动盈亏按实时价估算并已扣买入费，"
               f"未平仓时显示在持仓买入行）")
    # 交易流水勾稽（仅实时口径）：已实现盈亏 + 浮动盈亏(净, 含买入费)
    # = 账户累计盈亏，避免「流水只显示已实现=0，与账户累计盈亏对不上」的困惑。
    if mode == "实时看板":
        realized_sum = sum((t.get("realized_pnl") or 0.0) for t in trades)
        floating_sum = 0.0
        for t in trades:
            if t.get("side") == "BUY" and t.get("code") in open_codes:
                d = resolve_display_price(live, t["code"])
                if d["ready"] and d["price"]:
                    floating_sum += ((d["price"] - t["price"]) * t["quantity"]
                                     - (t.get("total_fee", 0.0) or 0.0))
        recon = realized_sum + floating_sum
        recon_ok = abs(recon - ov["total_pnl"]) < 0.01
        rc = "#1a7a3a" if recon_ok else "#c0392b"
        st.markdown(
            f'<div style="font-size:12.5px;margin:6px 0 0;color:#2d3748">'
            f'💡 勾稽：已实现盈亏合计 <b>{_fmt_money(realized_sum)}</b> ＋ '
            f'浮动盈亏合计(净) '
            f'<b style="color:{_color(floating_sum)}">{_fmt_money(floating_sum)}</b> '
            f'＝ 账户累计盈亏 '
            f'<b style="color:{_color(ov["total_pnl"])}">'
            f'{_fmt_money(ov["total_pnl"])}</b> '
            f'<b style="color:{rc}">{"✓ 一致" if recon_ok else "✗ 不一致"}</b>'
            f'（当前无平仓，已实现为 0，账户盈亏全部来自持仓浮动）</div>',
            unsafe_allow_html=True)

    # ========== 3) 持仓明细 + 行业分布 ==========
    st.subheader("③ 持仓明细（按盈亏比例排序）")
    st.caption("说明：现价列旁标注「市价单成交含 5bps 滑点」；涨停/跌停/停牌状态已显式标识；"
               "展示价与撮合基准价同源（live_prices.json），每 3s 刷新。")
    col_p, col_i = st.columns([3, 1])
    with col_p:
        st.markdown(positions_html(pos_rows), unsafe_allow_html=True)
    with col_i:
        st.markdown("**行业分布**")
        if ind:
            maxw = max(x["weight"] for x in ind) or 1.0
            for x in ind:
                pct = x["weight"]
                bar = int(pct / maxw * 100)
                st.markdown(
                    f'<div style="font-size:12px;margin:4px 0">'
                    f'{x["industry"]} '
                    f'<span style="color:#718096">{pct:.1%}</span><br>'
                    f'<div style="background:#edf2f7;height:8px;border-radius:4px">'
                    f'<div style="background:#2b6cb0;height:8px;width:{bar}%;'
                    f'border-radius:4px"></div></div></div>',
                    unsafe_allow_html=True)
        else:
            st.caption("无持仓")

    # ========== 4) 历史持仓（已清仓） ==========
    st.subheader("④ 历史持仓（已清仓）")
    st.caption("以成交流水为唯一数据源：统计每只已清仓标的建仓→清仓全程的已实现盈亏；"
               "「行情故障」标记表示当日行情源返回 0 价导致清仓价记录失真。")
    st.markdown(closed_positions_html(
        compute_closed_positions(trades, open_codes)), unsafe_allow_html=True)

    # ========== 5) 决策日志 ==========
    st.subheader("⑤ 决策日志（智能体思考过程）")
    ft = st.radio("类型过滤", ["全部", "OPEN", "ADD", "止损", "止盈",
                            "风控降仓", "RISK_TRIGGER", "REGIME", "PARAM_OPT"],
                  horizontal=True)
    feed = decision_feed(strat, progress)
    if ft != "全部":
        feed = [e for e in feed if e["type"] == ft]
    st.markdown(feed_html(feed), unsafe_allow_html=True)

def _auto_refresh() -> None:
    """自动刷新保障：无论渲染是否抛异常都必须执行（防止页面冻结在旧画面）。

    历史缺陷：渲染中途异常（如持仓明细格式错误抛 ValueError）会跳过函数末尾的
    st.rerun()，导致看板不再自动重绘，页面冻结在出错前的一次画面——阶段标签、
    时效戳均停在旧时间（曾被误读为「交易时段仍显示集合竞价」）。
    """
    if st.session_state.get("view_mode", "实时看板") != "实时看板":
        return
    interval = int(market_phase_label()["refresh"].rstrip("s"))
    st.caption(f"⏱ 自动刷新间隔：{interval}s"
               f"（交易时段 3s / 其他 60s，与快照刷新频率一致）")
    time.sleep(interval)
    st.rerun()


def main() -> None:
    try:
        _render()
    except Exception as exc:                 # 异常容错联动：单点渲染失败不连带中断刷新
        import traceback
        st.error(f"看板渲染异常（已捕获，自动刷新继续）："
                 f"{type(exc).__name__}: {exc}")
        with st.expander("查看堆栈"):
            st.code(traceback.format_exc())
    _auto_refresh()


if __name__ == "__main__":
    main()
