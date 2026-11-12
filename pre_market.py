"""stock-simulator · 盘前信息检索（开盘前主动准备，不触发任何交易）。

职责：在每个交易日开盘前（由 scheduler 在 09:00 触发一次）主动完成：
  1) 复用调度守护每 3s 落盘的实盘行情缓存 data/live_prices.json（零网络、毫秒级）；
  2) 采集当前持仓股的盘前快照（昨收 / 今开 / 盘前涨跌幅）；
  3) 研判市场风格 regime：优先复用智能体收盘复盘落盘的真实研判
     （data/learned_params.json → learner.regime），口径与盘中/收盘决策一致；
  4) 基于缓存中全部标的的盘前涨跌，生成「盘前关注」观察池（纯展示，不构成交易信号）；
  5) 快照智能体当前学习状态（learned_params.json 中优化后的真实权重）；
  6) 落盘 data/pre_market.json，供看板「盘前摘要」区块展示。

可靠性设计（关键）：
  - 全程只读本地已落盘数据，不发起任何行情网络请求：
    盘前时段行情源可能尚未就绪 / 限流，网络调用极易阻塞并拖累 09:30 开盘的盘中循环。
    真实的行情采集由常驻调度器负责，本脚本只做「信息汇总与研判」。
  - 任何单步异常都被吞掉，保证「部分失败也能产出可用摘要」；
  - 按交易日幂等标记（data/.pre_market_done.<date>），同日重复触发不重复检索。

依赖：无外部依赖（纯本地文件读取）。
"""
import os
import glob
import json
from datetime import date, datetime

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(HERE, "data")
PRE_MARKET_JSON = os.path.join(DATA_DIR, "pre_market.json")
ACCOUNT_JSON = os.path.join(DATA_DIR, "account.json")
LIVE_PRICES_JSON = os.path.join(DATA_DIR, "live_prices.json")
LEARNED_PARAMS_JSON = os.path.join(DATA_DIR, "learned_params.json")

WATCH_TOP = 8            # 盘前关注入选数量
MOMENTUM_OPEN = 0.3      # 盘前涨跌幅绝对值 ≥0.3% 视为异动


def _log(msg):
    print(f"[pre_market] {msg}", flush=True)


def _load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f) or default
    except Exception:
        return default


def _regime_from_learned(learned: dict) -> str:
    """复用智能体收盘复盘落盘的真实风格研判（口径与盘中/收盘决策一致）。"""
    try:
        r = (learned.get("learner") or {}).get("regime")
        if r in ("BULL", "OSCILLATION", "BEAR"):
            return r
    except Exception:
        pass
    return "UNKNOWN"


def _learner_state(learned: dict) -> dict:
    """快照智能体学习状态：优先真实优化后的权重，缺项用配置基线补齐。"""
    out = {}
    try:
        learner = learned.get("learner") or {}
        params = learned.get("params") or {}
        for k in ("factor_weights", "strategy_weights", "four_dim_weight", "strategy_eff"):
            for src in (learner, params):
                if isinstance(src.get(k), dict):
                    out[k] = dict(src[k])
                    break
    except Exception:
        pass
    try:
        import sys
        sys.path.insert(0, HERE)
        from config.strategy import StrategyConfig  # type: ignore
        cfg = StrategyConfig()
        out.setdefault("factor_weights", dict(getattr(cfg, "factor_weights", {})))
        out.setdefault("strategy_weights", dict(getattr(cfg, "strategy_weights", {})))
    except Exception:
        pass
    return out


def _holdings_pre(account: dict, live: dict) -> list:
    """持仓股盘前快照（全部来自 live_prices.json 缓存）。"""
    out = []
    for code, p in (account.get("positions") or {}).items():
        q = live.get(code)
        if not isinstance(q, dict):
            continue
        prev, op = q.get("prev_close"), q.get("open")
        chg = q.get("change_pct")
        if chg is None and prev and op and prev > 0:
            chg = round((op - prev) / prev * 100, 2)
        out.append({
            "code": code,
            "name": p.get("name") or q.get("name"),
            "prev_close": prev,
            "open": op,
            "change_pct": round(chg, 2) if isinstance(chg, (int, float)) else None,
            "cost": p.get("cost_price"),
        })
    return out


def _watchlist(live: dict, held_codes: set) -> list:
    """盘前异动观察池：基于缓存内标的的盘前涨跌幅排序（纯展示）。
    调度器默认只缓存持仓股：此时观察池退化为「持仓内盘前波动最大的几只」并标注来源，
    绝不虚构全市场数据；若缓存含非持仓标的（全市场已预热），则优先展示非持仓异动。"""
    universe, held = [], []
    for code, q in live.items():
        if code.startswith("_") or not isinstance(q, dict):
            continue
        chg = q.get("change_pct")
        if not isinstance(chg, (int, float)) or abs(chg) < MOMENTUM_OPEN:
            continue
        row = {"code": code, "name": q.get("name"),
               "change_pct": round(chg, 2),
               "scope": "持仓" if code in held_codes else "全市场"}
        (held if code in held_codes else universe).append(row)
    universe.sort(key=lambda x: abs(x["change_pct"]), reverse=True)
    held.sort(key=lambda x: abs(x["change_pct"]), reverse=True)
    # 优先非持仓异动；不足时用持仓内波动补足，始终有真实内容
    return (universe + held)[:WATCH_TOP]


def _data_basis(live_meta: dict) -> str:
    """判定行情快照基准：盘前 09:00 尚无当日行情，缓存仍是上一交易日收盘快照。
    如实标注，避免把「昨收涨跌」误称为「当日盘前涨跌」——模拟真实。"""
    gen = (live_meta.get("generated_at") or "")[:10]
    today = date.today().isoformat()
    if gen == today:
        return "当日实时快照"
    if gen:
        return f"上一交易日收盘快照（{gen}）"
    return "历史快照（时点未知）"


def _summary(regime: str, holdings: list, watch: list,
             live_meta: dict, basis: str) -> str:
    lines = []
    label = {"BULL": "偏多", "OSCILLATION": "震荡", "BEAR": "偏空",
             "UNKNOWN": "研判中"}.get(regime, regime)
    lines.append(f"盘前风格研判：{label}（{regime}，沿用智能体最近收盘复盘研判）。")
    lines.append(f"数据基准：{basis}。")
    if holdings:
        ups = [h for h in holdings if (h.get("change_pct") or 0) > 0]
        downs = [h for h in holdings if (h.get("change_pct") or 0) < 0]
        lines.append(f"持仓 {len(holdings)} 只：基准快照下 {len(ups)} 涨 / {len(downs)} 跌。")
        if ups:
            top = max(ups, key=lambda h: h["change_pct"])
            lines.append(f"其中最强：{top['name']}（{top['change_pct']:+}%）。")
    if watch:
        names = "、".join(f"{w['name']}（{w['change_pct']:+}%）" for w in watch[:3])
        lines.append(f"重点观察：{names}。")
    gen = (live_meta.get("generated_at") or "").replace("T", " ")
    if gen:
        lines.append(f"行情快照时点：{gen}（调度守护每 3s 刷新）。")
    lines.append("（盘前信息准备快照，不构成交易建议；实际决策以开盘后实时信号为准。）")
    return "".join(lines)


def run_pre_market(force: bool = False) -> str:
    today = date.today()
    done_flag = os.path.join(DATA_DIR, f".pre_market_done.{today.isoformat()}")
    if os.path.exists(done_flag) and not force:
        return f"SKIP (already done for {today.isoformat()})"

    account = _load_json(ACCOUNT_JSON, {})
    live = _load_json(LIVE_PRICES_JSON, {})
    learned = _load_json(LEARNED_PARAMS_JSON, {})
    live_meta = live.get("_meta", {}) or {}

    out = {
        "trading_day": today.isoformat(),
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source": live_meta.get("source", "westock") + " (调度守护实盘快照·本地缓存)",
        "snapshot_at": live_meta.get("generated_at"),
        "data_basis": _data_basis(live_meta),
        "regime": _regime_from_learned(learned),
        "holdings": [],
        "watchlist": [],
        "learner_state": {},
        "summary": "",
    }
    out["holdings"] = _holdings_pre(account, live)
    held_codes = {h["code"] for h in out["holdings"]}
    out["watchlist"] = _watchlist(live, held_codes)
    out["learner_state"] = _learner_state(learned)
    out["summary"] = _summary(out["regime"], out["holdings"],
                              out["watchlist"], live_meta, out["data_basis"])

    os.makedirs(DATA_DIR, exist_ok=True)
    with open(PRE_MARKET_JSON, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    with open(done_flag, "w") as f:
        f.write("ok")
    # 清理历史幂等标记，避免每天累积小文件（保持系统干净）
    try:
        for old in glob.glob(os.path.join(DATA_DIR, ".pre_market_done.*")):
            if old != done_flag:
                os.remove(old)
    except Exception:
        pass
    _log(f"完成：regime={out['regime']} 持仓{len(out['holdings'])} "
         f"异动{len(out['watchlist'])}（零网络，毫秒级）")
    return f"OK -> {PRE_MARKET_JSON}"


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="忽略幂等标记，强制重跑盘前检索")
    args = ap.parse_args()
    _log(f"盘前信息检索开始（交易日={date.today().isoformat()}）...")
    _log(run_pre_market(force=args.force))


if __name__ == "__main__":
    main()
