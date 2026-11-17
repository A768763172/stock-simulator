"""盘前资讯采集与「开机晚不漏跑」状态机单元测试（纯本地、mock CLI、不联网）。"""
import json
import os
import sys
import tempfile
import unittest
from datetime import date, datetime, time as dtime, timedelta
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pre_market as pm
import pre_market_news as pn
import scheduler


def _ago(hours=1):
    return (datetime.now() - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")


NEWS_ROWS = [
    {"id": "1", "src": "财政部", "summary": "", "time": _ago(1),
     "title": "财政部：全链条依法重拳惩治财务造假", "typeStr": "", "url": "http://x/1"},
    {"id": "2", "src": "财联社", "summary": "", "time": _ago(2),
     "title": "A股三大指数集体低开", "typeStr": "", "url": "http://x/2"},
    {"id": "1", "src": "财政部", "summary": "", "time": _ago(1),
     "title": "财政部：全链条依法重拳惩治财务造假", "typeStr": "", "url": "http://x/1"},
]
NOTICE_ROWS = [
    {"type": "0", "Ftranslate": "0", "id": "n1", "newstype": "x",
     "symbol": "sh600000", "time": _ago(20), "title": "浦发银行：第三季度报告",
     "update_time": _ago(20), "url": ""},
]
HOT_ROWS = [
    {"news_id": "h1", "news_title": "证监会最新发声！维护市场平稳运行", "rank": "1",
     "publish_time": str(int((datetime.now() - timedelta(hours=1)).timestamp())),
     "source": "财联社", "news_type": "1", "cont_type": "0",
     "property": "1", "has_video": "0", "url": "http://x/h1"},
]


def _fake_run(args, title_field):
    if args[0] == "news":
        return NEWS_ROWS
    if args[0] == "notice":
        return NOTICE_ROWS
    if args[0] == "hot":
        return HOT_ROWS
    raise RuntimeError("unexpected command")


class TestParseMd(unittest.TestCase):
    def test_title_containing_pipe_is_not_split(self):
        text = (
            "| news_id | news_title | rank | publish_time | source | url |\n"
            "| --- | --- | --- | --- | --- | --- |\n"
            "| h1 | 一文看市|A股放量下跌 | 1 | 1794467740 | 腾讯自选股 | http://x/1 |\n"
        )
        rows = pn._parse_md(text, "news_title")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["news_title"], "一文看市|A股放量下跌")
        self.assertEqual(rows[0]["rank"], "1")

    def test_repeated_headers_across_notice_sections(self):
        header = ("| type | id | symbol | time | title | url |\n"
                  "| --- | --- | --- | --- | --- | --- |\n")
        body1 = "| 0 | n1 | sh600000 | 2026-11-11 16:00 | 公告A |  |\n"
        body2 = "| 0 | n2 | sz000001 | 2026-11-11 15:00 | 公告B |  |\n"
        text = ("**sh600000**\n共 10 条\n" + header + body1
                + "**sz000001**\n共 5 条\n" + header + body2)
        rows = pn._parse_md(text, "title")
        self.assertEqual(len(rows), 2)
        self.assertEqual([r["symbol"] for r in rows], ["sh600000", "sz000001"])
        self.assertEqual([r["title"] for r in rows], ["公告A", "公告B"])

    def test_empty_text_returns_empty(self):
        self.assertEqual(pn._parse_md("", "title"), [])
        self.assertEqual(pn._parse_md("no table here", "title"), [])


class TestTsAndRecent(unittest.TestCase):
    def test_unix_timestamp_and_string(self):
        self.assertEqual(len(pn._ts("1794467740")), 16)
        self.assertEqual(pn._ts("2026-11-12 15:05:46"), "2026-11-12 15:05")
        self.assertEqual(pn._ts(""), "")

    def test_recent_window(self):
        self.assertTrue(pn._recent({"time": _ago(2)}, "time"))
        self.assertFalse(pn._recent({"time": _ago(72)}, "time"))
        self.assertTrue(pn._recent({"time": "无法解析"}, "time"))


class TestCollect(unittest.TestCase):
    def test_classification_dedup_and_sources(self):
        with patch.object(pn, "_run", side_effect=_fake_run), \
             patch.object(pn, "_holding_codes", return_value=["sh600000"]):
            out = pn.collect()
        self.assertTrue(out["available"])
        self.assertEqual(len(out["policy"]), 1)       # 重复标题被去重
        self.assertIn("财政部", out["policy"][0]["title"])
        self.assertEqual(len(out["market_news"]), 1)
        self.assertEqual(out["notices"][0]["source"], "sh600000")
        self.assertEqual(out["hot"][0]["source"], "财联社")
        self.assertEqual(len(out["sources"]), 3)
        self.assertTrue(all(v["ok"] for v in out["sources"].values()))
        self.assertIn("数据源就绪", out["note"])

    def test_single_source_failure_does_not_break_others(self):
        def flaky(args, title_field):
            if args[0] == "notice":
                raise RuntimeError("公告源超时")
            return _fake_run(args, title_field)
        with patch.object(pn, "_run", side_effect=flaky), \
             patch.object(pn, "_holding_codes", return_value=["sh600000"]), \
             patch.object(pn, "RETRY_WAIT", 0):
            out = pn.collect()
        self.assertFalse(out["sources"]["notice"]["ok"])
        self.assertIn("公告源超时", out["sources"]["notice"]["error"])
        self.assertEqual(out["notices"], [])
        self.assertTrue(out["sources"]["news"]["ok"])   # 其他源照常
        self.assertTrue(out["available"])

    def test_no_holdings_skips_notice_without_error(self):
        with patch.object(pn, "_run", side_effect=_fake_run), \
             patch.object(pn, "_holding_codes", return_value=[]):
            out = pn.collect()
        self.assertFalse(out["sources"]["notice"]["ok"])
        self.assertIn("无持仓", out["sources"]["notice"]["error"])

    def test_transient_timeout_is_retried_and_recovers(self):
        """开机后网络未就绪导致首轮超时，重试一次后该源应恢复。"""
        calls = {"hot": 0}

        def flaky(args, title_field):
            if args[0] == "hot":
                calls["hot"] += 1
                if calls["hot"] == 1:
                    raise RuntimeError("CLI 超时 45s")
            return _fake_run(args, title_field)

        with patch.object(pn, "_run", side_effect=flaky), \
             patch.object(pn, "_holding_codes", return_value=["sh600000"]), \
             patch.object(pn, "RETRY_WAIT", 0):
            out = pn.collect()
        self.assertEqual(calls["hot"], 2)
        self.assertTrue(out["sources"]["hot"]["ok"])
        self.assertEqual(out["sources"]["hot"]["attempts"], 2)
        self.assertEqual(len(out["hot"]), 1)

    def test_timeout_constants_leave_room_for_cold_cli(self):
        self.assertGreaterEqual(pn.CLI_TIMEOUT, 40)
        self.assertGreaterEqual(pn.CLI_RETRIES, 1)
        self.assertGreaterEqual(scheduler.NEWS_TIMEOUT,
                                pn.CLI_TIMEOUT * (pn.CLI_RETRIES + 1) * 3)


class TestRunIdempotentAndDegrade(unittest.TestCase):
    def test_idempotent_and_all_sources_failed_still_writes_honest_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = dict(
                DATA_DIR=tmp,
                OUT_JSON=os.path.join(tmp, "pre_market_news.json"),
                ACCOUNT_JSON=os.path.join(tmp, "none_account.json"),
            )
            with patch.multiple(pn, **paths), \
                 patch.object(pn, "RETRY_WAIT", 0), \
                 patch.object(pn, "_run", side_effect=RuntimeError("offline")):
                self.assertTrue(pn.run().startswith("OK"))
                with open(paths["OUT_JSON"], encoding="utf-8") as f:
                    out = json.load(f)
                self.assertFalse(out["available"])
                self.assertFalse(any(v["ok"] for v in out["sources"].values()))
                self.assertTrue(pn.run().startswith("SKIP"))       # 同日幂等
                self.assertTrue(pn.run(force=True).startswith("OK"))


class TestNewsBlock(unittest.TestCase):
    def test_today_news_is_accepted(self):
        today = date.today().isoformat()
        payload = {"trading_day": today, "available": True,
                   "generated_at": "2026-11-12 08:45:00",
                   "sources": {"news": {"ok": True, "count": 2}},
                   "policy": [{"title": "政策A"}], "market_news": [],
                   "notices": [], "hot": [], "note": "ok"}
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "news.json")
            with open(p, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False)
            with patch.object(pm, "NEWS_JSON", p):
                nb = pm._news_block()
        self.assertTrue(nb["available"])
        self.assertEqual(len(nb["policy"]), 1)

    def test_stale_or_missing_news_never_shown_as_today(self):
        payload = {"trading_day": "2000-01-01", "available": True,
                   "policy": [{"title": "旧闻"}]}
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "news.json")
            with open(p, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False)
            with patch.object(pm, "NEWS_JSON", p):
                nb = pm._news_block()
            self.assertFalse(nb["available"])
            self.assertEqual(nb["policy"], [])
            self.assertIn("未展示为当日资讯", nb["note"])
            with patch.object(pm, "NEWS_JSON", os.path.join(tmp, "missing.json")):
                self.assertFalse(pm._news_block()["available"])


class TestPreMarketAction(unittest.TestCase):
    """用户 08:30–08:45 开机，甚至 09:30 后才开机，都不能漏做盘前检索。"""

    def test_constants(self):
        self.assertEqual(scheduler.PRE_MARKET_START, dtime(8, 45))
        self.assertLess(scheduler.PRE_MARKET_START, scheduler.INTRADAY_START)
        self.assertLess(scheduler.NEWS_CUTOFF, scheduler.INTRADAY_START)
        self.assertEqual(scheduler.NEWS_TIMEOUT, 300)

    def test_boot_window(self):
        day = date(2026, 11, 13)
        cases = [
            (dtime(8, 30), "sleep"),   # 08:30 开机：等到 08:45
            (dtime(8, 44), "sleep"),
            (dtime(8, 45), "run"),     # 08:45 开机：立刻做
            (dtime(9, 10), "run"),     # 晚开机：启动即补做
            (dtime(9, 31), "run"),     # 开盘后才开机：仍补做
            (dtime(10, 30), "run"),
            (dtime(15, 40), "run"),
        ]
        for t, expect in cases:
            with self.subTest(t=t):
                self.assertEqual(
                    scheduler.pre_market_action(datetime.combine(day, t), day, None),
                    expect)

    def test_same_day_not_repeated(self):
        day = date(2026, 11, 13)
        self.assertEqual(
            scheduler.pre_market_action(datetime.combine(day, dtime(9, 0)), day, day),
            "skip")
        # 新交易日必须重新执行
        nxt = date(2026, 11, 16)
        self.assertEqual(
            scheduler.pre_market_action(datetime.combine(nxt, dtime(9, 0)), nxt, day),
            "run")


if __name__ == "__main__":
    unittest.main()
