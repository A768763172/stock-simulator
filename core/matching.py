"""交易撮合模块。

核心职责：对订单执行 A股实盘规则校验并撮合成交。
校验顺序（任一不通过即拒单）：
  1) 交易时段（9:30-11:30 / 13:00-15:00）
  2) 交易日
  3) 整手（100 股整数倍）
  4) 涨跌幅限制（涨停/跌停区间，优先用行情返回值）
  5) 资金 / 可卖数量（T+1：当日买入不可卖）
撮合后按三费标准计费并结算账户、更新持仓与已实现盈亏。
限价单未触达时挂单（冻结资金/股票），可重试或撤单。
"""
import uuid
from datetime import datetime
from typing import Optional

from core.order import Order, Trade, Side, OrderType, OrderStatus
from core.trading_rules import TradingRules
from core.position import PositionCalculator
from market.base import MarketProvider, Quote
from core.account import AccountManager


class MatchingEngine:
    def __init__(self, account: AccountManager, rules: TradingRules, market: MarketProvider):
        self.account = account
        self.rules = rules
        self.market = market
        self._frozen_cash: dict = {}     # order_id -> 冻结资金
        self._frozen_shares: dict = {}   # order_id -> 冻结股数

    # ---------- 工具 ----------
    @staticmethod
    def _new_id(prefix: str) -> str:
        return f"{prefix}{uuid.uuid4().hex[:12]}"

    def _add_frozen(self, oid: str, amount: float) -> None:
        """记录挂单冻结资金，并同步到账户（供清算勾稽：总资金=可用+冻结+市值）。"""
        self._frozen_cash[oid] = amount
        self.account.frozen_cash += amount

    def _pop_frozen(self, oid: str) -> float:
        """释放挂单冻结资金，并同步到账户。"""
        amt = self._frozen_cash.pop(oid, 0.0)
        self.account.frozen_cash -= amt
        return amt

    def _compute_fees(self, side: Side, amount: float):
        f = self.account.cfg.fee
        commission = max(amount * f.commission_rate, f.commission_min)
        stamp = amount * f.stamp_rate if side == Side.SELL else 0.0
        transfer = amount * f.transfer_rate
        total = commission + stamp + transfer
        return commission, stamp, transfer, total

    # ---------- 入口 ----------
    def submit_order(self, code: str, name: str, side: Side, quantity: int,
                     order_type: OrderType = OrderType.MARKET, price: float = 0.0,
                     now: Optional[datetime] = None) -> Order:
        now = now or datetime.now()
        order = Order(
            order_id=self._new_id("ORD"), code=code, name=name, side=side,
            order_type=order_type, price=price, quantity=quantity,
            created_at=now, trading_day=self.account.current_day,
        )
        return self.process_order(order, now, reserved=False)

    def process_order(self, order: Order, now: Optional[datetime] = None,
                      reserved: bool = False) -> Order:
        now = now or datetime.now()
        if order.status in (OrderStatus.FILLED, OrderStatus.REJECTED, OrderStatus.CANCELLED):
            return order

        # 1) 交易时段
        if not self.rules.is_trading_time(now):
            return self._reject(order, "非交易时段，不撮合")
        # 2) 交易日
        if self.account.current_day and not self.market.is_trading_day(self.account.current_day):
            return self._reject(order, "非交易日")
        # 3) 整手
        if not self.rules.validate_lot(order.quantity):
            return self._reject(order, f"数量须为{self.account.cfg.rules.lot_size}股整数倍")
        # 行情
        try:
            quote = self.market.get_quote(order.code)
        except Exception as e:
            return self._reject(order, f"行情获取失败: {e}")

        # 3.5) 停牌拦截：停牌标的禁止撮合（行情校验层 detect_suspension）
        from market.feed import detect_suspension
        if detect_suspension(quote.price, quote.prev_close,
                              getattr(quote, "volume", 0),
                              getattr(quote, "suspended", False)):
            return self._reject(order, "标的停牌中，禁止交易")

        # 4) 成交价
        fill_price = self._resolve_fill_price(order, quote)
        if fill_price is None:
            if order.order_id in self._frozen_cash or order.order_id in self._frozen_shares:
                return order  # 已挂单，继续等待触价
            return self._queue(order, quote)
        # 4.0) 零价防护：行情源中断时 quote.price 可能为 0（如 2026-10-13 14:31
        #      红利ETF 被以 0 元清仓，造成 -690.94 的伪亏损）。任何 <=0 的成交价
        #      都视为无效行情，直接拒单，绝不允许据此撮合与记账。
        if fill_price <= 0:
            return self._reject(order, f"行情价格无效({fill_price})，拒绝以零价成交")
        # 4.1) 滑点：仅市价单计入（限价单按限价精确成交，滑点=0）。
        #      买入支付更高、卖出收到更低，并纳入后续费用基数。
        if order.order_type == OrderType.MARKET and self.account.cfg.fee.slippage_rate:
            slip = self.account.cfg.fee.slippage_rate
            if order.side == Side.BUY:
                fill_price = round(fill_price * (1.0 + slip), 4)
            else:
                fill_price = round(fill_price * (1.0 - slip), 4)
        # 5) 涨跌幅
        if not self.rules.validate_price_limit(fill_price, quote.ceiling, quote.floor):
            return self._reject(order, f"价格 {fill_price} 超出涨跌停区间 [{quote.floor}, {quote.ceiling}]")

        reserved = reserved or (order.status == OrderStatus.PENDING and
                       (order.order_id in self._frozen_cash or order.order_id in self._frozen_shares))
        if order.side == Side.BUY:
            return self._match_buy(order, quote, fill_price, now, reserved)
        return self._match_sell(order, quote, fill_price, now, reserved)

    def retry_pending(self, now: Optional[datetime] = None) -> list:
        """重试所有挂单（如行情变动触发限价成交）。"""
        results = []
        for order in list(self.account.orders.values()):
            if order.status == OrderStatus.PENDING:
                results.append(self.process_order(order, now, reserved=True))
        return results

    def cancel_order(self, order_id: str) -> Order:
        order = self.account.orders.get(order_id)
        if order is None or order.status != OrderStatus.PENDING:
            return order
        self._release(order)
        order.status = OrderStatus.CANCELLED
        return order

    # ---------- 内部 ----------
    def _resolve_fill_price(self, order: Order, quote: Quote) -> Optional[float]:
        if order.order_type == OrderType.MARKET:
            return quote.price
        if order.side == Side.BUY:
            return quote.price if quote.price <= order.price else None
        return quote.price if quote.price >= order.price else None

    def _queue(self, order: Order, quote: Quote) -> Order:
        if order.side == Side.BUY:
            est_amount = order.price * order.quantity
            est = est_amount + self._compute_fees(Side.BUY, est_amount)[3]
            if self.account.cash < est:
                return self._reject(order, "资金不足（无法冻结挂单保证金）")
            self.account.cash -= est
            self._add_frozen(order.order_id, est)
        else:
            pos = self.account.get_position(order.code)
            if pos is None or pos.available < order.quantity:
                return self._reject(order, "可卖数量为0或不足（T+1）")
            # 仅冻结（减可用、加冻结）；成交时不再二次扣减 available
            pos.available -= order.quantity
            pos.frozen += order.quantity
            self._frozen_shares[order.order_id] = order.quantity
        order.status = OrderStatus.PENDING
        self.account.orders[order.order_id] = order
        return order

    def _match_buy(self, order, quote, fill_price, now, reserved) -> Order:
        amount = fill_price * order.quantity
        commission, stamp, transfer, total = self._compute_fees(Side.BUY, amount)
        cash_needed = amount + total
        if reserved:
            self.account.cash += self._pop_frozen(order.order_id)
        if self.account.cash < cash_needed:
            return self._reject(order, "资金不足")
        self.account.cash -= cash_needed
        pos = self.account.ensure_position(order.code, order.name)
        PositionCalculator.update_after_buy(pos, order.quantity, amount)
        trade = self._make_trade(order, quote, fill_price, amount,
                                 commission, stamp, transfer, total,
                                 cash_flow=-cash_needed, realized=0.0, now=now)
        self._record(order, trade, OrderStatus.FILLED)
        return order

    def _match_sell(self, order, quote, fill_price, now, reserved) -> Order:
        pos = self.account.get_position(order.code)
        if not reserved:
            if pos is None or pos.available < order.quantity:
                return self._reject(order, "可卖数量为0或不足（T+1）")
        amount = fill_price * order.quantity
        commission, stamp, transfer, total = self._compute_fees(Side.SELL, amount)
        cash_in = amount - total
        realized = (fill_price - pos.cost_price) * order.quantity - total
        self.account.cash += cash_in
        if reserved:
            # 挂单时已扣减 available 并增加 frozen：此处仅清减数量与冻结，
            # 不再二次扣减 available（修复历史版本「可用股数变负」缺陷）。
            pos.quantity -= order.quantity
            pos.frozen = max(0, pos.frozen - order.quantity)
            if pos.quantity <= 0:
                pos.quantity = 0
                pos.available = 0
                pos.frozen = 0
                pos.cost_price = 0.0
        else:
            PositionCalculator.update_after_sell(pos, order.quantity)
        # 清仓后从持仓字典移除（避免残留空持仓影响 len(positions)/快照统计）
        if pos.quantity <= 0:
            self.account.positions.pop(order.code, None)
        self.account.realized_pnl += realized
        trade = self._make_trade(order, quote, fill_price, amount,
                                 commission, stamp, transfer, total,
                                 cash_flow=cash_in, realized=realized, now=now)
        self._record(order, trade, OrderStatus.FILLED)
        return order

    def _make_trade(self, order, quote, price, amount, commission, stamp,
                    transfer, total, cash_flow, realized, now) -> Trade:
        return Trade(
            trade_id=self._new_id("TRD"), order_id=order.order_id,
            code=order.code, name=order.name, side=order.side, price=price,
            quantity=order.quantity, amount=amount, commission=commission,
            stamp_tax=stamp, transfer_fee=transfer, total_fee=total,
            cash_flow=cash_flow, realized_pnl=realized, datetime=now,
            trading_day=self.account.current_day,
        )

    def _record(self, order, trade, status) -> None:
        self.account.trades.append(trade)
        self.account.orders[order.order_id] = order
        order.quantity_filled = order.quantity
        order.status = status
        self._pop_frozen(order.order_id)
        self._frozen_shares.pop(order.order_id, None)

    def _release(self, order) -> None:
        fc = self._pop_frozen(order.order_id)
        if fc:
            self.account.cash += fc
        fs = self._frozen_shares.pop(order.order_id, 0)
        if fs:
            pos = self.account.get_position(order.code)
            if pos:
                pos.available += fs
                pos.frozen = max(0, pos.frozen - fs)

    def _reject(self, order, reason) -> Order:
        self._release(order)
        order.status = OrderStatus.REJECTED
        order.reject_reason = reason
        self.account.orders[order.order_id] = order
        return order
