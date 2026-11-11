"""交易智能体（量化交易专家主导，稳定复利风格）。

统一调度：
- 选股：Screener（选股池 + 多因子）产出排序候选与目标权重。
- 建仓：首次买入目标仓位的 40%；盈利前提下回调 2% 加 30%，再回调 2% 加剩余 30%。
- 退出：浮亏 3% 止损；盈利 8% 止盈 50%；盈利 15% 清仓；持股超 15 日时限退出。
- 风控：回撤铁律状态机（RiskController）强制降仓 / 只卖不买 / 暂停开仓。
- 约束：单股≤5%、单行业≤15%、总仓 20%-80%、周新开≤5、周交易≤5。

所有动作通过 Simulator 撮合引擎执行，受 T+1 / 整手 / 涨跌幅 / 手续费等实盘规则约束。
"""
from datetime import date, datetime

from config.strategy import StrategyConfig
from core.order import Side, OrderType, OrderStatus
from core.simulator import Simulator
from strategy.screener import Screener
from strategy.position import PositionPlanner
from strategy.risk import RiskController, RiskMode


class StrategyAgent:
    def __init__(self, sim: Simulator, cfg: StrategyConfig = None,
                 storage=None):
        self.sim = sim
        self.market = sim.market
        self.cfg = cfg or StrategyConfig()
        self.screener = Screener(self.cfg, self.market)
        self.planner = PositionPlanner(self.cfg)
        self.risk = RiskController(self.cfg, self.market)
        self.storage = storage

        # 每只持仓的运行态
        self.pos_state: dict = {}
        # 退出流水（供自学习引擎做盈亏归因）：{eid, code, reason, qty, price, day, pnl_pct, entry_factors}
        self.exit_log: list = []
        # 决策日志（供看板"决策依据/风控触发"面板，呈现智能体思考过程）
        self.decision_log: list = []
        self._last_mode: str = None
        self._last_regime: str = None
        # 自学习引擎（可选，收盘后复盘接入）
        self.learner = None
        # 周计数
        self.week_key: str = None
        self.new_opens_week: int = 0
        self.trades_week: int = 0
        # 当日已交易标的（防止同周期同方向信号重复触发 / 信号闪烁：
        # 当日卖出后不再于同日重新开仓同一标的）
        self._daily_traded: set = set()
        # 当日已交易集合所属交易日（跨盘中多次调用保留，仅新交易日重置，防闪烁重复开仓）
        self._daily_traded_day: str = None

        if self.storage:
            self.load()

    # ---------- 工具 ----------
    def equity(self) -> float:
        return float(self.sim.snapshot()["total_assets"])

    def _prices(self) -> dict:
        out = {}
        for code in self.sim.account.positions:
            try:
                out[code] = self.market.get_quote(code).price
            except Exception:
                out[code] = 0.0
        return out

    def _count_trade(self) -> None:
        self.trades_week += 1

    # ---------- 决策日志（看板"思考过程"） ----------
    def _log(self, etype: str, code: str, name: str, detail: str,
             metrics: dict = None, ts: datetime = None) -> None:
        """追加一条决策日志；保留最近 600 条，避免无限增长。

        ts: 可选业务时间（如撮合用的 now）。回放/盘中扫描时用墙钟会与成交流水
        时间对不上，故调用方可显式传入；缺省仍取当前时间。
        """
        from datetime import datetime as _dt
        self.decision_log.append({
            "ts": (ts or _dt.now()).isoformat(timespec="seconds"),
            "day": (self.sim.account.current_day.isoformat()
                    if self.sim.account.current_day else ""),
            "type": etype, "code": code, "name": name,
            "detail": detail, "metrics": metrics or {},
        })
        if len(self.decision_log) > 600:
            self.decision_log = self.decision_log[-600:]

    # ---------- 每日调度 ----------
    def daily_step(self, day: date, now: datetime = None) -> dict:
        if not self.market.is_trading_day(day):
            self.sim.set_day(day)
            return self.status()
        now = now or datetime(day.year, day.month, day.day, 10, 0, 0)
        self.sim.set_day(day)

        # 四维学习环境感知（盘中即时）：更新政策/消息/情绪环境态并动态适配仓位、
        # 止盈止损、新开仓频率与风控灵敏度。无 learner 时跳过，行为不变。
        if self.learner is not None:
            self.learner.sense_environment(day, now)

        wk = RiskController._week_key(day)
        if wk != self.week_key:
            self.week_key = wk
            self.new_opens_week = 0
            self.trades_week = 0

        # 每个交易日重置「当日已交易标的」集合（防止同周期重复下单 / 信号闪烁）
        self._daily_traded = set()

        equity_start = self.equity()
        self.risk.update(equity_start, day)

        for st in self.pos_state.values():
            st["holding_days"] = st.get("holding_days", 0) + 1

        # 1) 退出（随时允许卖出）
        self._manage_exits(now)

        # 2) 风控强制动作
        if self.risk.mode == RiskMode.DEFENSE_DAILY:
            self._reduce_to(self.cfg.dd_daily_reduce_to, now)
        elif self.risk.sell_only():
            self._reduce_to(self.risk.position_cap(), now)

        # 3) 新开仓（仅正常市况且未被暂停）
        if self.risk.mode == RiskMode.NORMAL and self.risk.allow_open(day):
            self._rebalance_entries(now)

        equity_end = self.equity()
        self.risk.close(equity_end, day)

        # 风控状态切换日志（仅在状态变化时记录，避免刷屏）
        if self.risk.mode.value != self._last_mode:
            if self.risk.mode != RiskMode.NORMAL:
                self._log("RISK_TRIGGER", "", "",
                          f"风控状态切换为{self.risk.mode.value}："
                          f"强制降仓/只卖不买，次日暂停开新仓")
                if self.storage:
                    self.storage.append_risk_event({
                        "ts": now.isoformat(timespec="seconds") if now else "",
                        "type": "RISK_TRIGGER",
                        "code": "", "qty": 0, "price": 0.0,
                        "day": (self.sim.account.current_day.isoformat()
                                if self.sim.account.current_day else ""),
                        "pnl_pct": 0.0,
                        "mode": self.risk.mode.value,
                        "peak_equity": round(self.risk.peak_equity, 2),
                        "prev_day_equity": round(self.risk.prev_day_equity, 2),
                        "equity": round(equity_end, 2),
                    })
            self._last_mode = self.risk.mode.value

        # 落盘：策略状态 + 引擎全量（账户/成交/快照/实时价）
        # 看板只读这些文件，因此持久化即"实时同步"，且不影响交易逻辑
        self.save()
        self.sim.save()
        return self.status()

    # ---------- 退出管理 ----------
    def _scan_exits(self, now: datetime, prices: dict = None) -> None:
        """纯退出扫描：止损 / 止盈 / 持仓到期。不触发任何买入。

        prices: 可选 {code: 现价} 覆盖（盘中高频监控传入实时价，避免触网）。
        缺省时使用 self._prices()（行情源，命中缓存时几乎零网络）。
        """
        if prices is None:
            prices = self._prices()
        for code in list(self.pos_state.keys()):
            st = self.pos_state[code]
            pos = self.sim.account.get_position(code)
            if pos is None or pos.quantity <= 0:
                self.pos_state.pop(code, None)
                continue
            price = prices.get(code, 0.0)
            if price <= 0 or pos.cost_price <= 0:
                continue
            pnl = (price - pos.cost_price) / pos.cost_price
            if pnl <= -self.cfg.stop_loss_pct:
                self._sell_all(code, now, "STOP_LOSS"); continue
            if pnl >= self.cfg.tp2_pct:
                self._sell_all(code, now, "TAKE_PROFIT"); continue
            if pnl >= self.cfg.tp1_pct and not st.get("tp1_done"):
                self._sell_half(code, now, "TAKE_PROFIT")
                st["tp1_done"] = True
            if st.get("holding_days", 0) >= self.cfg.hold_max_days:
                self._sell_all(code, now, "HOLD_TIMEOUT"); continue

    def _manage_exits(self, now: datetime) -> None:
        # 完整退出（含盈利加仓）：盘中完整决策（intraday_step）与收盘 daily_step 共用
        self._scan_exits(now, self._prices())
        # 盈利加仓（分批建仓后段）：仅在完整决策中评估，不在高频纯退出监控里
        prices = self._prices()
        for code, st in list(self.pos_state.items()):
            pos = self.sim.account.get_position(code)
            if pos is None or pos.quantity <= 0:
                continue
            price = prices.get(code, 0.0)
            if price <= 0 or pos.cost_price <= 0:
                continue
            pnl = (price - pos.cost_price) / pos.cost_price
            if self.cfg.allow_pyramid_win and pnl >= 0 and not self.cfg.allow_avg_down:
                step = st.get("batch_step", 1) + 1
                if step <= len(self.cfg.batch_plan):
                    threshold = self.planner.pullback_threshold(st["entry_price"], step)
                    if price <= threshold:
                        self._buy_batch(code, st, step, now)

    def _refresh_quotes(self, codes) -> None:
        """刷新给定标的的实时价缓存（供盘中撮合使用实时成交价）。

        优先用行情源的并行预热（warm_universe，westock 始终重抓 quote）；
        不支持时退回清掉 quote 缓存强制重抓。best-effort，异常不影响主流程。
        """
        codes = list(dict.fromkeys(codes or []))
        if not codes:
            return
        m = self.market
        if hasattr(m, "warm_universe"):
            try:
                m.warm_universe(codes, max_workers=8, refresh_fund=False)
                return
            except Exception:
                pass
        qb = getattr(m, "_cache", {}).get("quote", None) if hasattr(m, "_cache") else None
        if qb is not None:
            for c in codes:
                qb.pop(c, None)

    # ---------- 盘中实时 ----------
    def intraday_monitor(self, now: datetime, live_prices: dict = None) -> int:
        """高频实时退出监控（纯本地、不触网）：基于实时价扫描止损/止盈/到期并即时卖出。

        返回本次触发的退出笔数（含整清仓与半仓止盈，>0 时落盘持久化，供看板实时同步）。
        live_prices: {code: 现价}（通常来自看板同源的 live_prices.json）。
        """
        before = len(self.sim.account.trades)
        self._scan_exits(now, live_prices)
        triggered = len(self.sim.account.trades) - before
        if triggered > 0:
            self._log("INTRADAY_EXIT", "", "",
                      f"盘中实时退出 {triggered} 笔（止损/止盈/到期）",
                      {"exits": triggered})
            self.save()
            self.sim.save()
        return triggered

    def intraday_step(self, day: date, now: datetime = None) -> dict:
        """盘中实时完整决策（低频调用，如每 5 分钟）：与 daily_step 同源，
        但①不重复累计 holding_days（由收盘 daily_step 负责，避免盘中多 tick 虚增）；
        ②当日已交易集合跨盘中调用保留（防信号闪烁重复开仓），仅新交易日重置；
        ③实时跟踪回撤（risk.update 用当前净值）。

        注意：不替代收盘 daily_step（含 review/复盘/参数优化），仅把"盘中也能决策"
        落地；持仓周期计数仍由每日收盘的 daily_step 推进。
        """
        if not self.market.is_trading_day(day):
            self.sim.set_day(day)
            return self.status()
        now = now or datetime.now()
        self.sim.set_day(day)
        if self.learner is not None:
            self.learner.sense_environment(day, now)

        wk = RiskController._week_key(day)
        if wk != self.week_key:
            self.week_key = wk
            self.new_opens_week = 0
            self.trades_week = 0

        # 当日已交易集合：跨盘中调用保留，防同周期信号闪烁重复开仓；新交易日重置
        if self._daily_traded_day != day.isoformat():
            self._daily_traded = set()
            self._daily_traded_day = day.isoformat()

        # 刷新持仓实时价缓存，确保回撤跟踪与执行价实时
        held = list(self.sim.account.positions.keys())
        self._refresh_quotes(held)
        equity_start = self.equity()
        self.risk.update(equity_start, day)

        # 1) 退出（含盈利加仓）
        self._manage_exits(now)

        # 2) 风控强制动作
        if self.risk.mode == RiskMode.DEFENSE_DAILY:
            self._reduce_to(self.cfg.dd_daily_reduce_to, now)
        elif self.risk.sell_only():
            self._reduce_to(self.risk.position_cap(), now)

        # 3) 新开仓（仅正常市况且未被暂停）
        if self.risk.mode == RiskMode.NORMAL and self.risk.allow_open(day):
            candidates = self.screener.build(env=self._env_state(),
                                             dim_weights=self._dim_weights())
            self._refresh_quotes([c.code for c in candidates])
            total_assets = self.equity()
            for cand in candidates:
                if self.new_opens_week >= self.cfg.max_new_open_per_week:
                    break
                if self.trades_week >= self.cfg.max_trades_per_week:
                    break
                if cand.code in self.pos_state:
                    continue
                if cand.code in self._daily_traded:
                    continue
                try:
                    price = self.market.get_quote(cand.code).price
                except Exception:
                    continue
                if price <= 0:
                    continue
                target_shares = self.planner.target_shares(
                    total_assets, cand.target_weight, price)
                full_lot = self.planner.to_lot(target_shares)
                first_batch = self.planner.batch_shares(target_shares, 1)
                if first_batch < 100 and full_lot >= 100:
                    qty = full_lot
                else:
                    qty = first_batch
                if qty <= 0:
                    continue
                if not self._can_open(total_assets, price, qty, cand.target_weight):
                    continue
                o = self.sim.buy(cand.code, qty, OrderType.MARKET, 0.0, now)
                if o.status == OrderStatus.FILLED:
                    self._count_trade()
                    self.new_opens_week += 1
                    self._daily_traded.add(cand.code)
                    ef = cand.factors or {}
                    self._log("OPEN", cand.code, cand.name,
                              f"盘中开仓 目标权重{cand.target_weight:.1%} "
                              f"{'（整目标建满·小账户）' if qty == full_lot and first_batch < 100 else '首仓40%'} "
                              f"因子 q={ef.get('quality',0):.2f} "
                              f"v={ef.get('valuation',0):.2f} "
                              f"w={ef.get('low_vol',0):.2f} "
                              f"m={ef.get('momentum',0):.2f}",
                              {"weight": round(cand.target_weight, 4),
                               "price": round(price, 3), "qty": qty})
                    built_step = (len(self.cfg.batch_plan)
                                  if (qty == full_lot and first_batch < 100)
                                  else 1)
                    self.pos_state[cand.code] = {
                        "entry_day": self.sim.account.current_day.isoformat()
                        if self.sim.account.current_day else None,
                        "entry_price": price,
                        "entry_factors": cand.factors,
                        "entry_env": (self.learner.env_state.to_dict()
                                      if self.learner else None),
                        # 开仓时快照当前策略权重混合，供清仓时按「开仓策略构成」
                        # 把该笔盈亏归因到 strategy_eff，使策略类型权重真正按
                        # 真实盈亏进化（此前 strategy_eff 从未被累计，权重只会
                        # 盲从环境标签漂移）
                        "entry_strategy_weights": dict(self.cfg.strategy_weights),
                        "target_shares": target_shares,
                        "batch_step": built_step,
                        "last_buy_price": price,
                        "tp1_done": False,
                        "holding_days": 0,
                    }

        equity_end = self.equity()
        self.risk.close(equity_end, day)

        if self.risk.mode.value != self._last_mode:
            if self.risk.mode != RiskMode.NORMAL:
                self._log("RISK_TRIGGER", "", "",
                          f"风控状态切换为{self.risk.mode.value}："
                          f"强制降仓/只卖不买，次日暂停开新仓")
                if self.storage:
                    self.storage.append_risk_event({
                        "ts": now.isoformat(timespec="seconds") if now else "",
                        "type": "RISK_TRIGGER",
                        "code": "", "qty": 0, "price": 0.0,
                        "day": (self.sim.account.current_day.isoformat()
                                if self.sim.account.current_day else ""),
                        "pnl_pct": 0.0,
                        "mode": self.risk.mode.value,
                        "peak_equity": round(self.risk.peak_equity, 2),
                        "prev_day_equity": round(self.risk.prev_day_equity, 2),
                        "equity": round(equity_end, 2),
                    })
            self._last_mode = self.risk.mode.value

        self.save()
        self.sim.save()
        return self.status()

    def _filled_price(self, order, trade_from: int) -> float:
        """取本次卖出的**真实成交价**。

        旧实现卖出后用 `self._prices().get(code, 0.0)` 回填，行情映射缺失/取数
        失败时即写成 0（决策日志出现「价格0.000 盈亏-100%」）。撮合结果本身已
        带成交价，直接取刚落库的 Trade 最可靠，且与「实时交易流水」完全同源。
        """
        trades = self.sim.account.trades
        for t in reversed(trades[trade_from:]):
            if (getattr(t, "order_id", "") == order.order_id
                    and getattr(t, "code", "") == order.code):
                return float(getattr(t, "price", 0.0) or 0.0)
        return 0.0

    def _sell_all(self, code: str, now: datetime, reason: str = "RULE") -> None:
        pos = self.sim.account.get_position(code)
        if pos is None or pos.available <= 0:
            return
        qty = pos.available
        name = pos.name                      # 清仓后持仓对象被移除，名称需先留存
        before = len(self.sim.account.trades)
        o = self.sim.sell(code, qty, OrderType.MARKET, 0.0, now)
        if o.status == OrderStatus.FILLED:
            self._count_trade()
            price = (self._filled_price(o, before)
                     or self._prices().get(code, 0.0))
            self._record_exit(code, reason, qty, price, now, name)
            if pos.quantity <= 0:
                self.pos_state.pop(code, None)

    def _sell_half(self, code: str, now: datetime, reason: str = "RULE") -> None:
        pos = self.sim.account.get_position(code)
        if pos is None or pos.available <= 0:
            return
        qty = self.planner.to_lot(pos.available // 2)
        if qty <= 0:
            return
        name = pos.name
        before = len(self.sim.account.trades)
        o = self.sim.sell(code, qty, OrderType.MARKET, 0.0, now)
        if o.status == OrderStatus.FILLED:
            self._count_trade()
            price = (self._filled_price(o, before)
                     or self._prices().get(code, 0.0))
            self._record_exit(code, reason, qty, price, now, name)

    def _record_exit(self, code: str, reason: str, qty: int, price: float,
                     now: datetime, name: str = "") -> None:
        """记录一笔退出（供自学习引擎盈亏归因 + 看板决策日志 + 风控日志）。"""
        st = self.pos_state.get(code, {})
        entry = st.get("entry_price") or price
        # 兜底：成交价缺失时退回成本价，避免算出 -100% 的伪盈亏
        if price <= 0:
            price = entry
        pnl_pct = (price - entry) / entry if entry > 0 else 0.0
        # 当日已交易标的（防同周期重复下单 / 信号闪烁）
        self._daily_traded.add(code)
        # 风控降仓写入独立风控日志（完整可追溯）
        if reason == "RISK_REDUCE" and self.storage:
            self.storage.append_risk_event({
                "ts": now.isoformat(timespec="seconds") if now else "",
                "type": "RISK_REDUCE",
                "code": code, "qty": qty, "price": round(price, 3),
                "day": (self.sim.account.current_day.isoformat()
                        if self.sim.account.current_day else ""),
                "pnl_pct": round(pnl_pct, 4),
                "mode": self.risk.mode.value,
            })
        label = {
            "STOP_LOSS": "止损", "TAKE_PROFIT": "止盈",
            "HOLD_TIMEOUT": "持有到期", "RISK_REDUCE": "风控降仓",
        }.get(reason, reason)
        self.exit_log.append({
            "eid": f"{code}-{self.sim.account.current_day}-{reason}-{len(self.exit_log)}",
            "code": code, "reason": reason, "qty": qty, "price": price,
            "day": self.sim.account.current_day.isoformat()
            if self.sim.account.current_day else "",
            "pnl_pct": round(pnl_pct, 4),
            "entry_factors": st.get("entry_factors", {}),
            "entry_env": st.get("entry_env"),
            "entry_strategy_weights": st.get("entry_strategy_weights"),
            "exit_env": (self.learner.env_state.to_dict()
                         if self.learner else None),
        })
        # 名称：清仓后持仓对象已移除，优先用调用方留存的名字，避免日志只剩代码
        pos_now = self.sim.account.get_position(code)
        show_name = name or (pos_now.name if pos_now else code)
        detail = f"{label} 成交价{price:.3f} 盈亏{pnl_pct:+.2%} 数量{qty}股"
        if reason == "STOP_LOSS" and entry > 0:
            # 同时给出止损线（判定阈值），便于核对「为什么在这个价位被卖出」
            detail += f" 止损线{entry * (1 - self.cfg.stop_loss_pct):.3f}"
        self._log(label, code, show_name, detail,
                  {"reason": reason, "pnl_pct": round(pnl_pct, 4),
                   "price": round(price, 3)}, ts=now)

    def _buy_batch(self, code: str, st: dict, step: int, now: datetime) -> None:
        target = st.get("target_shares", 0)
        qty = self.planner.batch_shares(target, step)
        if qty <= 0:
            return
        price = self._prices().get(code, 0.0)
        if price <= 0 or not self._cash_ok(price, qty):
            return
        o = self.sim.buy(code, qty, OrderType.MARKET, 0.0, now)
        if o.status == OrderStatus.FILLED:
            self._count_trade()
            st["batch_step"] = step
            st["last_buy_price"] = price
            self._log("ADD", code, self.sim.account.get_position(code).name,
                      f"盈利加仓第{step}批 回调至{price:.3f} 加仓{qty}股",
                      {"price": round(price, 3), "step": step})

    # ---------- 新开仓 ----------
    def _rebalance_entries(self, now: datetime) -> None:
        candidates = self.screener.build(env=self._env_state(),
                                         dim_weights=self._dim_weights())
        total_assets = self.equity()
        for cand in candidates:
            if self.new_opens_week >= self.cfg.max_new_open_per_week:
                break
            if self.trades_week >= self.cfg.max_trades_per_week:
                break
            if cand.code in self.pos_state:
                continue
            # 防信号闪烁：当日已卖出过的标的，当日不再重新开仓
            if cand.code in self._daily_traded:
                continue
            try:
                price = self.market.get_quote(cand.code).price
            except Exception:
                continue
            if price <= 0:
                continue
            target_shares = self.planner.target_shares(
                total_assets, cand.target_weight, price)
            full_lot = self.planner.to_lot(target_shares)
            # 首仓 40%；但小账户下 40% 取整不足 1 手时，若整目标≥1 手且可负担，
            # 则一次性开出整目标（小仓位无法分批，直接建满，保证能开仓）。
            first_batch = self.planner.batch_shares(target_shares, 1)
            if first_batch < 100 and full_lot >= 100:
                qty = full_lot
            else:
                qty = first_batch
            if qty <= 0:
                continue
            if not self._can_open(total_assets, price, qty, cand.target_weight):
                continue
            o = self.sim.buy(cand.code, qty, OrderType.MARKET, 0.0, now)
            if o.status == OrderStatus.FILLED:
                self._count_trade()
                self.new_opens_week += 1
                self._daily_traded.add(cand.code)
                ef = cand.factors or {}
                self._log("OPEN", cand.code, cand.name,
                          f"新开仓 目标权重{cand.target_weight:.1%} "
                          f"{'（整目标建满·小账户）' if qty == full_lot and first_batch < 100 else '首仓40%'} "
                          f"因子 q={ef.get('quality',0):.2f} "
                          f"v={ef.get('valuation',0):.2f} "
                          f"w={ef.get('low_vol',0):.2f} "
                          f"m={ef.get('momentum',0):.2f}",
                          {"weight": round(cand.target_weight, 4),
                           "price": round(price, 3), "qty": qty})
                # 整目标一次建满时标记批次已完成，避免后续无意义金字塔加仓
                built_step = (len(self.cfg.batch_plan)
                              if (qty == full_lot and first_batch < 100)
                              else 1)
                self.pos_state[cand.code] = {
                    "entry_day": self.sim.account.current_day.isoformat()
                    if self.sim.account.current_day else None,
                    "entry_price": price,
                    "entry_factors": cand.factors,
                    "entry_env": (self.learner.env_state.to_dict()
                                  if self.learner else None),
                    # 开仓时快照当前策略权重混合，供清仓时按「开仓策略构成」归因
                    "entry_strategy_weights": dict(self.cfg.strategy_weights),
                    "target_shares": target_shares,
                    "batch_step": built_step,
                    "last_buy_price": price,
                    "tp1_done": False,
                    "holding_days": 0,
                }

    # ---------- 约束检查 ----------
    def _position_ceiling(self) -> float:
        """有效总仓上限：风控铁律应急上限（NORMAL=1.0，即不可杠杆），
        若配置了硬性 total_pos_max 则取更严格者。自适应模式下 total_pos_max=None，
        仅受风控状态机约束。"""
        cap = self.risk.position_cap()
        if self.cfg.total_pos_max is not None:
            cap = min(cap, self.cfg.total_pos_max)
        return cap

    def _cash_ok(self, price: float, qty: int) -> bool:
        need = price * qty
        # 自适应模式：现金下限由目标仓位反推（不得把总仓推过目标），
        # 与硬性 min_cash_reserve 取较严格者。
        floor = self.cfg.min_cash_reserve
        if self.cfg.adaptive_position and self.cfg.base_total_target is not None:
            floor = max(floor, 1.0 - self.cfg.base_total_target)
        return self.sim.account.cash - need >= floor * self.equity()

    def _can_open(self, total_assets: float, price: float, qty: int,
                  weight: float) -> bool:
        if not self._cash_ok(price, qty):
            return False
        mv_now = self.sim.pnl.snapshot()["market_value"]
        new_mv = mv_now + price * qty
        if new_mv > self._position_ceiling() * total_assets:
            return False
        return True

    def _reduce_to(self, frac: float, now: datetime = None) -> None:
        """按比例减仓，使总市值 ≤ frac * 总资产（仅卖可卖部分）。

        按市值从大到小跨持仓均匀减持，直到满足 `need`；修复旧版本「卖出语句在
        for 循环体外、实际只处理了最后一个持仓」导致熔断降仓失效的缺陷。
        """
        now = now or datetime.now().replace(hour=10, minute=0, second=0)
        prices = self._prices()
        total_assets = self.equity()
        target_mv = frac * total_assets
        mv = sum(pos.quantity * prices.get(c, 0.0)
                 for c, pos in self.sim.account.positions.items())
        if mv <= target_mv or mv <= 0:
            return
        need = mv - target_mv
        ranked = sorted(
            self.sim.account.positions.items(),
            key=lambda kv: kv[1].quantity * prices.get(kv[0], 0.0),
            reverse=True,
        )
        for code, pos in ranked:
            if need <= 1e-6:
                break
            if pos.available <= 0:
                continue
            p = prices.get(code, 0.0)
            if p <= 0:
                continue
            sell_mv_here = min(need, pos.quantity * p)
            qty = self.planner.to_lot(int(sell_mv_here // p))
            if qty > pos.available:
                qty = pos.available
            if qty <= 0:
                continue
            o = self.sim.sell(code, qty, OrderType.MARKET, 0.0, now)
            if o.status == OrderStatus.FILLED:
                self._count_trade()
                self._record_exit(code, "RISK_REDUCE", qty, p, now)
                need -= qty * p
                if pos.quantity <= 0:
                    self.pos_state.pop(code, None)

    # ---------- 状态 / 持久化 ----------
    def status(self) -> dict:
        snap = self.sim.snapshot()
        total_assets = snap["total_assets"]
        pos_frac = (snap["market_value"] / total_assets) if total_assets else 0.0
        return {
            "day": self.sim.account.current_day.isoformat()
            if self.sim.account.current_day else None,
            "mode": self.risk.mode.value,
            "risk_position_cap": self.risk.position_cap(),
            "equity": round(total_assets, 2),
            "position_fraction": round(pos_frac, 4),
            "cash": snap["cash"],
            "new_opens_week": self.new_opens_week,
            "trades_week": self.trades_week,
            "held": len(self.pos_state),
            "peak_equity": round(self.risk.peak_equity, 2),
        }

    def screen_report(self) -> dict:
        return self.screener.report()

    def to_dict(self) -> dict:
        return {
            "pos_state": self.pos_state,
            "week_key": self.week_key,
            "new_opens_week": self.new_opens_week,
            "trades_week": self.trades_week,
            "exit_log": self.exit_log,
            "decision_log": self.decision_log,
            "risk": self.risk.to_dict(),
        }

    def save(self) -> None:
        if self.storage:
            self.storage.save_strategy_state(self.to_dict())

    def load(self) -> None:
        if not self.storage:
            return
        d = self.storage.load_strategy_state()
        if not d:
            return
        self.pos_state = d.get("pos_state", {})
        self.week_key = d.get("week_key")
        self.new_opens_week = d.get("new_opens_week", 0)
        self.trades_week = d.get("trades_week", 0)
        self.exit_log = d.get("exit_log", [])
        self.decision_log = d.get("decision_log", [])
        self._last_mode = d.get("risk", {}).get("mode")
        self.risk.load_dict(d.get("risk", {}))
        # 清洗被污染的风控峰值（demo/历史残留），避免永久锁死 DEFENSE_PEAK：
        # 峰值只能由真实净值经 close() 推高，不应凭空高于账户可达上限。
        try:
            from config.settings import INITIAL_CAPITAL
            acc = self.sim.account
            _has_traded = bool(acc.realized_pnl) or bool(acc.positions) or bool(acc.trades)
            if self.risk.sanitize(self.equity(), INITIAL_CAPITAL, _has_traded):
                self._log("RISK_SANITIZE", "", "",
                          "风控峰值异常已清洗，已钳制到账户可达上限"
                          "（防止 demo/历史污染永久触发 DEFENSE_PEAK）")
        except Exception:
            pass

    # ---------- 自学习接入 ----------
    def attach_learner(self, learner) -> None:
        self.learner = learner

    # ---------- 四维学习输入（供选股环境加成；无 learner 时为 None） ----------
    def _env_state(self):
        """当前市场环境态（政策/消息/情绪 + 激活条目）。未感知过时为 None。"""
        if self.learner is None:
            return None
        st = getattr(self.learner, "env_state", None)
        return st if st is not None and st.day else None

    def _dim_weights(self) -> dict:
        """四维学习权重（price/policy/news/sentiment，收盘归因再平衡产出）。"""
        if self.learner is None:
            return {}
        return dict(getattr(self.learner, "four_dim_weights", {}) or {})

    def review(self, day, now=None) -> dict:
        """收盘后（默认 15:30）触发自学习引擎复盘与参数进化。"""
        if self.learner is None:
            return {}
        result = self.learner.review(day, now)

        # 看板决策日志：市场风格 + 参数优化（每 5 日回测）
        regime = result.get("regime")
        if regime and regime != self._last_regime:
            self._log("REGIME", "", "",
                      f"市场风格识别为{self._regime_zh(regime)}，"
                      f"调整总仓中枢至 {self.cfg.base_total_target:.1%}")
            self._last_regime = regime
        bt = result.get("backtest")
        if bt:
            sl, tp1, tp2, base, stock = bt["combo"]
            self._log("PARAM_OPT", "", "",
                      f"历史回测优化：止损{sl:.0%}/止盈{tp1:.0%}&{tp2:.0%}/"
                      f"单股上限{stock:.0%} → 夏普{bt['sharpe']:.2f} "
                      f"回撤{bt['mdd']:.2%} 胜率{bt['wr']:.1%}",
                      {"sharpe": bt["sharpe"], "mdd": bt["mdd"],
                       "wr": bt["wr"], "ret": bt["ret"]})
        # 复盘新增的决策日志需落盘
        self.save()
        return result

    @staticmethod
    def _regime_zh(regime: str) -> str:
        return {"BULL": "牛市", "OSCILLATION": "震荡市",
                "BEAR": "熊市"}.get(regime, regime)

    def style_check(self, now=None) -> list:
        """周度风格校验：检测追高题材 / 仓位超标漂移并自动纠偏。

        返回漂移事件列表（已自动卖出纠偏）。
        """
        from datetime import datetime as _dt
        now = now or _dt.now().replace(hour=15, minute=30, second=0, microsecond=0)
        drifts: list = []
        total = self.equity()
        if total <= 0:
            return drifts
        prices = self._prices()
        mv = sum(pos.quantity * prices.get(c, 0.0)
                 for c, pos in self.sim.account.positions.items())

        # 1) 总仓位上限（仅风控应急上限触发时纠偏；自适应模式 NORMAL 下无硬性上限）
        ceiling = self._position_ceiling()
        if ceiling < 1.0 and mv > ceiling * total:
            for code, pos in list(self.sim.account.positions.items()):
                if pos.available <= 0:
                    continue
                p = prices.get(code, 0.0)
                if p <= 0:
                    continue
                target_mv = ceiling * total
                sell_mv = mv - target_mv
                qty = self.planner.to_lot(int(sell_mv // p))
                if qty > 0:
                    o = self.sim.sell(code, min(qty, pos.available),
                                      OrderType.MARKET, 0.0, now)
                    if o.status == OrderStatus.FILLED:
                        self._record_exit(code, "RISK_REDUCE", o.quantity_filled,
                                          o.price, now)
                        drifts.append(f"{code} 总仓位超限已降至风控上限")
                mv = target_mv
                break

        # 2) 单股上限 + 3) 追高题材
        for code, pos in list(self.sim.account.positions.items()):
            if pos.available <= 0:
                continue
            p = prices.get(code, 0.0)
            if p <= 0:
                continue
            w = pos.quantity * p / total
            st = self.pos_state.get(code, {})
            ef = st.get("entry_factors", {})
            # 追高题材：高估值分位 + 高动量
            try:
                fund = self.market.get_fundamentals(code)
                chasing = fund.pe_pct_3y > 0.85 and ef.get("momentum", 0.0) > 0.8
            except Exception:
                chasing = False
            if w > self.cfg.single_stock_max * 1.0001 or chasing:
                target_w = self.cfg.single_stock_max * (0.5 if chasing else 1.0)
                sell_mv = pos.quantity * p - target_w * total
                qty = self.planner.to_lot(int(sell_mv // p)) if sell_mv > 0 else 0
                if qty > 0 and qty <= pos.available:
                    o = self.sim.sell(code, qty, OrderType.MARKET, 0.0, now)
                    if o.status == OrderStatus.FILLED:
                        self._record_exit(code, "RISK_REDUCE", o.quantity_filled,
                                          o.price, now)
                        tag = "追高题材已减仓" if chasing else "单股超限已降至上限"
                        drifts.append(f"{code} {tag}")
        if not drifts:
            drifts.append("风格正常，未检测到漂移")
        return drifts
