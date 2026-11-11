"""模拟器门面（Simulator）。

粘合五大核心模块与行情/存储，提供高层 API：
  buy / sell / set_day(切换交易日) / snapshot / positions / save / load。
默认行情源由配置 provider 决定（westock 优先，未安装时自动回退 mock）。
"""
import sys
from datetime import date, datetime

from config.settings import SimulatorConfig
from core.account import AccountManager
from core.matching import MatchingEngine
from core.trading_rules import TradingRules
from core.pnl import PnLStatistics
from core.order import Side, OrderType
from market.base import MarketProvider
from market.mock_provider import MockProvider
from storage.local_store import LocalStore


class Simulator:
    def __init__(self, config: SimulatorConfig = None, market: MarketProvider = None,
                 storage: LocalStore = None):
        self.config = config or SimulatorConfig()
        self.market = market or self._build_market()
        self.rules = TradingRules(self.config.rules)
        self.account = AccountManager(self.config)
        self.engine = MatchingEngine(self.account, self.rules, self.market)
        self.storage = storage or LocalStore(self.config.storage_dir)
        self.pnl = PnLStatistics(self.account, self.market, self.storage)

    def _build_market(self) -> MarketProvider:
        if self.config.provider == "mock":
            return MockProvider()
        try:
            from market.westock_provider import WeStockProvider
            return WeStockProvider()
        except Exception:
            print("[warn] westock 未安装，回退 MockProvider（离线演示）", file=sys.stderr)
            return MockProvider()

    # ---------- 交易日 ----------
    def set_day(self, d: date) -> None:
        self.account.advance_trading_day(d)

    # ---------- 交易 ----------
    def _name(self, code: str) -> str:
        try:
            return self.market.get_quote(code).name
        except Exception:
            return code

    def buy(self, code: str, quantity: int, order_type: OrderType = OrderType.MARKET,
            price: float = 0.0, now: datetime = None):
        name = self._name(code)
        return self.engine.submit_order(code, name, Side.BUY, quantity, order_type, price, now)

    def sell(self, code: str, quantity: int, order_type: OrderType = OrderType.MARKET,
             price: float = 0.0, now: datetime = None):
        name = self._name(code)
        return self.engine.submit_order(code, name, Side.SELL, quantity, order_type, price, now)

    # ---------- 查询 ----------
    def snapshot(self) -> dict:
        return self.pnl.snapshot()

    def positions(self) -> list:
        return self.pnl.position_details()

    def report(self) -> dict:
        return {"snapshot": self.snapshot(), "positions": self.positions()}

    # ---------- 持久化 ----------
    def save(self) -> None:
        self.storage.save_account(self.account)
        self.storage.append_new_trades(self.account.trades)
        snap = self.snapshot()
        snap["day"] = (self.account.current_day.isoformat()
                       if self.account.current_day else None)
        self.storage.save_snapshot(snap)
        # 实时行情快照：看板与智能体同源，互不干扰
        self.storage.save_live_prices(self._live_prices())

    def _industry(self, code: str, fallback: dict = None) -> str:
        """解析标的行业（用于看板展示 / 单行业仓位约束）。

        联动优化（异常容错联动）：盘中每 3s 刷新快照时为避免对每只持仓重复
        触达行情源的基本面网络（网络抖动 / 429 级联会拖累看板依赖的实时价获取），
        优先复用上一次有效快照中的 industry；仅当快照缺该字段时才回退 get_fundamentals。
        行业为静态元数据，首次成功后即稳定，无需每次刷新重抓。
        """
        if fallback:
            ind = (fallback.get(code) or {}).get("industry")
            if ind:
                return ind
        try:
            return self.market.get_fundamentals(code).industry or "其他"
        except Exception:
            return "其他"

    def _live_prices(self) -> dict:
        """汇总持仓标的的实时行情（与撮合引擎同源，经校验闸门过滤脏数据）。

        落盘字段（看板展示口径与撮合基准价完全一致）：
          price / prev_close / open / high / low / volume / change_pct /
          ceiling / floor / industry / is_suspended / limit_hit /
          updated_at / source。
        防脏价三级防御 + 停牌检测：
          1) 源正常且校验通过且未停牌 → 写入真实价与全套字段；
          2) 停牌标的 → 写冻结价 + is_suspended=True，禁止展示为可交易价；
          3) 源瞬时失败 / 校验未过 → 沿用上一次有效价（source=fallback），不闪烁成 0；
          4) 既无有效价可回退 → 跳过该标的（看板显式「行情数据未就绪」，不写脏值）。
        顶部写入 _meta（generated_at / source / positions）供看板展示时效戳与数据源状态。
        """
        from market.feed import validate_quote, detect_suspension
        src = "westock" if type(self.market).__name__ == "WeStockProvider" else "mock"
        out: dict = {}
        fallback: dict = {}
        try:
            fallback = self.storage.load_live_prices() if self.storage else {}
        except Exception:
            fallback = {}
        gen_at = datetime.now().isoformat(timespec="seconds")
        for code in self.account.positions:
            try:
                q = self.market.get_quote(code)
                price = float(q.price)
                name = q.name
                prev = float(q.prev_close)
                ceiling = float(getattr(q, "ceiling", 0) or 0)
                floor = float(getattr(q, "floor", 0) or 0)
                o = float(getattr(q, "open", 0) or 0)
                h = float(getattr(q, "high", 0) or 0)
                lo = float(getattr(q, "low", 0) or 0)
                vol = float(getattr(q, "volume", 0) or 0)
                chg = float(getattr(q, "change_pct", 0) or 0)
                if chg == 0 and prev > 0:
                    chg = round((price - prev) / prev * 100, 2)
                suspended = detect_suspension(
                    price, prev, vol, bool(getattr(q, "suspended", False)))
            except Exception:
                # 源瞬时失败：优先沿用上次有效价，避免看板闪烁成 0
                rec = fallback.get(code)
                if rec and rec.get("price"):
                    kept = dict(rec)
                    kept["updated_at"] = datetime.now().isoformat(timespec="seconds")
                    kept["source"] = "fallback"
                    kept["_warn"] = "源失败,沿用上次有效价"
                    out[code] = kept
                continue
            # 停牌：写冻结价 + 停牌标识，不进入可交易价闸门
            if suspended:
                out[code] = {
                    "code": code, "name": name,
                    "price": round(price, 3), "prev_close": round(prev, 3),
                    "open": round(o, 3), "high": round(h, 3), "low": round(lo, 3),
                    "volume": round(vol, 0), "change_pct": round(chg, 2),
                    "ceiling": round(ceiling, 3), "floor": round(floor, 3),
                    "industry": self._industry(code, fallback),
                    "is_suspended": True, "limit_hit": "NONE",
                    "updated_at": datetime.now().isoformat(timespec="seconds"),
                    "source": src,
                }
                continue
            ok, reason = validate_quote(price, prev, ceiling, floor)
            # 脏价二级防御：行情价超出涨跌停区间（源异常/错位）时按无效处理，
            # 沿用上次有效价或跳过落库，杜绝「脏数据落盘」在板块/引擎间扩散。
            if ok and ceiling > 0 and floor > 0 and (
                    price > ceiling + 1e-6 or price < floor - 1e-6):
                ok, reason = False, f"价格超出涨跌停区间[{floor},{ceiling}]"
            if not ok:
                # 脏价拦截：沿用上次有效价并告警，绝不直接落脏值
                rec = fallback.get(code)
                if rec and rec.get("price") and float(rec["price"]) > 0:
                    kept = dict(rec)
                    kept["updated_at"] = datetime.now().isoformat(timespec="seconds")
                    kept["source"] = "fallback"
                    kept["_warn"] = f"校验未过:{reason},沿用上次有效价"
                    out[code] = kept
                else:
                    msg = (f"[live_price] {code} 校验未过({reason})且无有效价可回退，"
                           f"跳过落库（不写脏数据）")
                    try:
                        if self.storage is not None:
                            self.storage.append_risk_event({
                                "ts": datetime.now().isoformat(timespec="seconds"),
                                "type": "LIVE_PRICE_GUARD", "code": code,
                                "qty": 0, "price": 0.0,
                                "day": (self.account.current_day.isoformat()
                                        if self.account.current_day else ""),
                                "pnl_pct": 0.0, "mode": "GUARD",
                            })
                    except Exception:
                        pass
                    print(msg, file=sys.stderr)
                continue
            limit_hit = "NONE"
            if ceiling > 0 and abs(price - ceiling) < 1e-6:
                limit_hit = "UP"
            elif floor > 0 and abs(price - floor) < 1e-6:
                limit_hit = "DOWN"
            out[code] = {
                "code": code, "name": name,
                "price": round(price, 3), "prev_close": round(prev, 3),
                "open": round(o, 3), "high": round(h, 3), "low": round(lo, 3),
                "volume": round(vol, 0), "change_pct": round(chg, 2),
                "ceiling": round(ceiling, 3), "floor": round(floor, 3),
                "industry": self._industry(code, fallback),
                "is_suspended": False, "limit_hit": limit_hit,
                "updated_at": datetime.now().isoformat(timespec="seconds"),
                "source": src,
            }
        out["_meta"] = {
            "generated_at": gen_at, "source": src,
            "positions": len([c for c in out if c != "_meta"]),
        }
        return out

    def refresh_live_prices(self) -> dict:
        """仅刷新持仓实时价快照（看板用），不写账户/成交/快照，避免污染历史。"""
        data = self._live_prices()
        self.storage.save_live_prices(data)
        return data

    def load(self) -> bool:
        acc = self.storage.load_account(self.config)
        if acc is None:
            return False
        from core.order import Trade, Side
        from datetime import date as _date
        for d in self.storage.load_trades():
            td = None
            if d.get("datetime"):
                try:
                    td = datetime.strptime(d["datetime"], "%Y-%m-%d %H:%M:%S")
                except Exception:
                    td = None
            acc.trades.append(Trade(
                trade_id=d["trade_id"], order_id=d["order_id"], code=d["code"],
                name=d["name"], side=Side(d["side"]), price=float(d["price"]),
                quantity=int(d["quantity"]), amount=float(d["amount"]),
                commission=float(d["commission"]), stamp_tax=float(d["stamp_tax"]),
                transfer_fee=float(d["transfer_fee"]), total_fee=float(d["total_fee"]),
                cash_flow=float(d["cash_flow"]),
                realized_pnl=float(d.get("realized_pnl", 0.0)),
                datetime=td,
                trading_day=_date.fromisoformat(d["trading_day"]) if d.get("trading_day") else None,
            ))
        self.account = acc
        self.engine.account = acc
        self.pnl.account = acc
        return True

    def reset(self) -> None:
        self.storage.reset()
        self.account = AccountManager(self.config)
        self.engine.account = self.account
        self.pnl.account = self.account
