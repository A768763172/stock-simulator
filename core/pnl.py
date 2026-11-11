"""盈亏统计模块。

计算已实现盈亏、浮动盈亏、总资产、总收益率，并生成逐标的盈亏明细。
所有金额单位：元（人民币）。不涉及任何外部上报。
"""
import json
from typing import Dict, List
from pathlib import Path

from core.account import AccountManager
from core.position import PositionCalculator
from market.base import MarketProvider


# data/live_prices.json 是引擎 save / 调度守护刷新写入的实时价快照，
# 作为 get_quote 瞬时失败（开盘前后行情接口抖动）时的兜底数据源。
_LIVE_PRICES_PATH = (
    Path(__file__).resolve().parent.parent / "data" / "live_prices.json"
)


class PnLStatistics:
    def __init__(self, account: AccountManager, market: MarketProvider, storage=None):
        self.account = account
        self.market = market
        self.storage = storage

    def _load_live_fallback(self) -> Dict[str, dict]:
        """读取实时价快照，作为 get_quote 失败时的回退（避免现价写成 0）。

        优先用与引擎一致的 storage（Simulator 传入），保证 fallback 与刷新落库
        同源；storage 缺失时回退模块级硬编码路径（真实运行两处指向同一 data 目录）。
        """
        if self.storage is not None:
            try:
                return self.storage.load_live_prices()
            except Exception:
                pass
        try:
            if _LIVE_PRICES_PATH.exists():
                return json.loads(_LIVE_PRICES_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
        return {}

    def _current_prices(self) -> Dict[str, float]:
        prices, _ = self._current_prices_with_flags()
        return prices

    def _current_prices_with_flags(self) -> tuple:
        """返回 (prices, unavailable_set)。

        异常容错联动：当实时行情(get_quote)与快照回退(live_prices)均不可得时，
        不再静默写成 0 价（会在看板概览与持仓明细间制造「总额悄悄少算」与
        「持仓标红行情未就绪」的数据割裂），而是将标的记入 unavailable 集合，
        由 snapshot / position_details 显式携带 price_ready 信号，供看板联动告警。
        """
        prices: Dict[str, float] = {}
        unavailable: set = set()
        fallback = self._load_live_fallback()
        from market.feed import validate_quote
        for code in self.account.positions:
            try:
                q = self.market.get_quote(code)
                p = float(q.price)
                prev = float(getattr(q, "prev_close", 0) or 0)
                ceiling = float(getattr(q, "ceiling", 0) or 0)
                floor = float(getattr(q, "floor", 0) or 0)
                ok, _ = validate_quote(p, prev, ceiling, floor)
                if ok:
                    prices[code] = p
                    continue
            except Exception:
                pass
            # 回退：实时价快照（调度守护盘中持续刷新，收盘后也保留最新价）
            rec = fallback.get(code)
            if rec and rec.get("price"):
                try:
                    prices[code] = float(rec["price"])
                    continue
                except Exception:
                    pass
            # 源与回退均不可得：标记不可用，交由上层显式告警，绝不静默写成 0
            unavailable.add(code)
        return prices, unavailable

    def snapshot(self) -> dict:
        prices, unavailable = self._current_prices_with_flags()
        mv = PositionCalculator.total_market_value(self.account.positions, prices)
        unrealized = sum(
            p.unrealized_pnl(prices.get(c, 0.0))
            for c, p in self.account.positions.items()
        )
        realized = self.account.realized_pnl
        frozen_cash = self.account.frozen_cash
        # 勾稽关系：总资金 = 可用资金 + 冻结资金 + 持仓市值
        total_assets = self.account.cash + frozen_cash + mv
        # 总盈亏（净）= 总权益 - 期初：现金在成交时已含全部手续费（买/卖费）
        # 直接扣除，故此口径天然包含手续费，使「总权益 = 期初 + 总盈亏」与
        # 「累计收益率」三者勾稽一致。
        # （旧口径 total_pnl = realized + unrealized 漏算买入费——cost_price 只记成交价
        #  不含买费，导致总盈亏虚高、与总权益对不上，详见 reconcile() 恒等式 B。）
        total_pnl = total_assets - self.account.initial_capital
        total_return = (total_pnl / self.account.initial_capital
                        if self.account.initial_capital else 0.0)
        return {
            "cash": round(self.account.cash, 2),
            "frozen_cash": round(frozen_cash, 2),
            "market_value": round(mv, 2),
            "total_assets": round(total_assets, 2),
            "realized_pnl": round(realized, 2),
            "unrealized_pnl": round(unrealized, 2),
            "total_pnl": round(total_pnl, 2),
            "total_return_pct": round(total_return * 100, 4),
            "initial_capital": self.account.initial_capital,
            "position_count": len(self.account.positions),
            "trade_count": len(self.account.trades),
            # 异常容错联动：价格就绪信号，供看板与勾稽联动告警
            "price_ready": len(unavailable) == 0,
            "price_unavailable": sorted(unavailable),
        }

    def reconcile(self) -> dict:
        """资金勾稽校验，返回两类恒等式的核对结果。

        恒等式 A（核心）：总资金 = 可用资金 + 冻结资金 + 持仓市值
        恒等式 B（派生）：总资金 = 期初余额 + 已实现盈亏(毛) + 浮动盈亏 - 累计买入手续费
          其中「已实现盈亏(毛)」= 已实现盈亏 + 卖出手续费（因 realized 已扣卖出费）。
        两式均应在 0.01 元容差内成立，否则说明存在资金泄漏 / 数量级错误。
        price_ready=False 时说明部分标的现价不可得，勾稽基于已知价估算，
        看板须同步联动告警，避免「悄悄少算」。
        """
        prices, unavailable = self._current_prices_with_flags()
        mv = PositionCalculator.total_market_value(self.account.positions, prices)
        unrealized = sum(
            p.unrealized_pnl(prices.get(c, 0.0))
            for c, p in self.account.positions.items()
        )
        available = self.account.cash
        frozen = self.account.frozen_cash
        realized = self.account.realized_pnl
        ic = self.account.initial_capital
        cum_fee = sum((t.total_fee or 0.0) for t in self.account.trades)
        sell_fee = sum((t.total_fee or 0.0) for t in self.account.trades
                       if t.side.value == "SELL")
        buy_fee = cum_fee - sell_fee

        total_assets = available + frozen + mv
        # realized_gross = 已实现盈亏 + 卖出费用 = 毛利（不含任何费用的盈亏）
        realized_gross = realized + sell_fee
        # 恒等式 B：总资金 = 期初 + 毛利 + 浮动盈亏 - 累计全部费用（买+卖）
        alt = ic + realized_gross + unrealized - cum_fee

        tol = 0.01 * max(1.0, abs(total_assets))
        return {
            "total_assets": round(total_assets, 2),
            "available": round(available, 2),
            "frozen_cash": round(frozen, 2),
            "market_value": round(mv, 2),
            "unrealized": round(unrealized, 2),
            "realized": round(realized, 2),
            "realized_gross": round(realized_gross, 2),
            "cumulative_fee": round(cum_fee, 2),
            "buy_fee": round(buy_fee, 2),
            "identity_a_ok": abs(total_assets - (available + frozen + mv)) < 1e-6,
            "identity_b_ok": abs(total_assets - alt) < tol,
            "delta_b": round(total_assets - alt, 4),
            "price_ready": len(unavailable) == 0,
            "price_unavailable": sorted(unavailable),
        }

    def position_details(self) -> List[dict]:
        prices, unavailable = self._current_prices_with_flags()
        details = []
        for code, pos in self.account.positions.items():
            price = prices.get(code, 0.0)
            mv = pos.market_value(price)
            upnl = pos.unrealized_pnl(price)
            cost = pos.cost_price * pos.quantity
            details.append({
                "code": code,
                "name": pos.name,
                "quantity": pos.quantity,
                "available": pos.available,
                "cost_price": round(pos.cost_price, 3),
                "current_price": round(price, 3),
                "market_value": round(mv, 2),
                "cost_amount": round(cost, 2),
                "unrealized_pnl": round(upnl, 2),
                "unrealized_pct": round((upnl / cost * 100), 4) if cost else 0.0,
                "price_ready": code not in unavailable,
            })
        return details
