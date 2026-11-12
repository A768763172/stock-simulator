"""盘前信息检索 pre_market.py 单元测试（纯本地逻辑，tempfile 隔离，不联网）。"""
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pre_market as pm


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
                PRE_MARKET_JSON=os.path.join(tmp, "pre_market.json"),
            )
            with patch.multiple(pm, **paths):
                r1 = pm.run_pre_market()
                self.assertTrue(r1.startswith("OK"))
                out = json.load(open(paths["PRE_MARKET_JSON"], encoding="utf-8"))
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
                PRE_MARKET_JSON=os.path.join(tmp, "pre_market.json"),
            )
            with patch.multiple(pm, **paths):
                r = pm.run_pre_market()
                self.assertTrue(r.startswith("OK"))
                out = json.load(open(paths["PRE_MARKET_JSON"], encoding="utf-8"))
                self.assertEqual(out["holdings"], [])
                self.assertEqual(out["regime"], "UNKNOWN")
                self.assertTrue(out["summary"])


if __name__ == "__main__":
    unittest.main()
