"""盘前信息检索 pre_market.py 单元测试（纯本地逻辑，tempfile 隔离，不联网）。"""
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pre_market as pm


def _q(day, price, prev, name="X", **kw):
    """构造一条 westock 全市场缓存 quote 记录。"""
    d = {"code": "code", "name": name, "price": price, "prev_close": prev,
         "change_pct": 0.0, "time": day}
    d.update(kw)
    return {"date": day, "data": d}


def _row(code, chg, industry="", as_of="2026-11-12"):
    return {"code": code, "name": code, "change_pct": chg,
            "industry": industry, "as_of": as_of}


class TestRegimeFromLearned(unittest.TestCase):
    def test_valid_regime(self):
        for r in ("BULL", "OSCILLATION", "BEAR"):
            self.assertEqual(pm._regime_from_learned({"learner": {"regime": r}}), r)

    def test_missing_or_invalid_returns_unknown(self):
        self.assertEqual(pm._regime_from_learned({}), "UNKNOWN")
        self.assertEqual(pm._regime_from_learned({"learner": {"regime": "WILD"}}), "UNKNOWN")
        self.assertEqual(pm._regime_from_learned({"learner": None}), "UNKNOWN")


class TestHoldingsPre(unittest.TestCase):
    def test_build_from_live_cache(self):
        account = {"positions": {
            "sh600000": {"name": "浦发银行", "cost_price": 8.0},
            "sz000001": {"name": "平安银行", "cost_price": 11.0},
        }}
        live = {
            "sh600000": {"name": "浦发银行", "prev_close": 8.0, "open": 8.08,
                         "change_pct": 1.0},
            "sz000001": {"name": "平安银行", "prev_close": 11.0, "open": 10.9,
                         "change_pct": -0.91},
        }
        rows = pm._holdings_pre(account, live)
        self.assertEqual(len(rows), 2)
        by = {r["code"]: r for r in rows}
        self.assertEqual(by["sh600000"]["change_pct"], 1.0)
        self.assertEqual(by["sz000001"]["change_pct"], -0.91)
        self.assertEqual(by["sh600000"]["cost"], 8.0)

    def test_change_pct_fallback_when_missing(self):
        account = {"positions": {"sh600000": {"name": "X", "cost_price": 1.0}}}
        live = {"sh600000": {"prev_close": 10.0, "open": 10.5}}
        r = pm._holdings_pre(account, live)[0]
        self.assertAlmostEqual(r["change_pct"], 5.0)

    def test_missing_quote_is_skipped(self):
        account = {"positions": {"sh600000": {"name": "X"}}}
        self.assertEqual(pm._holdings_pre(account, {}), [])


class TestWatchlist(unittest.TestCase):
    def test_scope_and_threshold(self):
        live = {
            "sh600000": {"name": "持仓大涨", "change_pct": 1.2},   # 持仓
            "sz000001": {"name": "持仓小跌", "change_pct": -0.1},  # 太小，过滤
            "sh600111": {"name": "全市场异动", "change_pct": -2.0},  # 非持仓
            "_meta": {"source": "westock"},
        }
        rows = pm._watchlist(live, {"sh600000"})
        codes = [r["code"] for r in rows]
        self.assertIn("sh600111", codes)
        self.assertIn("sh600000", codes)
        self.assertNotIn("sz000001", codes)
        self.assertNotIn("_meta", codes)
        # 非持仓优先排前面
        self.assertEqual(codes[0], "sh600111")
        by = {r["code"]: r for r in rows}
        self.assertEqual(by["sh600111"]["scope"], "全市场")
        self.assertEqual(by["sh600000"]["scope"], "持仓")

    def test_empty_cache_returns_empty(self):
        self.assertEqual(pm._watchlist({}, set()), [])


class TestLearnerState(unittest.TestCase):
    def test_reads_real_weights_from_learned_params(self):
        learned = {"learner": {
            "strategy_eff": {"trend_follow": 1.5},
            "factor_weights": {"momentum": 0.4},
        }}
        st = pm._learner_state(learned)
        self.assertEqual(st["strategy_eff"]["trend_follow"], 1.5)
        self.assertEqual(st["factor_weights"]["momentum"], 0.4)
        # 缺项由配置基线补齐
        self.assertIn("strategy_weights", st)

    def test_empty_learned_still_has_config_baseline(self):
        st = pm._learner_state({})
        self.assertIn("factor_weights", st)


class TestRunPreMarket(unittest.TestCase):
    def test_idempotent_and_writes_complete_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            account = {"positions": {
                "sh600000": {"name": "浦发银行", "cost_price": 8.0}}}
            live = {"_meta": {"source": "westock", "generated_at": "2026-11-12T09:00:00"},
                    "sh600000": {"name": "浦发银行", "prev_close": 8.0,
                                 "open": 8.08, "change_pct": 1.0}}
            learned = {"learner": {"regime": "BULL",
                                   "factor_weights": {"momentum": 0.5}}}
            def w(name, obj):
                with open(os.path.join(tmp, name), "w", encoding="utf-8") as f:
                    json.dump(obj, f, ensure_ascii=False)
            w("account.json", account)
            w("live_prices.json", live)
            w("learned_params.json", learned)

            paths = dict(
                DATA_DIR=tmp,
                ACCOUNT_JSON=os.path.join(tmp, "account.json"),
                LIVE_PRICES_JSON=os.path.join(tmp, "live_prices.json"),
                LEARNED_PARAMS_JSON=os.path.join(tmp, "learned_params.json"),
                WESTOCK_CACHE_JSON=os.path.join(tmp, "none_cache.json"),
                PRE_MARKET_JSON=os.path.join(tmp, "pre_market.json"),
            )
            with patch.multiple(pm, **paths):
                r1 = pm.run_pre_market()
                self.assertTrue(r1.startswith("OK"))
                with open(paths["PRE_MARKET_JSON"], encoding="utf-8") as f:
                    out = json.load(f)
                # 关键字段齐全且真实
                self.assertEqual(out["regime"], "BULL")
                self.assertEqual(len(out["holdings"]), 1)
                self.assertEqual(out["source"], "westock (调度守护实盘快照·本地缓存)")
                self.assertTrue(out["summary"])
                self.assertIn("factor_weights", out["learner_state"])
                # 2) 幂等：同日再次触发跳过
                r2 = pm.run_pre_market()
                self.assertTrue(r2.startswith("SKIP"))
                # 3) --force 可强制重跑
                r3 = pm.run_pre_market(force=True)
                self.assertTrue(r3.startswith("OK"))

    def test_never_crashes_with_empty_files(self):
        """所有数据文件缺失时也必须落盘一份可用摘要（异常自愈）。"""
        with tempfile.TemporaryDirectory() as tmp:
            paths = dict(
                DATA_DIR=tmp,
                ACCOUNT_JSON=os.path.join(tmp, "none1.json"),
                LIVE_PRICES_JSON=os.path.join(tmp, "none2.json"),
                LEARNED_PARAMS_JSON=os.path.join(tmp, "none3.json"),
                WESTOCK_CACHE_JSON=os.path.join(tmp, "none4.json"),
                PRE_MARKET_JSON=os.path.join(tmp, "pre_market.json"),
            )
            with patch.multiple(pm, **paths):
                r = pm.run_pre_market()
                self.assertTrue(r.startswith("OK"))
                with open(paths["PRE_MARKET_JSON"], encoding="utf-8") as f:
                    out = json.load(f)
                self.assertEqual(out["holdings"], [])
                self.assertEqual(out["regime"], "UNKNOWN")
                self.assertTrue(out["summary"])


class TestUniverseRows(unittest.TestCase):
    """全市场快照解析：统一基准日、自算涨跌幅、剔除无效行情、绝不虚构。"""

    def test_latest_date_only_and_self_computed_change(self):
        cache = {
            "quote": {
                "sh600000": _q("2026-11-12", 11.0, 10.0, "浦发银行"),
                "sh600001": _q("2026-11-12", 9.0, 10.0, "下跌股"),
                "sh600002": _q("2026-11-10", 10.5, 10.0, "旧快照"),
                "sh600003": _q("2026-11-12", 0.0, 10.0, "无效价"),
                "sh600004": _q("2026-11-12", 100.0, 10.0, "复权跳变"),
            },
            "fund": {"sh600000": {"date": "2026-10-21",
                                  "data": {"industry": "银行"}}},
        }
        rows, stats = pm._universe_rows(cache)
        self.assertEqual(stats["sample"], 2)
        self.assertEqual(stats["snapshot_date"], "2026-11-12")
        self.assertEqual(stats["pool_total"], 5)
        self.assertEqual(stats["valid_total"], 3)
        self.assertEqual(stats["stale_excluded"], 1)
        self.assertEqual(stats["invalid_excluded"], 2)
        self.assertEqual(stats["by_date"]["2026-11-12"], 2)
        by = {r["code"]: r for r in rows}
        # quote 的 change_pct 常是 0 占位，必须由 price/prev_close 自行计算
        self.assertEqual(by["sh600000"]["change_pct"], 10.0)
        self.assertEqual(by["sh600001"]["change_pct"], -10.0)
        self.assertEqual(by["sh600000"]["industry"], "银行")
        self.assertEqual(by["sh600000"]["as_of"], "2026-11-12")

    def test_empty_or_broken_cache_is_safe(self):
        for cache in ({}, {"quote": None}, {"quote": {"a": None}},
                      {"quote": {"a": {"date": "d", "data": "bad"}}}):
            rows, stats = pm._universe_rows(cache)
            self.assertEqual(rows, [])
            self.assertEqual(stats["sample"], 0)

    def test_name_fallback_to_code(self):
        cache = {"quote": {"sh600000": _q("2026-11-12", 10.0, 10.0, "")}}
        rows, _ = pm._universe_rows(cache)
        self.assertEqual(rows[0]["name"], "sh600000")


class TestMarketBreadth(unittest.TestCase):
    def test_counts_limits_and_extremes(self):
        rows = [_row("a", 10.0, "银行"), _row("b", 3.5, "电子"),
                _row("c", 0.0, "银行"), _row("d", -4.0, "传媒"),
                _row("e", -10.0, "电子")]
        b = pm._market_breadth(rows)
        self.assertEqual(b["sample"], 5)
        self.assertEqual((b["up"], b["down"], b["flat"]), (2, 2, 1))
        self.assertEqual((b["limit_up"], b["limit_down"]), (1, 1))
        self.assertEqual((b["big_up"], b["big_down"]), (2, 2))
        self.assertEqual(b["avg_change"], -0.1)
        self.assertEqual(b["median_change"], 0.0)
        self.assertEqual(b["leaders"][0]["code"], "a")
        self.assertEqual(b["laggards"][0]["code"], "e")

    def test_empty_rows(self):
        b = pm._market_breadth([])
        self.assertEqual(b["sample"], 0)
        self.assertIsNone(b["avg_change"])
        self.assertEqual(b["leaders"], [])


class TestSectorMoves(unittest.TestCase):
    def test_min_sample_and_ordering(self):
        rows = [_row("a", 2.0, "银行"), _row("b", 2.0, "银行"),
                _row("c", 2.0, "银行"),
                _row("d", -1.0, "电子"), _row("e", -2.0, "电子"),
                _row("f", -3.0, "电子"),
                _row("g", -5.0, "传媒"), _row("h", -5.0, "传媒")]
        s = pm._sector_moves(rows)
        self.assertEqual(s["tracked"], 2)       # 传媒仅 2 只，样本不足被排除
        self.assertEqual(s["leaders"][0]["industry"], "银行")
        self.assertEqual(s["laggards"][0]["industry"], "电子")
        self.assertEqual(s["leaders"][0]["avg_change"], 2.0)
        self.assertEqual(s["laggards"][0]["avg_change"], -2.0)


class TestUniverseWatchlist(unittest.TestCase):
    def test_threshold_both_sides_and_non_holding_first(self):
        rows = [_row("hold", 9.0, "银行"), _row("g1", 4.0, "通信"),
                _row("l1", -5.0, "有色"), _row("small", 0.5)]
        out = pm._universe_watchlist(rows, {}, {"hold"})
        codes = [r["code"] for r in out]
        self.assertNotIn("small", codes)         # |涨跌| < 2% 过滤
        self.assertEqual(set(codes), {"hold", "g1", "l1"})
        self.assertNotEqual(codes[0], "hold")    # 非持仓优先
        self.assertEqual(out[0]["scope"], "全市场")
        self.assertTrue(any(r["scope"] == "持仓" for r in out))
        self.assertTrue(all("as_of" in r and "industry" in r for r in out))

    def test_falls_back_to_holdings_when_no_universe_move(self):
        live = {"sh600000": {"name": "持仓异动", "change_pct": 1.5}}
        out = pm._universe_watchlist([], live, {"sh600000"})
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["scope"], "持仓")
        self.assertEqual(out[0]["as_of"], "")


class TestRunPreMarketUniverse(unittest.TestCase):
    def _paths(self, tmp):
        def w(name, obj):
            with open(os.path.join(tmp, name), "w", encoding="utf-8") as f:
                json.dump(obj, f, ensure_ascii=False)
        w("account.json", {"positions": {
            "hold": {"name": "持仓股", "cost_price": 10.0}}})
        w("live_prices.json", {
            "_meta": {"source": "westock", "generated_at": "2026-11-12T14:50:00"},
            "hold": {"name": "持仓股", "prev_close": 10.0,
                     "open": 10.1, "change_pct": 1.0}})
        w("learned_params.json", {"learner": {"regime": "BULL"}})
        w("cache.json", {"quote": {
            "sh600000": _q("2026-11-12", 11.0, 10.0, "领涨股"),
            "sh600001": _q("2026-11-12", 9.0, 10.0, "领跌股"),
        }, "fund": {"sh600000": {"date": "2026-10-21",
                                  "data": {"industry": "银行"}}}})
        return dict(
            DATA_DIR=tmp,
            ACCOUNT_JSON=os.path.join(tmp, "account.json"),
            LIVE_PRICES_JSON=os.path.join(tmp, "live_prices.json"),
            LEARNED_PARAMS_JSON=os.path.join(tmp, "learned_params.json"),
            WESTOCK_CACHE_JSON=os.path.join(tmp, "cache.json"),
            PRE_MARKET_JSON=os.path.join(tmp, "pre_market.json"),
        )

    def test_full_market_fields_written(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._paths(tmp)
            with patch.multiple(pm, **paths):
                self.assertTrue(pm.run_pre_market().startswith("OK"))
                with open(paths["PRE_MARKET_JSON"], encoding="utf-8") as f:
                    out = json.load(f)
            self.assertEqual(out["universe"]["sample"], 2)
            self.assertEqual(out["market_breadth"]["up"], 1)
            self.assertEqual(out["market_breadth"]["down"], 1)
            self.assertIn("全市场", out["universe"]["scope"])
            self.assertTrue(out["universe_basis"])
            self.assertTrue(any(w["scope"] == "全市场" for w in out["watchlist"]))
            self.assertIn("全市场涨跌", out["summary"])
            self.assertEqual(out["regime"], "BULL")

    def test_missing_cache_degrades_without_fabrication(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._paths(tmp)
            paths["WESTOCK_CACHE_JSON"] = os.path.join(tmp, "missing.json")
            with patch.multiple(pm, **paths):
                self.assertTrue(pm.run_pre_market().startswith("OK"))
                with open(paths["PRE_MARKET_JSON"], encoding="utf-8") as f:
                    out = json.load(f)
            self.assertEqual(out["universe"]["sample"], 0)
            self.assertEqual(out["market_breadth"]["sample"], 0)
            self.assertIn("全市场快照暂不可用", out["summary"])
            self.assertTrue(out["watchlist"])   # 持仓异动兜底仍有内容


class TestSchedulerTiming(unittest.TestCase):
    def test_pre_market_starts_before_open_and_after_midnight(self):
        from datetime import time as dtime
        import scheduler
        self.assertEqual(scheduler.PRE_MARKET_START, dtime(8, 30))
        self.assertLess(scheduler.PRE_MARKET_START, scheduler.INTRADAY_START)
        self.assertEqual(scheduler.INTRADAY_START, dtime(9, 30))
        self.assertEqual(scheduler.CLOSE_RUN, dtime(15, 35))


if __name__ == "__main__":
    unittest.main()
