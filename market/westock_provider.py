"""westock-data 行情提供器（WeStockProvider）。

对接 westock-data 技能：通过 `westock` CLI 获取 A股实时行情 / K线 / 基本面 / 交易日历。
统一调用入口（与 westock CLI v0.0.4 对齐）：
  - 实时行情：   westock quote <code>
  - K线：        westock kline <code> --period day --limit <n>
  - 基本面：     westock finance <code> --fields core   （三大表）
  - 简况(行业)： westock profile <code>
  - 交易日历：   westock trade-calendar --date <YYYY-MM-DD>
  - 指数成分：   westock index constituent <index_code>

解析 westock 默认输出的 Markdown 表格。A股 quote 直接返回 price_ceiling / price_floor，
撮合引擎据此执行涨跌幅限制，无需自行计算。

本地缓存：行情接口对全市场扫描代价高，且 westock 有速率限制（429）。
提供器内置日期级缓存（落盘 data/.westock_cache.json）：
  - 基本面(fund)   缓存 7 天（财务/行业变化慢）
  - 实时价(quote)  缓存当天（同一次运行内复用，避免重复请求）
  - 收盘价(close)  缓存当天（每日刷新，供波动率/动量/估值分位计算）
  - 指数成分(idx)  缓存 7 天
  - 交易日历(cal)  缓存 30 天
缓存与引擎 data/ 同目录，reset() 不会清除（保留以加速后续运行）。

所有数据本地解析，不涉及任何上传。
"""
import json
import os
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import date, datetime, timedelta
from typing import Dict, Any, List, Tuple

from market.base import MarketProvider, Quote, Kline, Fundamentals


class WeStockProvider(MarketProvider):
    def __init__(self, cli: str = "westock", cache_dir: str = None):
        self.cli = cli
        self._cal_cache: Dict[str, bool] = {}
        # 缓存文件锚定到项目 data/（与 LocalStore 一致）
        if cache_dir:
            self._cache_path = os.path.abspath(cache_dir)
        else:
            root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            self._cache_path = os.path.join(root, "data", ".westock_cache.json")
        self._cache: Dict[str, Any] = self._load_cache()

    # ---------- 缓存 ----------
    def _load_cache(self) -> dict:
        try:
            with open(self._cache_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def save_cache(self) -> None:
        try:
            os.makedirs(os.path.dirname(self._cache_path), exist_ok=True)
            with open(self._cache_path, "w", encoding="utf-8") as f:
                json.dump(self._cache, f, ensure_ascii=False)
        except Exception:
            pass

    def _today(self) -> str:
        return date.today().isoformat()

    def _get(self, bucket: str, key: str, ttl_days: int, fetcher):
        b = self._cache.setdefault(bucket, {})
        entry = b.get(key)
        if entry:
            ed = entry.get("date")
            try:
                if ed and (date.today() - date.fromisoformat(ed)).days < ttl_days:
                    return entry["data"]
            except Exception:
                pass
        data = fetcher()
        b[key] = {"date": self._today(), "data": data}
        return data

    # ---------- CLI 调用（带重试/退避，缓解 429） ----------
    def _run(self, *args: str, retries: int = 5, timeout: int = 40) -> str:
        """调用 westock CLI。

        timeout/retries 可配：盘中实时决策用「快速模式」（短超时 + 少重试），
        避免单只脏源卡满 5 次重试 × 40s 独占主循环；收盘流程仍用默认宽松值。
        """
        delay = 1.0
        last_err = ""
        for i in range(retries + 1):
            try:
                # 显式指定 encoding="utf-8"：westock CLI 输出 UTF-8 文本，
                # 不指定时 Windows 默认按 GBK(cp936) 解码，会把中文名变成乱码
                # （如「红利ETF华泰柏瑞」→「绾㈠埄ETF鍗庢嘲鏌忕憺」），并随缓存复用。
                res = subprocess.run(
                    [self.cli, *args], capture_output=True, text=True,
                    encoding="utf-8", timeout=timeout
                )
            except FileNotFoundError:
                raise RuntimeError(
                    f"未找到 westock CLI（{self.cli}），请先安装 westock-data 技能"
                )
            out = (res.stdout or "")
            err = (res.stderr or "")
            if res.returncode == 0 and out.strip():
                return out
            last_err = (err + out).strip()[-400:]
            slow = any(k in last_err.lower() for k in
                       ("429", "rate", "频繁", "too many", "limit", "timeout"))
            if i < retries:
                time.sleep(delay if not slow else delay * 2)
                delay = min(delay * 2, 30.0)
                continue
            raise RuntimeError(f"westock 执行失败: {' '.join(args)} -> {last_err}")
        raise RuntimeError(f"westock 执行失败(重试耗尽): {' '.join(args)} -> {last_err}")

    # ---------- Markdown 表格解析 ----------
    @staticmethod
    def _parse_all_rows(text: str) -> List[Tuple[list, dict]]:
        lines = [l.rstrip() for l in text.splitlines() if l.strip().startswith("|")]
        results = []
        headers = None
        for l in lines:
            cells = [c.strip() for c in l.strip("|").split("|")]
            if headers is None:
                headers = cells
                continue
            if all(set(c) <= set("-: ") and "-" in c for c in cells):
                continue  # 分隔行
            if len(cells) == len(headers):
                results.append((headers, dict(zip(headers, cells))))
            else:
                headers = cells  # 表头切换
        return results

    @staticmethod
    def _num(row: dict, *keys, default: float = 0.0) -> float:
        for k in keys:
            if k in row and row[k] not in ("", None):
                try:
                    return float(row[k])
                except (TypeError, ValueError):
                    return default
        return default

    @staticmethod
    def _suspended_flag(row: dict) -> bool:
        """从行情表识别停牌状态（交易状态/交易状态列含「停牌」或 suspend）。"""
        for k in ("status", "交易状态", "state"):
            v = (row.get(k) or "").strip().lower()
            if v and ("停牌" in v or "suspend" in v):
                return True
        return False

    # ---------- 接口实现 ----------
    def get_quote(self, code: str) -> Quote:
        def _fetch():
            out = self._run("quote", code)
            row = self._find_row(out, code)
            if row is None:
                raise RuntimeError(f"无法解析行情: {code}")
            return {
                "code": code,
                "name": row.get("name") or row.get("名称") or code,
                "price": self._num(row, "price", "最新价", "closePrice"),
                "prev_close": self._num(row, "prev_close", "昨收"),
                "open": self._num(row, "open", "今开"),
                "high": self._num(row, "high", "最高"),
                "low": self._num(row, "low", "最低"),
                "ceiling": self._num(row, "price_ceiling", "涨停价"),
                "floor": self._num(row, "price_floor", "跌停价"),
                "volume": self._num(row, "volume", "成交量"),
                "change_pct": self._num(row, "change_pct", "涨跌幅"),
                "time": row.get("time") or "",
                "pe_ratio": self._num(row, "pe_ratio", "市盈率"),
                "pb_ratio": self._num(row, "pb_ratio", "市净率"),
                "suspended": self._suspended_flag(row),
            }
        d = self._get("quote", code, 1, _fetch)
        return Quote(
            code=code, name=d["name"], price=d["price"], prev_close=d["prev_close"],
            open=d["open"], high=d["high"], low=d["low"],
            ceiling=d["ceiling"], floor=d["floor"], time=d["time"],
            volume=d["volume"], change_pct=d["change_pct"],
            suspended=d["suspended"],
        )

    def get_kline(self, code: str, period: str = "day", limit: int = 20) -> List[Kline]:
        out = self._run("kline", code, "--period", period, "--limit", str(limit))
        kl = []
        for _, r in self._parse_all_rows(out):
            try:
                kl.append(Kline(
                    date=r.get("date") or r.get("日期") or "",
                    open=self._num(r, "open", "今开"),
                    close=self._num(r, "last", "close", "收盘"),
                    high=self._num(r, "high", "最高"),
                    low=self._num(r, "low", "最低"),
                    volume=self._num(r, "volume", "成交量"),
                    amount=self._num(r, "amount", "成交额"),
                    change_pct=self._num(r, "change_pct", "涨跌幅"),
                ))
            except (TypeError, ValueError):
                continue
        # westock 返回最新在前，因子/回测均按「旧→新」顺序，故反转
        kl.reverse()
        return kl

    def _raw_close_history(self, code: str, days: int = 126) -> List[float]:
        kl = self.get_kline(code, period="day", limit=days)
        return [k.close for k in kl if k.close > 0] if kl else []

    def get_close_history(self, code: str, days: int = 126) -> List[float]:
        # 缓存统一以 code 为键（存满 126 日），取用时按 days 截断，
        # 让一次预热覆盖所有回看窗口；TTL 7 天，日常运行极少触网。
        full = self._get("close", code, 7, lambda: self._raw_close_history(code, 126))
        return full[-days:] if days and days < len(full) else full

    # ---------- 并行预热（解决全市场扫描的高延迟 / 429） ----------
    def _raw_kline_closes_amounts(self, code: str, limit: int = 126):
        """返回 (closes, amounts)，westock 最新在前需反转。"""
        out = self._run("kline", code, "--period", "day", "--limit", str(limit))
        closes, amounts = [], []
        for _, r in self._parse_all_rows(out):
            c = self._num(r, "last", "close", "收盘")
            if c > 0:
                closes.append(c)
            a = self._num(r, "amount", "成交额")
            if a > 0:
                amounts.append(a)
        closes.reverse(); amounts.reverse()
        return closes, amounts

    def _fetch_fundamentals_raw(self, code: str) -> dict:
        """并行抓取单只股票的全部基本面子资源，返回待缓存的原始字典。"""
        def _profile():
            try:
                rows = self._parse_all_rows(self._run("profile", code))
                return rows[0][1] if rows else {}
            except Exception:
                return {}
        def _finance():
            try:
                flat: Dict[str, str] = {}
                for _, r in self._parse_all_rows(
                        self._run("finance", code, "--fields", "core")):
                    flat.update(r)
                return flat
            except Exception:
                return {}
        def _quote():
            try:
                out = self._run("quote", code)
                return self._find_row(out, code) or {}
            except Exception:
                return {}
        def _kline():
            try:
                return self._raw_kline_closes_amounts(code, 126)
            except Exception:
                return ([], [])
        with ThreadPoolExecutor(max_workers=4) as ex:
            fp, ff, fq, fk = (ex.submit(_profile), ex.submit(_finance),
                              ex.submit(_quote), ex.submit(_kline))
            pr, flat, qd, (closes, amounts) = (fp.result(), ff.result(),
                                               fq.result(), fk.result())
        industry = pr.get("industry") or pr.get("行业") or ""
        name = pr.get("name") or pr.get("名称") or code or qd.get("name") or code
        roe_ttm = self._num(flat, "ROETTM", "ROE", "ROEWeighted")
        gross_margin = self._num(flat, "GrossIncomeRatio", "毛利率")
        net_profit_ttm = self._num(flat, "NPParentCompanyOwnersTTM", "净利润TTM",
                                   "NPParentCompanyOwners")
        ocf = self._num(flat, "经营活动现金流量净额", "OCF", "NetOperatingCashFlow")
        price = self._num(qd, "price", "最新价", "closePrice")
        pe_val = self._num(qd, "pe_ratio", "市盈率")
        pb_val = self._num(qd, "pb_ratio", "市净率")
        pe_pct = pb_pct = 0.5
        if len(closes) >= 5 and price > 0:
            le = sum(1 for c in closes if c <= price)
            pe_pct = pb_pct = le / len(closes)
        avg_turn = sum(amounts[-30:]) / len(amounts[-30:]) if amounts else 0.0
        return {
            "code": code, "name": name, "industry": industry,
            "roe_3y": [roe_ttm, roe_ttm, roe_ttm],
            "ocf": ocf,
            "net_profit": net_profit_ttm,
            "net_profit_ttm": net_profit_ttm,
            "gross_margin_series": [gross_margin, gross_margin, gross_margin],
            "pe": pe_val, "pb": pb_val,
            "pe_pct_3y": pe_pct, "pb_pct_3y": pb_pct,
            "pledge_ratio": 0.0,
            "avg_turnover_30d": avg_turn,
            "is_st": ("st" in str(name).lower()),
            # 一并缓存 quote / close，供行情刷新与因子计算复用
            "quote": {
                "code": code, "name": name,
                "price": price,
                "prev_close": self._num(qd, "prev_close", "昨收"),
                "open": self._num(qd, "open", "今开"),
                "high": self._num(qd, "high", "最高"),
                "low": self._num(qd, "low", "最低"),
                "ceiling": self._num(qd, "price_ceiling", "涨停价"),
                "floor": self._num(qd, "price_floor", "跌停价"),
                "volume": self._num(qd, "volume", "成交量"),
                "change_pct": self._num(qd, "change_pct", "涨跌幅"),
                "time": qd.get("time") or "",
                "pe_ratio": pe_val, "pb_ratio": pb_val,
                "suspended": self._suspended_flag(qd),
            },
            "close": closes,
        }

    def warm_universe(self, codes: List[str], max_workers: int = 10,
                      refresh_fund: bool = False, skip_quote: bool = False,
                      max_seconds: float = None, cli_timeout: int = 40,
                      cli_retries: int = 5) -> int:
        """并行预热全市场缓存：quote(每日) + close(7天) + 可选 fundamentals(30天)。

        先并行抓取原始数据，回到主线程统一写入缓存，避免多线程缓存竞写。
        返回成功预热的股票数。

        skip_quote=True 时跳过实时价抓取（盘中实时价由 refresh_intraday 每 3s
        按需拉取持仓标的，无需在此预热全市场 quote，避免每日开盘无谓的 ~数百秒开销）。

        max_seconds：整批预热的墙钟总预算（秒）。到点未完成的标的直接跳过，
        保证任何脏源/网络抖动都不会让调用方（盘中决策）被独占超过该时长。
        cli_timeout/cli_retries：透传单只 CLI 的超时与重试；盘中快模式用
        短超时少重试，与 max_seconds 共同兜底。默认 None/40/5 保持原行为。
        """
        codes = list(dict.fromkeys(codes))
        today_d = date.today()
        today = today_d.isoformat()
        fund_b = self._cache.setdefault("fund", {})
        quote_b = self._cache.setdefault("quote", {})
        close_b = self._cache.setdefault("close", {})

        def _age(bucket: dict, code: str) -> int:
            try:
                return (today_d - date.fromisoformat(bucket.get(code, {}).get("date", "2000-01-01"))).days
            except Exception:
                return 9999

        def _work(code: str):
            # fundamentals：未缓存或超 30 天才抓取（最贵的 profile/finance）
            need_fund = (refresh_fund
                         or code not in fund_b
                         or _age(fund_b, code) >= 30)
            raw = None
            if need_fund:
                try:
                    raw = self._fetch_fundamentals_raw(code)
                except Exception:
                    raw = None
            # quote 每日刷新（1d TTL）；skip_quote 时跳过（盘中由 refresh_intraday 实时拉取）
            quote = None
            if not skip_quote:
                try:
                    qd = self._find_row(
                        self._run("quote", code, timeout=cli_timeout,
                                  retries=cli_retries), code) or {}
                    quote = {
                        "code": code,
                        "name": qd.get("name") or qd.get("名称") or code,
                        "price": self._num(qd, "price", "最新价", "closePrice"),
                        "prev_close": self._num(qd, "prev_close", "昨收"),
                        "open": self._num(qd, "open", "今开"),
                        "high": self._num(qd, "high", "最高"),
                        "low": self._num(qd, "low", "最低"),
                        "ceiling": self._num(qd, "price_ceiling", "涨停价"),
                        "floor": self._num(qd, "price_floor", "跌停价"),
                        "volume": self._num(qd, "volume", "成交量"),
                        "change_pct": self._num(qd, "change_pct", "涨跌幅"),
                        "time": qd.get("time") or "",
                        "pe_ratio": self._num(qd, "pe_ratio", "市盈率"),
                        "pb_ratio": self._num(qd, "pb_ratio", "市净率"),
                        "suspended": self._suspended_flag(qd),
                    }
                except Exception:
                    quote = None
            # close：fundamentals 已含则复用；否则近 6 天未刷新才补抓 kline
            closes = raw.get("close") if raw else []
            if not closes and _age(close_b, code) >= 6:
                try:
                    closes, _ = self._raw_kline_closes_amounts(code, 126)
                except Exception:
                    closes = []
            return code, raw, quote, closes

        def _commit(code, raw, quote, closes):
            if raw is not None:
                fund_b[code] = {"date": today, "data": raw}
                if raw.get("quote"):
                    quote_b[code] = {"date": today, "data": raw["quote"]}
                if raw.get("close"):
                    close_b[code] = {"date": today, "data": raw["close"]}
            if quote is not None:
                quote_b[code] = {"date": today, "data": quote}
            if closes:
                close_b[code] = {"date": today, "data": closes}

        done = 0
        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            fut_map = {ex.submit(_work, c): c for c in codes}
            if max_seconds is None:
                finished = set(fut_map)
            else:
                # 墙钟总预算：到点只提交已完成的，其余 cancel，
                # 保证脏源/抖动不会让盘中决策被长时间独占。
                finished, pending = wait(fut_map, timeout=max_seconds)
            for fut, code in fut_map.items():
                if fut not in finished:
                    fut.cancel()
                    continue
                try:
                    _code, raw, quote, closes = fut.result()
                except Exception:
                    continue
                _commit(_code, raw, quote, closes)
                done += 1
        self.save_cache()
        return done

    def get_fundamentals(self, code: str) -> Fundamentals:
        d = self._get("fund", code, 30, lambda: self._fetch_fundamentals_raw(code))
        qd = d.get("quote", {})
        nm = d.get("name") or qd.get("name") or code
        return Fundamentals(
            code=code, name=nm, industry=d.get("industry", ""),
            roe_3y=d.get("roe_3y", [0, 0, 0]), ocf=d.get("ocf", 0.0),
            net_profit=d.get("net_profit", 0.0),
            net_profit_ttm=d.get("net_profit_ttm", 0.0),
            gross_margin_series=d.get("gross_margin_series", [0, 0, 0]),
            pe=d.get("pe", 0.0), pb=d.get("pb", 0.0),
            pe_pct_3y=d.get("pe_pct_3y", 0.5), pb_pct_3y=d.get("pb_pct_3y", 0.5),
            pledge_ratio=d.get("pledge_ratio", 0.0),
            avg_turnover_30d=d.get("avg_turnover_30d", 0.0),
            is_st=d.get("is_st", False),
        )

    def get_index_constituents(self, index_code: str) -> List[str]:
        def _fetch():
            out = self._run("index", "constituent", index_code)
            codes = []
            for _, r in self._parse_all_rows(out):
                c = r.get("code") or r.get("代码")
                if c:
                    codes.append(c)
            return codes
        return self._get("idx", index_code, 7, _fetch)

    def get_universe(self) -> List[str]:
        # 全市场扫描起点由优先指数池提供，这里回退为空（candidate_universe 优先用指数成分）
        return []

    def market_breadth(self) -> List[dict]:
        """从已预热的 quote 缓存提取当日市场宽度快照（零网络开销）。

        只取当日刷新过且字段齐全的条目；停牌/无涨跌幅的跳过。
        选股预热（warm_universe）后候选池 quote 即就绪，情绪指标可直接统计。
        """
        out: List[dict] = []
        quote_b = self._cache.get("quote", {})
        for code, v in quote_b.items():
            d = v.get("data") if isinstance(v, dict) else None
            if not isinstance(d, dict):
                continue
            if d.get("suspended"):
                continue
            price = d.get("price") or 0.0
            prev = d.get("prev_close") or 0.0
            if price <= 0 or prev <= 0:
                continue
            out.append({
                "code": code,
                "price": price,
                "prev_close": prev,
                "high": d.get("high") or 0.0,
                "low": d.get("low") or 0.0,
                "ceiling": d.get("ceiling") or 0.0,
                "floor": d.get("floor") or 0.0,
                "change_pct": d.get("change_pct"),
            })
        return out

    def is_trading_day(self, d: date) -> bool:
        key = d.isoformat()
        if key in self._cache.get("cal", {}):
            return self._cache["cal"][key]
        try:
            out = self._run("trade-calendar", "--date", key)
            # 解析「是否交易日」列
            res = False
            for headers, r in self._parse_all_rows(out):
                if "是否交易日" in headers:
                    res = (r.get("是否交易日") or "").strip() == "是"
                    break
            self._cache.setdefault("cal", {})[key] = res
            return res
        except Exception:
            res = d.weekday() < 5  # 回退：工作日
            self._cache.setdefault("cal", {})[key] = res
            return res

    def next_trading_day(self, d: date) -> date:
        nd = d + timedelta(days=1)
        while not self.is_trading_day(nd):
            nd += timedelta(days=1)
        return nd

    @staticmethod
    def _find_row(text: str, code: str):
        for headers, r in WeStockProvider._parse_all_rows(text):
            if r.get("code") == code or r.get("代码") == code:
                return r
        rows = WeStockProvider._parse_all_rows(text)
        return rows[0][1] if rows else None
