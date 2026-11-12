"""stock-simulator · 盘前信息检索（开盘前主动准备，不触发任何交易）。

职责：在每个交易日开盘前（由 scheduler 在 08:30 触发一次）主动完成：
  1) 复用收盘流程预热的「全市场行情快照」data/.westock_cache.json
     （沪深300 + 中证500 候选池，含 price / prev_close / 行业），
     汇总全市场涨跌分布、涨跌停、领涨领跌个股与行业强弱、异动榜；
  2) 复用调度守护每 3s 落盘的实盘行情缓存 data/live_prices.json，
     采集当前持仓股的盘前快照（昨收 / 今开 / 涨跌幅）；
  3) 研判市场风格 regime：优先复用智能体收盘复盘落盘的真实研判
     （data/learned_params.json → learner.regime），口径与盘中/收盘决策一致；
  4) 基于全市场真实快照生成「盘前关注」观察池（纯展示，不构成交易信号），
     非持仓优先、涨跌两侧兼顾，全市场样本不足时用持仓内异动如实补足；
  5) 快照智能体当前学习状态（learned_params.json 中优化后的真实权重）；
  6) 落盘 data/pre_market.json，供看板「盘前速览」展示。

可靠性设计（关键）：
  - 全程只读本地已落盘数据，不发起任何行情网络请求：
    A 股 09:15 才开始集合竞价，08:30 盘前抓到的仍只是昨收；且历史上
    warm_universe / get_quote 等网络调用在盘前极易阻塞并拖累 09:30 开盘循环。
    真实的全市场行情采集由 15:35 收盘流程负责，本脚本只做「信息汇总与研判」。
  - 全市场统计统一采用「最新快照日」的有效样本，混用不同日期的涨跌幅会失真；
    较旧快照 / 价格缺失的标的被排除并如实计数，绝不虚构行情。
  - 涨跌幅一律由 price 与 prev_close 自行计算（quote 的 change_pct 常为 0 占位）。
  - 任何单步异常都被吞掉并降级，保证「部分失败也能产出可用摘要」；
  - 按交易日幂等标记（data/.pre_market_done.<date>），同日重复触发不重复检索。

依赖：无外部依赖（纯本地文件读取）。
"""
import os
import glob
import json
import statistics
from datetime import date, datetime

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(HERE, "data")
PRE_MARKET_JSON = os.path.join(DATA_DIR, "pre_market.json")
ACCOUNT_JSON = os.path.join(DATA_DIR, "account.json")
LIVE_PRICES_JSON = os.path.join(DATA_DIR, "live_prices.json")
LEARNED_PARAMS_JSON = os.path.join(DATA_DIR, "learned_params.json")
WESTOCK_CACHE_JSON = os.path.join(DATA_DIR, ".westock_cache.json")

WATCH_TOP = 10           # 盘前异动观察池入选数量
MOMENTUM_OPEN = 0.3      # 持仓快照涨跌幅绝对值 ≥0.3% 视为异动
UNIVERSE_MOVE = 2.0      # 全市场异动阈值：|涨跌幅| ≥2%
BIG_MOVE = 3.0           # 大涨 / 大跌统计阈值
LIMIT_MOVE = 9.8         # 涨跌停统计阈值（含 20cm 板的近似下界）
SECTOR_MIN = 3           # 行业强弱统计的最小样本数
UNIVERSE_SCOPE = "沪深300 + 中证500 候选池（系统选股全市场）"


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
    """持仓快照异动（兜底口径）：基于 live_prices.json 内标的的涨跌幅排序。
    全市场快照可用时，主口径走 _universe_watchlist；本函数仅用于补足与兼容。"""
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
    return (universe + held)[:WATCH_TOP]


def _universe_rows(cache: dict):
    """解析全市场行情快照 → (有效样本 rows, 覆盖统计 stats)。

    口径：只采纳「最新快照日」且 price / prev_close 均有效的标的，
    避免把不同日期的涨跌幅混在一起统计；其余标的如实计入排除数。
    涨跌幅一律用 (price - prev_close) / prev_close 计算。
    """
    quotes = cache.get("quote") or {}
    funds = cache.get("fund") or {}
    if not isinstance(quotes, dict):
        quotes = {}
    if not isinstance(funds, dict):
        funds = {}
    all_rows, invalid = [], 0
    for code, entry in quotes.items():
        if not isinstance(entry, dict):
            continue
        d = entry.get("data") or {}
        if not isinstance(d, dict):
            continue
        try:
            price = float(d.get("price") or 0)
            prev = float(d.get("prev_close") or 0)
        except (TypeError, ValueError):
            price = prev = 0.0
        if price <= 0 or prev <= 0:
            invalid += 1
            continue
        chg = (price - prev) / prev * 100.0
        if not -30.0 < chg < 30.0:      # 脏数据 / 复权跳变，排除
            invalid += 1
            continue
        as_of = str(entry.get("date") or "")[:10] or str(d.get("time") or "")[:10]
        name = str(d.get("name") or "").strip() or str(code)
        industry = ""
        f = funds.get(code)
        if isinstance(f, dict):
            industry = str((f.get("data") or {}).get("industry") or "").strip()
        all_rows.append({
            "code": str(code),
            "name": name,
            "change_pct": round(chg, 2),
            "as_of": as_of,
            "industry": industry,
        })
    by_date = {}
    for r in all_rows:
        k = r["as_of"] or "未知"
        by_date[k] = by_date.get(k, 0) + 1
    dated = sorted(k for k in by_date if k != "未知")
    fresh = dated[-1] if dated else ""
    if fresh:
        rows = [r for r in all_rows if r["as_of"] == fresh]
        stale = len(all_rows) - len(rows)
    else:
        rows, stale = all_rows, 0
    stats = {
        "scope": UNIVERSE_SCOPE,
        "pool_total": len(quotes),
        "valid_total": len(all_rows),
        "sample": len(rows),
        "snapshot_date": fresh or None,
        "stale_excluded": stale,
        "invalid_excluded": invalid,
        "by_date": by_date,
        "source": "westock 全市场快照（15:35 收盘流程预热，本地缓存）",
    }
    return rows, stats


def _market_breadth(rows: list) -> dict:
    """全市场涨跌宽度：涨跌家数、涨跌停、大涨大跌、均值/中位数、领涨领跌个股。"""
    chgs = [r["change_pct"] for r in rows if isinstance(r.get("change_pct"), (int, float))]
    up = sum(1 for x in chgs if x > 0.005)
    down = sum(1 for x in chgs if x < -0.005)
    ordered = sorted(rows, key=lambda r: r.get("change_pct") or 0, reverse=True)

    def _mini(r):
        return {k: r.get(k) for k in ("code", "name", "change_pct", "industry", "as_of")}

    return {
        "sample": len(chgs),
        "up": up,
        "down": down,
        "flat": len(chgs) - up - down,
        "limit_up": sum(1 for x in chgs if x >= LIMIT_MOVE),
        "limit_down": sum(1 for x in chgs if x <= -LIMIT_MOVE),
        "big_up": sum(1 for x in chgs if x >= BIG_MOVE),
        "big_down": sum(1 for x in chgs if x <= -BIG_MOVE),
        "avg_change": round(statistics.mean(chgs), 2) if chgs else None,
        "median_change": round(statistics.median(chgs), 2) if chgs else None,
        "leaders": [_mini(r) for r in ordered[:5]],
        "laggards": [_mini(r) for r in ordered[-5:][::-1]],
    }


def _sector_moves(rows: list) -> dict:
    """行业强弱：按行业平均涨跌幅排序，样本不足 SECTOR_MIN 的行业不参与。"""
    buckets = {}
    for r in rows:
        ind = r.get("industry")
        chg = r.get("change_pct")
        if not ind or not isinstance(chg, (int, float)):
            continue
        buckets.setdefault(ind, []).append(chg)
    items = [{"industry": k, "count": len(v),
              "avg_change": round(statistics.mean(v), 2)}
             for k, v in buckets.items() if len(v) >= SECTOR_MIN]
    items.sort(key=lambda x: x["avg_change"], reverse=True)
    return {"leaders": items[:3], "laggards": items[-3:][::-1],
            "tracked": len(items)}


def _universe_watchlist(rows: list, live: dict, held_codes: set) -> list:
    """全市场异动榜：|涨跌幅| ≥UNIVERSE_MOVE，涨跌两侧配额、非持仓优先。
    样本不足时用持仓实时快照异动补足，并如实标注 scope 与快照日。"""
    moved = [r for r in rows if abs(r.get("change_pct") or 0) >= UNIVERSE_MOVE]
    gainers = sorted([r for r in moved if r["change_pct"] > 0],
                     key=lambda r: -r["change_pct"])
    losers = sorted([r for r in moved if r["change_pct"] < 0],
                    key=lambda r: r["change_pct"])
    half = WATCH_TOP // 2
    picked = gainers[:half] + losers[:half]
    if len(picked) < WATCH_TOP:
        rest = sorted(gainers[half:] + losers[half:],
                      key=lambda r: -abs(r["change_pct"]))
        picked += rest[:WATCH_TOP - len(picked)]
    # 非持仓优先（避免观察池被自己的持仓占满），组内按波动绝对值降序
    picked.sort(key=lambda r: (r["code"] in held_codes, -abs(r["change_pct"])))
    out, seen = [], set()
    for r in picked:
        code = r["code"]
        out.append({
            "code": code,
            "name": r.get("name"),
            "change_pct": round(r["change_pct"], 2),
            "scope": "持仓" if code in held_codes else "全市场",
            "industry": r.get("industry") or "",
            "as_of": r.get("as_of") or "",
        })
        seen.add(code)
    if len(out) < WATCH_TOP:
        for h in _watchlist(live, held_codes):
            if h["code"] in seen:
                continue
            row = dict(h)
            row.setdefault("industry", "")
            row.setdefault("as_of", "")
            out.append(row)
            seen.add(h["code"])
            if len(out) >= WATCH_TOP:
                break
    return out


def _data_basis(live_meta: dict) -> str:
    """判定持仓行情快照基准：盘前 08:30 尚无当日行情，缓存仍是上一交易日收盘快照。
    如实标注，避免把「昨收涨跌」误称为「当日盘前涨跌」——模拟真实。"""
    gen = (live_meta.get("generated_at") or "")[:10]
    today = date.today().isoformat()
    if gen == today:
        return "当日实时快照"
    if gen:
        return f"上一交易日收盘快照（{gen}）"
    return "历史快照（时点未知）"


def _universe_basis(stats: dict) -> str:
    d = stats.get("snapshot_date")
    if not d:
        return "全市场快照（时点未知）"
    return (f"全市场快照 {d}（{stats.get('sample', 0)} 只有效 / "
            f"候选池 {stats.get('pool_total', 0)} 只）")


def _summary(regime: str, holdings: list, watch: list,
             live_meta: dict, basis: str, breadth: dict,
             sectors: dict, stats: dict) -> str:
    lines = []
    label = {"BULL": "偏多", "OSCILLATION": "震荡", "BEAR": "偏空",
             "UNKNOWN": "研判中"}.get(regime, regime)
    lines.append(f"盘前风格研判：{label}（{regime}，沿用智能体最近收盘复盘研判）。")
    lines.append(f"持仓数据基准：{basis}；全市场基准：{_universe_basis(stats)}。")
    sample = breadth.get("sample", 0)
    if sample:
        lines.append(
            f"全市场涨跌：{breadth['up']} 涨 / {breadth['down']} 跌 / "
            f"{breadth['flat']} 平，平均 {breadth['avg_change']:+}%、"
            f"中位 {breadth['median_change']:+}%；"
            f"涨停 {breadth['limit_up']} / 跌停 {breadth['limit_down']}，"
            f"大涨(≥3%) {breadth['big_up']} / 大跌(≤-3%) {breadth['big_down']}。")
        if breadth.get("leaders"):
            t = breadth["leaders"][0]
            lines.append(f"领涨：{t['name']}（{t['change_pct']:+}%）；"
                         f"领跌：{breadth['laggards'][0]['name']}"
                         f"（{breadth['laggards'][0]['change_pct']:+}%）。")
    else:
        lines.append("全市场快照暂不可用，本期仅展示持仓口径（不虚构行情）。")
    sec_up = (sectors or {}).get("leaders") or []
    sec_dn = (sectors or {}).get("laggards") or []
    if sec_up and sec_dn:
        lines.append(f"行业强弱：{sec_up[0]['industry']}（{sec_up[0]['avg_change']:+}%）领涨，"
                     f"{sec_dn[0]['industry']}（{sec_dn[0]['avg_change']:+}%）领跌。")
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
    if stats.get("stale_excluded") or stats.get("invalid_excluded"):
        lines.append(f"另有 {stats.get('stale_excluded', 0)} 只较旧快照、"
                     f"{stats.get('invalid_excluded', 0)} 只无效行情未纳入统计。")
    gen = (live_meta.get("generated_at") or "").replace("T", " ")
    if gen:
        lines.append(f"持仓快照时点：{gen}（调度守护每 3s 刷新）。")
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
    cache = _load_json(WESTOCK_CACHE_JSON, {})
    live_meta = live.get("_meta", {}) or {}

    # 全市场聚合整体降级：任何异常都不影响持仓口径与摘要产出
    try:
        rows, stats = _universe_rows(cache)
        breadth = _market_breadth(rows)
        sectors = _sector_moves(rows)
    except Exception as e:
        _log(f"全市场快照聚合异常（降级为持仓口径）: {e}")
        rows, stats, breadth, sectors = [], {}, {}, {}

    out = {
        "trading_day": today.isoformat(),
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source": live_meta.get("source", "westock") + " (调度守护实盘快照·本地缓存)",
        "snapshot_at": live_meta.get("generated_at"),
        "data_basis": _data_basis(live_meta),
        "universe_basis": _universe_basis(stats),
        "universe": stats,
        "market_breadth": breadth,
        "sectors": sectors,
        "regime": _regime_from_learned(learned),
        "holdings": [],
        "watchlist": [],
        "learner_state": {},
        "summary": "",
    }
    out["holdings"] = _holdings_pre(account, live)
    held_codes = {h["code"] for h in out["holdings"]}
    try:
        out["watchlist"] = _universe_watchlist(rows, live, held_codes)
    except Exception as e:
        _log(f"全市场异动榜异常（降级为持仓口径）: {e}")
        out["watchlist"] = _watchlist(live, held_codes)
    out["learner_state"] = _learner_state(learned)
    out["summary"] = _summary(out["regime"], out["holdings"],
                              out["watchlist"], live_meta, out["data_basis"],
                              breadth, sectors, stats)

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
    _log(f"完成：regime={out['regime']} 全市场{stats.get('sample', 0)}只 "
         f"持仓{len(out['holdings'])} 异动{len(out['watchlist'])}（零网络，毫秒级）")
    return f"OK -> {PRE_MARKET_JSON}"


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="忽略幂等标记，强制重跑盘前检索")
    args = ap.parse_args()
    _log(f"盘前信息检索开始（交易日={date.today().isoformat()}，全市场口径）...")
    _log(run_pre_market(force=args.force))


if __name__ == "__main__":
    main()
