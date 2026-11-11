"""数据源稳定性与实时性 · 实时探针（针对 WeStockProvider / westock.exe）。

设计原则：
- 缓存隔离到临时目录，不扰动在线系统的 data/.westock_cache.json。
- 所有指标为"现跑实测"，不引用任何历史监控（系统无该基础设施）。
- 仅测量，不改写任何在线数据/日志。
"""
import os, sys, time, json, subprocess, statistics
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

EXE = r"C:\Users/Administrator\.workbuddy\binaries\westock\westock.exe"
CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".probe_cache")
os.makedirs(CACHE_DIR, exist_ok=True)

CODES = ["sh601000", "sh601668", "sh600511", "sh600900", "sh601816",
         "sh600519", "sh601318", "sh000300"]

R = {}  # 结果收集


def raw_call(args, timeout=45):
    """直接 subprocess 调用 westock CLI，返回 (ok, dt, rc, out_len, err)。"""
    t0 = time.time()
    try:
        r = subprocess.run([EXE, *args], capture_output=True, text=True,
                           timeout=timeout, encoding="utf-8", errors="ignore")
        dt = time.time() - t0
        out = (r.stdout or "")
        ok = (r.returncode == 0) and bool(out.strip())
        return {"ok": ok, "dt": dt, "rc": r.returncode,
                "out_len": len(out), "err": (r.stderr or "")[:160]}
    except subprocess.TimeoutExpired:
        return {"ok": False, "dt": time.time() - t0, "rc": -1,
                "out_len": 0, "err": "TIMEOUT", "timeout": True}
    except Exception as e:  # noqa
        return {"ok": False, "dt": time.time() - t0, "rc": -2,
                "out_len": 0, "err": str(e)[:160]}


def pct(vals, p):
    if not vals:
        return 0.0
    s = sorted(vals)
    k = max(0, min(len(s) - 1, int(round((p / 100.0) * (len(s) - 1)))))
    return s[k]


# ---------- 1) 服务可用率 + 时延分布（重复调用 quote）----------
N = 16
print(f"[probe] 1) 可用率/时延：{N} 次 quote 调用 ...", flush=True)
samples = []
for i in range(N):
    c = CODES[i % len(CODES)]
    samples.append(raw_call(["quote", c]))
ok_n = sum(1 for s in samples if s["ok"])
lats = [s["dt"] for s in samples if s["ok"]]
timeouts = sum(1 for s in samples if s.get("timeout"))
errs = sum(1 for s in samples if (not s["ok"]) and (not s.get("timeout")))
R["availability"] = {
    "total": N, "ok": ok_n,
    "availability_pct": round(ok_n / N * 100, 2),
    "timeout_cnt": timeouts, "error_cnt": errs,
    "latency_min_s": round(min(lats), 3) if lats else None,
    "latency_p50_s": round(pct(lats, 50), 3) if lats else None,
    "latency_p95_s": round(pct(lats, 95), 3) if lats else None,
    "latency_max_s": round(max(lats), 3) if lats else None,
    "latency_mean_s": round(statistics.mean(lats), 3) if lats else None,
    "raw": samples,
}

# ---------- 2) 接口覆盖耗时（kline / fundamentals / calendar / index）----------
print("[probe] 2) 接口覆盖耗时 ...", flush=True)
mp = __import__("market.westock_provider", fromlist=["WeStockProvider"])
prov = mp.WeStockProvider(cli=EXE, cache_dir=CACHE_DIR)
cov = {}
for label, fn in [
    ("kline_day_20", lambda: prov.get_kline("sh601000", "day", 20)),
    ("fundamentals", lambda: prov.get_fundamentals("sh601000")),
    ("trade_calendar", lambda: prov.is_trading_day(__import__("datetime").date.today())),
    ("index_constituents_300", lambda: prov.get_index_constituents("sh000300")),
]:
    t0 = time.time()
    try:
        v = fn()
        cov[label] = {"ok": True, "dt_s": round(time.time() - t0, 3),
                      "sample": (len(v) if hasattr(v, "__len__") else str(v)[:40])}
    except Exception as e:  # noqa
        cov[label] = {"ok": False, "dt_s": round(time.time() - t0, 3),
                      "err": str(e)[:160]}
R["coverage"] = cov

# ---------- 3) 并发承载（10 路并行 quote）----------
print("[probe] 3) 并发承载：10 路并行 quote ...", flush=True)
cc = ["sh601000", "sh601668", "sh600511", "sh600900", "sh601816",
      "sh600519", "sh601318", "sh000300", "sh601988", "sh601398"]
t0 = time.time()
with ThreadPoolExecutor(max_workers=10) as ex:
    cres = list(ex.map(lambda c: raw_call(["quote", c], timeout=45), cc))
wall = time.time() - t0
cok = sum(1 for s in cres if s["ok"])
R["concurrency"] = {
    "workers": 10, "wall_s": round(wall, 3),
    "ok": cok, "success_pct": round(cok / len(cc) * 100, 2),
    "effective_qps": round(len(cc) / wall, 3) if wall > 0 else None,
    "raw": cres,
}

# ---------- 4) 429 限流行为（同标的突发 6 连发，观察重试/退避恢复）----------
print("[probe] 4) 429 限流突发 6 连发 ...", flush=True)
burst = [raw_call(["quote", "sh601000"], timeout=45) for _ in range(6)]
bok = sum(1 for s in burst if s["ok"])
R["rate_limit"] = {
    "calls": 6, "ok": bok, "success_pct": round(bok / 6 * 100, 2),
    "any_timeout": any(s.get("timeout") for s in burst),
    "max_single_dt_s": round(max(s["dt"] for s in burst), 3),
    "raw": burst,
}

# ---------- 5) 缓存一致性 + 全链路时延分段 ----------
print("[probe] 5) 缓存一致性与全链路时延分段 ...", flush=True)
from market.feed import validate_quote, MarketFeed

# 5a 清洗（MarketFeed.validate_quote）微基准
t0 = time.time()
for _ in range(2000):
    validate_quote(4.38, 4.30, 4.82, 3.98)
clean_us = (time.time() - t0) / 2000 * 1e6

# 5b 接口输出（缓存命中）时延：先 warm 缓存，再读
prov2 = mp.WeStockProvider(cli=EXE, cache_dir=CACHE_DIR)
t0 = time.time()
q = prov2.get_quote("sh601000")   # 首次可能触网
first_dt = time.time() - t0
t0 = time.time()
q2 = prov2.get_quote("sh601000")  # 缓存命中
cache_dt = time.time() - t0

# 5c 一致性：连续 5 次读缓存，价格应一致且即时
cons = []
for _ in range(5):
    t0 = time.time()
    qq = prov2.get_quote("sh601000")
    cons.append({"price": qq.price, "dt_s": round(time.time() - t0, 5)})
same_price = len(set(c["price"] for c in cons)) == 1

R["e2e"] = {
    "clean_validate_us": round(clean_us, 3),
    "get_quote_first_hit_s": round(first_dt, 3),
    "get_quote_cache_hit_s": round(cache_dt, 5),
    "consistency_same_price": same_price,
    "consistency_prices": cons,
    "collect_latency_from_avail_p50_s": R["availability"]["latency_p50_s"],
}

# ---------- 6) 时效准确性（业务时间戳 vs 抓取时刻）----------
print("[probe] 6) 时效准确性 ...", flush=True)
q3 = prov2.get_quote("sh600519")
now_iso = datetime.now().isoformat(timespec="seconds")
R["freshness"] = {
    "quote_time_field": q3.time,
    "fetched_at": now_iso,
    "time_field_present": bool(q3.time),
    "note": "westock 返回的 time 字段为行情交易所时间戳；本探针抓取时刻为 fetched_at。"
}

# 汇总
R["meta"] = {
    "exe": EXE,
    "probe_at": datetime.now().isoformat(timespec="seconds"),
    "cache_dir": CACHE_DIR,
    "codes": CODES,
    "note": "所有指标为现跑实测；系统无 3 个月历史监控，MTBF/MTTR/灾备等指标不在本次实测范围。",
}
out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "probe_results.json")
with open(out, "w", encoding="utf-8") as f:
    json.dump(R, f, ensure_ascii=False, indent=2, default=str)
print(f"[probe] DONE -> {out}", flush=True)
print(json.dumps({k: v for k, v in R.items()
                  if k not in ("availability", "concurrency", "rate_limit")},
                 ensure_ascii=False, indent=2, default=str))
