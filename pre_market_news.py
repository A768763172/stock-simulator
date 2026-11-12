"""stock-simulator · 盘前资讯采集（政策要闻 / 公司公告 / 财经热文）。

由 scheduler 在交易日 08:45 以**独立子进程 + 硬超时**调用，落盘
data/pre_market_news.json，再由 pre_market.py（零网络）汇总进盘前摘要。

为什么单独成脚本：
  - 盘前网络调用是历史上把 09:30 开盘循环拖垮的根因。把联网部分隔离到
    独立进程，主调度器只做「限时等待 + 失败照常往下走」，任何源挂掉都
    不会影响开盘；pre_market.py 本身继续坚持零网络、只读本地文件。
  - 复用系统已有的 westock CLI（与行情同一数据源，无需引入外部网站 / API Key）：
      westock news list sh000001,sz399001 --type 3 --limit N  → 政策与市场要闻
      westock notice list <持仓代码...> --limit N              → 公司公告
      westock hot news --limit N                              → 财经热文榜

真实性：只展示 CLI 真实返回的标题 / 时间 / 来源 / 链接，绝不生成或补写内容；
每个源单独记录成功 / 失败状态与条数，失败时看板如实提示「资讯源未就绪」。
按交易日幂等标记 .pre_market_news_done.<date>，可用 --force 强制重采。
"""
import os
import glob
import json
import subprocess
from datetime import date, datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(HERE, "data")
ACCOUNT_JSON = os.path.join(DATA_DIR, "account.json")
OUT_JSON = os.path.join(DATA_DIR, "pre_market_news.json")

WESTOCK_DEFAULT = "C:/Users/Administrator/.workbuddy/binaries/westock/westock.exe"
CLI_TIMEOUT = 25          # 单个 CLI 调用硬超时（秒），失败立即跳过该源
RECENT_HOURS = 30         # 只保留近 30 小时：覆盖昨夜公告与今晨政策，不含旧闻
TOP_N = 10                # 每组最多展示条数

POLICY_KW = (
    "证监会", "发改委", "财政部", "央行", "人民银行", "国务院", "工信部",
    "金融监管", "政治局", "国常会", "政策", "监管", "印发", "通知", "意见",
    "办法", "规划", "条例", "降准", "降息", "利率", "关税", "税", "补贴",
    "发声", "部署", "表态", "会议", "草案", "新规",
)


def _log(msg):
    print(f"[pre_market_news] {msg}", flush=True)


def _cli():
    return WESTOCK_DEFAULT if os.path.exists(WESTOCK_DEFAULT) else "westock"


def _load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f) or default
    except Exception:
        return default


def _parse_md(text: str, title_field: str) -> list:
    """解析 westock 的 Markdown 表格（含多段表、标题里带 | 的情况）。

    notice list 会按标的分段重复输出表头；hot news 的标题常含 `|`
    （如「一文看市|A股…」），直接按 | split 会错列，故把多出的列并回标题列。
    """
    rows, headers = [], None
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if headers is None:
            headers = cells
            continue
        if cells == headers:
            continue                                  # 分段重复表头
        if all(c and set(c) <= set("-: ") for c in cells):
            continue                                  # 分隔行
        if len(cells) > len(headers) and title_field in headers:
            i = headers.index(title_field)
            extra = len(cells) - len(headers)
            cells = (cells[:i] + ["|".join(cells[i:i + extra + 1])]
                     + cells[i + extra + 1:])
        if len(cells) != len(headers):
            headers = cells                           # 表头切换
            continue
        rows.append(dict(zip(headers, cells)))
    return rows


def _run(args, title_field):
    cmd = [_cli(), *args]
    res = subprocess.run(cmd, capture_output=True, text=True,
                         encoding="utf-8", timeout=CLI_TIMEOUT)
    if res.returncode != 0 or not (res.stdout or "").strip():
        raise RuntimeError(((res.stderr or "") + (res.stdout or "")).strip()[-160:]
                           or f"退出码 {res.returncode}")
    rows = _parse_md(res.stdout, title_field)
    if not rows:
        raise RuntimeError("返回内容中无有效表格行")
    return rows


def _ts(value) -> str:
    """统一时间字符串；hot news 给的是 Unix 秒，其余是 'YYYY-MM-DD HH:MM:SS'。"""
    s = str(value or "").strip()
    if not s:
        return ""
    if s.isdigit():
        try:
            return datetime.fromtimestamp(int(s)).strftime("%Y-%m-%d %H:%M")
        except (ValueError, OSError, OverflowError):
            return ""
    return s[:16]


def _recent(row, key) -> bool:
    """近 RECENT_HOURS 小时内；时间无法解析的保留（宁可多展示也不误删真实内容）。"""
    s = _ts(row.get(key))
    if not s:
        return True
    try:
        t = datetime.strptime(s, "%Y-%m-%d %H:%M")
    except ValueError:
        return True
    return t >= datetime.now() - timedelta(hours=RECENT_HOURS)


def _item(row, keys) -> dict:
    out = {}
    for dst, src in keys.items():
        v = row.get(src, "")
        out[dst] = _ts(v) if dst == "time" else (v or "")
    out["title"] = str(out.get("title") or "").strip()
    return out


def _dedup(items, limit=TOP_N):
    seen, out = set(), []
    for it in items:
        t = it.get("title")
        if not t or t in seen:
            continue
        seen.add(t)
        out.append(it)
        if len(out) >= limit:
            break
    return out


def _holding_codes() -> list:
    acc = _load_json(ACCOUNT_JSON, {})
    return [c for c in (acc.get("positions") or {}) if isinstance(c, str)][:12]


def collect() -> dict:
    today = date.today().isoformat()
    out = {
        "trading_day": today,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source": "westock 资讯 / 公告接口（盘前主动采集，独立子进程限时）",
        "sources": {},
        "policy": [], "market_news": [], "notices": [], "hot": [],
        "available": False,
    }
    src = out["sources"]

    def _source(key, label, args, title_field, handler):
        try:
            rows = _run(args, title_field)
            items = handler(rows)
            src[key] = {"label": label, "ok": True, "count": len(items),
                        "error": None}
            _log(f"{label}：采集 {len(rows)} 行，保留 {len(items)} 条")
            return items
        except Exception as e:
            src[key] = {"label": label, "ok": False, "count": 0,
                        "error": f"{type(e).__name__}: {str(e)[:120]}"}
            _log(f"{label}：源未就绪（{src[key]['error']}），跳过且不阻塞")
            return []

    # 1) 政策与市场要闻（指数代码 = 市场资讯入口）
    news_rows = _source(
        "news", "政策与市场要闻",
        ["news", "list", "sh000001,sz399001", "--type", "3", "--limit", "30"],
        "title", lambda rows: [r for r in rows if _recent(r, "time")])
    # 多指数代码的返回会按来源分段，需按真实时间全局重排，避免旧闻排在新政前面
    news_rows.sort(key=lambda r: _ts(r.get("time")), reverse=True)
    policy, market = [], []
    for r in news_rows:
        it = _item(r, {"title": "title", "time": "time",
                       "source": "src", "url": "url"})
        it["tag"] = "政策" if any(k in it["title"] for k in POLICY_KW) else "要闻"
        (policy if it["tag"] == "政策" else market).append(it)
    out["policy"] = _dedup(policy)
    out["market_news"] = _dedup(market)

    # 2) 公司公告（持仓股，最多 12 只一次批量查询）
    codes = _holding_codes()
    if codes:
        # 公告不做时间过滤：持仓股可能数日无新公告，过滤会让板块永远空白。
        # 保留接口返回的真实时间，看板如实显示「最近一期公告」是哪天。
        notice_items = _source(
            "notice", "公司公告",
            ["notice", "list", ",".join(codes), "--limit", "6"],
            "title",
            lambda rows: [_item(r, {"title": "title", "time": "time",
                                    "source": "symbol", "url": "url",
                                    "code": "symbol"}) for r in rows])
        # notice list 按标的分段返回，不全局排序会让第一只持仓的旧公告占满列表；
        # 按真实时间倒序后取前 N，才是「所有持仓中最近发布的公告」。
        notice_items.sort(key=lambda x: x.get("time") or "", reverse=True)
        out["notices"] = _dedup(notice_items)
    else:
        src["notice"] = {"label": "公司公告", "ok": False, "count": 0,
                         "error": "当前无持仓，无需查询个股公告"}

    # 3) 财经热文榜（政策/市场情绪补充）
    hot_items = _source(
        "hot", "财经热文榜", ["hot", "news", "--limit", "12"], "news_title",
        lambda rows: [_item(r, {"title": "news_title", "time": "publish_time",
                                "source": "source", "url": "url"})
                      for r in rows if _recent(r, "publish_time")])
    out["hot"] = _dedup(hot_items)

    out["available"] = any(v.get("ok") and v.get("count") for v in src.values())
    ok = sum(1 for v in src.values() if v.get("ok"))
    total = sum(len(out[k]) for k in ("policy", "market_news", "notices", "hot"))
    out["note"] = (f"盘前资讯采集：{ok}/{len(src)} 个数据源就绪，共 {total} 条；"
                   f"要闻与热文取近 {RECENT_HOURS} 小时，公告为接口返回的最新一期"
                   f"（均保留真实时间，仅展示行情源返回内容）。")
    return out


def run(force: bool = False) -> str:
    today = date.today()
    done_flag = os.path.join(DATA_DIR, f".pre_market_news_done.{today.isoformat()}")
    if os.path.exists(done_flag) and not force:
        return f"SKIP (already collected for {today.isoformat()})"
    try:
        out = collect()
    except Exception as e:
        # 兜底：即使整体异常也要落盘一份「诚实失败」的结果供看板显示
        _log(f"采集整体异常（降级）: {e}")
        out = {
            "trading_day": today.isoformat(),
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "source": "westock 资讯 / 公告接口", "sources": {},
            "policy": [], "market_news": [], "notices": [], "hot": [],
            "available": False, "note": f"资讯采集失败：{type(e).__name__}: {str(e)[:120]}",
        }
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    with open(done_flag, "w") as f:
        f.write("ok")
    try:
        for old in glob.glob(os.path.join(DATA_DIR, ".pre_market_news_done.*")):
            if old != done_flag:
                os.remove(old)
    except Exception:
        pass
    _log(out["note"])
    return f"OK -> {OUT_JSON}"


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="忽略幂等标记，强制重新采集")
    args = ap.parse_args()
    _log(f"盘前资讯采集开始（交易日={date.today().isoformat()}）...")
    _log(run(force=args.force))


if __name__ == "__main__":
    main()
