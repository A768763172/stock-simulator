"""看板渲染回归测试（不依赖真实 streamlit：以桩模块导入 dashboard）。

覆盖两个历史缺陷：
- P0：持仓明细 open/high/low 单元格曾把三元表达式写进 f-string 格式说明符，
  渲染即抛 ValueError，并使自动刷新中断、页面冻结（阶段标签停在旧时间）。
- P1：概览曾优先采用历史收盘快照的 total_assets/market_value，盘中展示
  与持仓明细不同源的「两套数据」（如 mock 演示遗留快照导致总权益虚高）。
"""
import sys
import types
import unittest
from pathlib import Path

ROOT = str(Path(__file__).resolve().parent.parent)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def _import_dashboard():
    """以最小 streamlit 桩导入 dashboard（streamlit 未安装时亦可测）。"""
    if "streamlit" not in sys.modules:
        stub = types.ModuleType("streamlit")
        stub.set_page_config = lambda *a, **k: None
        stub.session_state = {}
        sys.modules["streamlit"] = stub
    import importlib
    return importlib.import_module("dashboard")


def _row(**over):
    base = {
        "code": "sh600519", "name": "贵州茅台", "industry": "白酒",
        "qty": 100, "available": 100, "cost_price": 1700.0,
        "price": 1248.71, "ready": True, "status": "READY",
        "market_value": 124871.0, "upnl": -45129.0, "pct": -0.2655,
        "weight": 0.4779, "open": 1251.24, "high": 1255.0, "low": 1240.0,
        "volume": 12345.0, "change_pct": -0.37, "ceiling": 1376.36,
        "floor": 1126.12, "limit_hit": "NONE", "is_suspended": False,
        "updated_at": "2026-09-24T09:36:30", "source": "westock",
        "warn": None,
    }
    base.update(over)
    return base


class TestPositionsRender(unittest.TestCase):
    def test_positions_html_rows_render_without_error(self):
        """P0 回归：含 open/high/low 的持仓行必须正常渲染（历史抛 ValueError）。"""
        dash = _import_dashboard()
        html = dash.positions_html([_row()])
        self.assertIn("贵州茅台", html)
        self.assertIn("1248.710", html)     # 现价
        self.assertIn("1251.240", html)     # 开盘
        self.assertIn("1255.000", html)     # 最高
        self.assertIn("1240.000", html)     # 最低

    def test_positions_html_not_ready_shows_dash_not_crash(self):
        """行情未就绪行：显式「行情未就绪」，四价位显示 '—'，不得抛异常。"""
        dash = _import_dashboard()
        row = _row(price=None, ready=False, status="NOT_READY",
                   market_value=None, upnl=None,
                   open=0.0, high=0.0, low=0.0, volume=0.0,
                   updated_at="", source="")
        html = dash.positions_html([row])
        self.assertIn("行情未就绪", html)


class TestTradeTimeRender(unittest.TestCase):
    """回归：成交流水「时间」列曾用字符串末 8 位截取，遇微秒截成 `8.772186`。"""
    def test_microseconds_datetime_shows_hms(self):
        dash = _import_dashboard()
        html = dash.trades_html([{
            "side": "BUY", "code": "sh601816", "name": "京沪高铁",
            "price": 5.1926, "quantity": 100, "amount": 519.26,
            "total_fee": 5.0052, "realized_pnl": 0.0,
            "datetime": "2026-10-19 09:30:08.772186",
        }])
        self.assertIn("09:30:08", html)
        self.assertNotIn("8.772186", html)

    def test_iso_and_minute_only_formats(self):
        dash = _import_dashboard()
        self.assertEqual(dash.fmt_trade_time("2026-10-19T09:30:08"), "09:30:08")
        self.assertEqual(dash.fmt_trade_time("2026-10-19 09:30"), "09:30:00")
        self.assertEqual(dash.fmt_trade_time(""), "—")

    def test_trade_date_column_renders_cn_date(self):
        """流水需带「日期」列（如 2026年10月19日），跨日查看时不必倒推。"""
        dash = _import_dashboard()
        html = dash.trades_html([{
            "side": "SELL", "code": "sh510880", "name": "红利ETF华泰柏瑞",
            "price": 3.473, "quantity": 200, "amount": 694.6,
            "total_fee": 5.0069, "realized_pnl": -1.35,
            "datetime": "2026-10-13 14:31:12.194550",
        }])
        self.assertIn("<th>日期</th>", html)
        self.assertIn("2026年10月13日", html)
        self.assertIn("14:31:12", html)

    def test_fmt_trade_date_formats(self):
        dash = _import_dashboard()
        self.assertEqual(dash.fmt_trade_date("2026-10-28 09:30:00"), "2026年10月28日")
        self.assertEqual(dash.fmt_trade_date("2026-10-28T09:30:00"), "2026年10月28日")
        self.assertEqual(dash.fmt_trade_date("2026-10-28"), "2026年10月28日")
        self.assertEqual(dash.fmt_trade_date(""), "—")


class TestPositionsDayPnl(unittest.TestCase):
    """持仓明细的当日浮动盈亏 / 当日盈亏比例（以昨收为基准）。"""

    def test_day_pnl_rendered(self):
        dash = _import_dashboard()
        row = _row(prev_close=1200.0,
                   day_upnl=round((1248.71 - 1200.0) * 100, 2),
                   day_pct=1248.71 / 1200.0 - 1.0)
        html = dash.positions_html([row])
        self.assertIn("当日浮动盈亏", html)
        self.assertIn("当日盈亏比例", html)
        self.assertIn("4,871.00", html)
        self.assertIn("+4.06%", html)

    def test_day_pnl_missing_shows_dash(self):
        dash = _import_dashboard()
        html = dash.positions_html([_row(day_upnl=None, day_pct=None)])
        self.assertIn("当日浮动盈亏", html)


class TestPositionsTotalRow(unittest.TestCase):
    """持仓明细表尾需有「合计」行：总市值 = 各持仓市值之和，当日/浮动盈亏直接相加，
    当日盈亏比例按「合计当日盈亏 ÷ 合计昨收市值」加权（百分比直加无经济含义）。"""
    def _rows(self):
        return [
            _row(qty=100, market_value=1000.0, day_upnl=100.0,
                 day_pct=100.0 / (9.0 * 100), prev_close=9.0, upnl=50.0),
            _row(qty=200, market_value=2000.0, day_upnl=200.0,
                 day_pct=200.0 / (8.0 * 200), prev_close=8.0, upnl=80.0),
        ]

    def test_total_row_renders(self):
        dash = _import_dashboard()
        html = dash.positions_html(self._rows())
        self.assertIn("合计", html)
        self.assertIn("合计（2 只）", html)
        # 总市值 = 1000 + 2000 = 3000.00
        self.assertIn("3,000.00", html)
        # 当日浮动盈亏合计 = 100 + 200 = 300.00
        self.assertIn("300.00", html)
        # 当日盈亏比例 = 300 / (9*100 + 8*200=2500) = +12.00%
        self.assertIn("+12.00%", html)
        # 浮动盈亏合计 = 50 + 80 = 130.00
        self.assertIn("130.00", html)

    def test_empty_no_total(self):
        dash = _import_dashboard()
        html = dash.positions_html([])
        self.assertNotIn("合计", html)


class TestPositionsHoldingDays(unittest.TestCase):
    """持仓明细需含「持仓天数」列（建仓日→今的自然日差 +1，无建仓记录显式 '—'）。"""

    def test_holding_days_rendered(self):
        dash = _import_dashboard()
        row = _row(holding_days=12)
        html = dash.positions_html([row])
        self.assertIn("<th>持仓天数</th>", html)
        self.assertIn("12 天", html)

    def test_holding_days_missing_shows_dash(self):
        dash = _import_dashboard()
        html = dash.positions_html([_row(holding_days=None)])
        self.assertIn("—", html)

    def test_calc_holding_days(self):
        dash = _import_dashboard()
        # 建仓日即基准日 → 1 天；跨 4 个自然日差 +1 = 5 天
        self.assertEqual(dash.calc_holding_days("2026-10-13", "2026-10-13"), 1)
        self.assertEqual(dash.calc_holding_days("2026-10-09", "2026-10-13"), 5)
        self.assertIsNone(dash.calc_holding_days(None, "2026-10-13"))
        self.assertIsNone(dash.calc_holding_days("2026-10-13", None))

    def test_first_buy_by_code(self):
        dash = _import_dashboard()
        trades = [
            {"side": "BUY", "code": "sh600519",
             "datetime": "2026-10-09 09:30:00.241564"},
            {"side": "BUY", "code": "sh600519",
             "datetime": "2026-10-12 09:30:00.241564"},
            {"side": "SELL", "code": "sh600519",
             "datetime": "2026-10-13 14:31:12.194550"},
        ]
        self.assertEqual(dash.first_buy_by_code(trades)["sh600519"], "2026-10-09")
        # 历史回看时建仓日不超过所选交易日
        self.assertEqual(
            dash.first_buy_by_code(trades, ref_day="2026-10-11")["sh600519"],
            "2026-10-09")


class TestClosedPositionsHoldingDays(unittest.TestCase):
    """历史持仓表需含「持仓天数」列，并正确计入合计行的跨列数（colspan=8）。

    走真实管线：trades → compute_closed_positions（派生建仓日/清仓日/持仓天数）
    → closed_positions_html。
    """

    def _trades(self):
        return [
            {"side": "BUY", "code": "sz000623", "name": "吉林敖东",
             "datetime": "2026-10-12 09:30:06.000000", "quantity": 100,
             "amount": 1753.88, "realized_pnl": 0.0, "price": 17.5388},
            {"side": "SELL", "code": "sz000623", "name": "吉林敖东",
             "datetime": "2026-10-19 09:30:08.000000", "quantity": 100,
             "amount": 1701.2, "realized_pnl": -59.45, "price": 17.012},
            {"side": "BUY", "code": "sh510880", "name": "红利ETF华泰柏瑞",
             "datetime": "2026-10-09 09:30:00.000000", "quantity": 200,
             "amount": 685.94, "realized_pnl": 0.0, "price": 3.4297},
            {"side": "SELL", "code": "sh510880", "name": "红利ETF华泰柏瑞",
             "datetime": "2026-10-13 14:31:12.000000", "quantity": 200,
             "amount": 694.6, "realized_pnl": -1.35, "price": 3.473},
        ]

    def test_holding_days_header_and_cells(self):
        dash = _import_dashboard()
        rows = dash.compute_closed_positions(self._trades(), open_codes=set())
        html = dash.closed_positions_html(rows)
        self.assertIn("<th>持仓天数</th>", html)
        # 吉林敖东 10-12→10-19 = 7 天 +1 = 8 天；红利ETF 10-09→10-13 = 4 天 +1 = 5 天
        self.assertIn("8 天", html)
        self.assertIn("5 天", html)

    def test_total_row_colspan_updated(self):
        dash = _import_dashboard()
        rows = dash.compute_closed_positions(self._trades(), open_codes=set())
        html = dash.closed_positions_html(rows)
        self.assertIn("colspan=8", html)
        self.assertIn("合计", html)


class TestClosedPositionsTotal(unittest.TestCase):
    """历史持仓表尾需有已实现盈亏 / 盈亏比例的合计行（比例按成本加权）。"""

    def _rows(self):
        return [
            {"code": "sz000623", "name": "吉林敖东", "first_buy": "2026-10-12 09:30:06",
             "last_sell": "2026-10-19 09:30:08", "buy_qty": 100, "sell_qty": 100,
             "last_sell_price": 17.012, "realized": -59.45,
             "sold_cost": 1763.90, "pnl_pct": -0.0337, "bad_price": False},
            {"code": "sh510880", "name": "红利ETF华泰柏瑞", "first_buy": "2026-10-09 09:30:00",
             "last_sell": "2026-10-13 14:31:12", "buy_qty": 200, "sell_qty": 200,
             "last_sell_price": 3.473, "realized": -1.35,
             "sold_cost": 690.95, "pnl_pct": -0.0020, "bad_price": False},
        ]

    def test_total_row_rendered(self):
        dash = _import_dashboard()
        html = dash.closed_positions_html(self._rows())
        self.assertIn("合计", html)
        self.assertIn("-60.80", html)          # -59.45 + -1.35
        # 加权比例：-60.80 / 2454.85 = -2.48%
        self.assertIn("-2.48%", html)

    def test_empty_rows_no_total(self):
        dash = _import_dashboard()
        html = dash.closed_positions_html([])
        self.assertNotIn("合计", html)


class TestOverviewLiveConsistency(unittest.TestCase):
    def test_live_overview_ignores_stale_snapshot(self):
        """实时口径：总权益 = 可用 + 冻结 + 实时市值，不采用含 mock 价的旧快照。"""
        dash = _import_dashboard()
        account = {"cash": 50000.0, "frozen_cash": 0.0,
                   "initial_capital": 50000.0, "realized_pnl": 0.0,
                   "positions": {"sh601318": {"quantity": 500,
                                              "cost_price": 50.0}}}
        live = {"sh601318": {"price": 53.8}}
        snaps = [{"day": "2026-10-02", "total_assets": 299500.0,
                  "cash": 50000.0, "market_value": 249500.0,
                  "total_return_pct": 7.0}]
        ov = dash.compute_overview(account, live, snaps, {}, {}, live_mode=True)
        self.assertAlmostEqual(ov["total_assets"], 50000.0 + 500 * 53.8, places=2)
        self.assertAlmostEqual(ov["market_value"], 500 * 53.8, places=2)
        self.assertTrue(ov["reconcile_ok"])
        self.assertNotAlmostEqual(ov["total_assets"], 299500.0, places=2)

    def test_live_overview_without_positions_equals_cash(self):
        """空仓时总权益即现金，且当前交易日取自账户而非遗留快照。"""
        dash = _import_dashboard()
        account = {"cash": 50000.0, "frozen_cash": 0.0,
                   "initial_capital": 50000.0, "realized_pnl": 0.0,
                   "positions": {}, "current_day": "2026-09-24"}
        snaps = [{"day": "2026-10-02", "total_assets": 299500.0,
                  "total_return_pct": 7.0}]
        ov = dash.compute_overview(account, {}, snaps, {}, {}, live_mode=True)
        self.assertAlmostEqual(ov["total_assets"], 50000.0, places=2)
        self.assertEqual(ov["market_value"], 0.0)
        self.assertEqual(ov["current_day"], "2026-09-24")

    def test_historical_mode_still_uses_snapshot(self):
        """历史回看口径保持快照数值（不受实时口径改动影响）。"""
        dash = _import_dashboard()
        snaps = [{"day": "2026-10-02", "total_assets": 299500.0,
                  "cash": 50000.0, "market_value": 249500.0,
                  "total_return_pct": 7.0}]
        ov = dash.compute_overview({"cash": 1.0, "positions": {}}, {},
                                   snaps, {}, {}, live_mode=False)
        self.assertAlmostEqual(ov["total_assets"], 299500.0, places=2)


class TestAggregateDayPnl(unittest.TestCase):
    """账户概览「当日盈亏」必须与持仓明细「当日浮动盈亏」之和同口径（均按昨收）。"""
    def _row(self, prev_close, price, qty, day_upnl=None):
        r = _row(prev_close=prev_close, price=price, qty=qty)
        if day_upnl is not None:
            r["day_upnl"] = day_upnl
        else:
            r["day_upnl"] = round((price - prev_close) * qty, 2)
        return r

    def test_matches_position_day_upnl_sum(self):
        dash = _import_dashboard()
        rows = [
            self._row(6.92, 7.61, 100),    # +69.00
            self._row(5.20, 5.22, 200),    # +4.00
            self._row(14.57, 14.29, 1000), # -280.00
        ]
        pnl, pct = dash.aggregate_day_pnl(rows)
        self.assertEqual(pnl, round(69.0 + 4.0 - 280.0, 2))
        # 昨日持仓市值 = 6.92*100 + 5.20*200 + 14.57*1000 = 16302
        self.assertAlmostEqual(pct, (-207.0) / 16302.0, places=6)

    def test_excludes_unready_or_missing_prev_close(self):
        dash = _import_dashboard()
        rows = [
            self._row(6.92, 7.61, 100),                          # +69.00 计入
            {"day_upnl": None, "prev_close": None, "qty": 100},   # 行情未就绪，不计
            {"day_upnl": None, "prev_close": 0.0, "qty": 100},    # 昨收=0 → 不计
        ]
        pnl, pct = dash.aggregate_day_pnl(rows)
        self.assertEqual(pnl, 69.0)
        self.assertAlmostEqual(pct, 69.0 / (6.92 * 100), places=6)

    def test_empty_positions(self):
        dash = _import_dashboard()
        pnl, pct = dash.aggregate_day_pnl([])
        self.assertEqual(pnl, 0.0)
        self.assertEqual(pct, 0.0)


class TestAgentStatusMapping(unittest.TestCase):
    """智能体状态标签必须与 RiskMode 枚举字面量一致，否则活着的智能体会
    被误显示成「无智能体」（历史缺陷：看板写 WEEKLY_DEFENSE/PEAK_DEFENSE，
    而枚举实际为 DEFENSE_WEEKLY/DEFENSE_PEAK）。"""

    def _status(self, mode):
        dash = _import_dashboard()
        strat = {"risk": {"mode": mode}} if mode is not None else {}
        ov = dash.compute_overview({"cash": 50000.0, "positions": {}},
                                   {}, [], {}, strat, live_mode=True)
        return ov["status"]

    def test_normal(self):
        self.assertEqual(self._status("NORMAL"), "运行中（正常）")

    def test_defense_daily(self):
        self.assertEqual(self._status("DEFENSE_DAILY"), "防守（日回撤）")

    def test_defense_weekly(self):
        self.assertEqual(self._status("DEFENSE_WEEKLY"), "防守（周回撤）")

    def test_defense_peak(self):
        """智能体在线且全面防守时，绝不可误显示「无智能体」。"""
        self.assertEqual(self._status("DEFENSE_PEAK"), "全面防守")

    def test_unknown_mode_not_false_no_agent(self):
        """未知模式不得误标为「无智能体」。"""
        self.assertNotIn("无智能体", self._status("FOO_MODE"))


if __name__ == "__main__":
    unittest.main()
