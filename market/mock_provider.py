"""离线行情提供器（MockProvider）。

用于单元测试与无网演示：内置样本标的，确定性地生成因子所需的历史与基本面数据。
交易日历采用周一至周五（可子类扩展节假日）。
数据由代码哈希做种子，保证每次运行结果一致、可复现、可断言。
"""
import hashlib
import random
from datetime import date, timedelta
from typing import Dict, Any, List

from market.base import MarketProvider, Quote, Kline, Fundamentals


# 样本实时行情（兼容旧演示 / get_quote 回退）
_SAMPLE: Dict[str, Dict[str, Any]] = {
    "sh600519": {"name": "贵州茅台", "price": 1680.0, "prev_close": 1700.0},
    "sz000001": {"name": "平安银行", "price": 11.20, "prev_close": 11.00},
    "sz300750": {"name": "宁德时代", "price": 185.0, "prev_close": 180.0},
    "sh688981": {"name": "中芯国际", "price": 52.30, "prev_close": 50.00},
    "bj830799": {"name": "艾融软件", "price": 9.50, "prev_close": 10.00},
}

# 选股池演示用的全市场代码（含应通过与应剔除的样例）
UNIVERSE: List[str] = [
    # —— 主板候选（优先指数成分）——
    "sh600519", "sh601398", "sh600036", "sz000333", "sz000651",
    "sh600276", "sz000858", "sh601318", "sz000002", "sh600900",
    # —— 应被基本面/板块剔除的样例 ——
    "sh600868",   # ST
    "sz000980",   # *ST
    "sz000725",   # 近一年净利润亏损
    "sh600516",   # 股权质押率>50%
    "sh601988",   # 近30日日均成交额<500万
    "sh688981",   # 科创板（板块剔除）
    "sz300750",   # 创业板（板块剔除）
    "bj830799",   # 北交所（板块剔除）
]

# 指数成分（演示用；成分内若含科创/创业会在池内二次剔除）
_INDEX_MEMBERS: Dict[str, List[str]] = {
    "sh000300": ["sh600519", "sh601398", "sh600036", "sz000333",
                 "sz000651", "sh600276", "sz000858", "sh601318", "sh600900"],
    "sz399905": ["sz000002", "sz000725", "sh600516", "sh601988", "sh600868"],
}

# 基本面字段覆盖（用于制造明确的通过/剔除样例）
_CURATED: Dict[str, Dict[str, Any]] = {
    "sh600868": {"is_st": True},
    "sz000980": {"is_st": True},
    "sz000725": {"net_profit_ttm": -1_200_000_000.0},
    "sh600516": {"pledge_ratio": 0.62},
    "sh601988": {"avg_turnover_30d": 3_000_000.0},
}

_NAME: Dict[str, str] = {
    "sh600519": "贵州茅台", "sh601398": "工商银行", "sh600036": "招商银行",
    "sz000333": "美的集团", "sz000651": "格力电器", "sh600276": "恒瑞医药",
    "sz000858": "五粮液", "sh601318": "中国平安", "sz000002": "万科A",
    "sh600900": "长江电力", "sh600868": "ST云城", "sz000980": "*ST凯",
    "sz000725": "京东方A", "sh600516": "方大炭素", "sh601988": "中国银行",
}

# 行业归属（用于单行业≤15% 仓位约束演示）
_INDUSTRY: Dict[str, str] = {
    "sh600519": "白酒", "sz000858": "白酒",
    "sh601398": "银行", "sh600036": "银行", "sh601988": "银行",
    "sz000333": "家电", "sz000651": "家电",
    "sh600276": "医药",
    "sh601318": "保险",
    "sz000002": "地产",
    "sh600900": "公用事业",
    "sh600868": "地产", "sz000980": "其他", "sz000725": "电子",
    "sh600516": "材料",
}


class MockProvider(MarketProvider):
    def __init__(self, data: Dict[str, Dict[str, Any]] = None):
        self.data = dict(_SAMPLE) if data is None else data

    # ---------- 工具 ----------
    @staticmethod
    def _seed(code: str) -> int:
        return int(hashlib.md5(code.encode("utf-8")).hexdigest(), 16) & 0xFFFFFFFF

    def _name(self, code: str) -> str:
        if code in self.data:
            return self.data[code].get("name", code)
        return _NAME.get(code, code)

    def set_price(self, code: str, price: float, prev_close: float = None) -> None:
        """测试 / 演示时手动改价。"""
        if code not in self.data:
            self.data[code] = {"name": self._name(code), "price": price,
                               "prev_close": prev_close or price}
        else:
            # 复制内层字典，避免污染类级 _SAMPLE（浅拷贝仅复制顶层）
            self.data[code] = dict(self.data[code])
            self.data[code]["price"] = price
            if prev_close is not None:
                self.data[code]["prev_close"] = prev_close

    # ---------- 实时行情 ----------
    def get_quote(self, code: str) -> Quote:
        def _ohlc(prev: float, price: float):
            """确定性 OHLC：开盘取昨收，最高/最低在开收区间外各扩 0.5%，
            保证 开/高/低/收 四值可区分且稳定（同一 code 每次返回一致），
            避免看板「开盘/最高/最低」三列恒等于现价而误判字段未透传。"""
            open_p = round(prev, 2)
            hi = round(max(price, open_p) * 1.005, 2)
            lo = round(min(price, open_p) * 0.995, 2)
            return open_p, hi, lo

        d = self.data.get(code)
        if d:
            prev = float(d["prev_close"]); price = float(d["price"])
            suspended = bool(d.get("suspended", False))
            open_p, hi, lo = _ohlc(prev, price)
            return Quote(code=code, name=d.get("name", code), price=price,
                         prev_close=prev, open=open_p, high=hi, low=lo,
                         ceiling=round(prev * 1.1, 2), floor=round(prev * 0.9, 2),
                         time="09:30:00",
                         volume=float(d.get("volume", 1_000_000)),
                         change_pct=round((price - prev) / prev * 100, 2) if prev else 0.0,
                         suspended=suspended)
        # 回退：用历史末值构造
        closes = self.get_close_history(code, 30)
        price = closes[-1]; prev = closes[-2]
        open_p, hi, lo = _ohlc(prev, price)
        return Quote(code=code, name=self._name(code), price=price, prev_close=prev,
                     open=open_p, high=hi, low=lo,
                     ceiling=round(prev * 1.1, 2), floor=round(prev * 0.9, 2),
                     time="09:30:00", volume=1_000_000,
                     change_pct=round((price - prev) / prev * 100, 2) if prev else 0.0,
                     suspended=False)

    def set_suspended(self, code: str, flag: bool = True) -> None:
        """测试 / 演示：标记某标的为停牌。"""
        if code not in self.data:
            self.data[code] = {"name": self._name(code), "price": 0.0,
                               "prev_close": 0.0}
        else:
            # 复制内层字典，避免污染类级 _SAMPLE
            self.data[code] = dict(self.data[code])
        self.data[code]["suspended"] = bool(flag)

    def market_breadth(self) -> List[dict]:
        """市场宽度快照：遍历样本数据（与 get_quote 同口径的确定性 OHLC）。"""
        out: List[dict] = []
        for code in self.data:
            q = self.get_quote(code)
            if q.suspended or q.price <= 0 or q.prev_close <= 0:
                continue
            out.append({
                "code": code, "price": q.price, "prev_close": q.prev_close,
                "high": q.high, "low": q.low,
                "ceiling": q.ceiling, "floor": q.floor,
                "change_pct": q.change_pct,
            })
        return out

    # ---------- K线 / 历史 ----------
    def get_kline(self, code: str, period: str = "day", limit: int = 20) -> List[Kline]:
        closes = self.get_close_history(code, limit)
        out = []
        for i, c in enumerate(closes):
            out.append(Kline(
                date=f"2026-09-{i+1:02d}", open=c, close=c,
                high=round(c * 1.01, 2), low=round(c * 0.99, 2),
                volume=1_000_000, amount=c * 1_000_000,
                change_pct=round((c - closes[0]) / closes[0] * 100, 2),
            ))
        return out

    def get_close_history(self, code: str, days: int = 126) -> List[float]:
        rng = random.Random(self._seed(code + "h"))
        base = rng.uniform(8.0, 60.0)
        vol = rng.uniform(0.008, 0.030)          # 日波动率（决定低波动因子）
        trend = rng.uniform(-0.0006, 0.0012)     # 轻微漂移（决定动量）
        p = base
        closes = []
        for _ in range(days):
            p *= (1 + trend + rng.gauss(0.0, vol))
            closes.append(round(max(p, 1.0), 3))
        return closes

    # ---------- 基本面 ----------
    def get_fundamentals(self, code: str) -> Fundamentals:
        rng = random.Random(self._seed(code))
        roe_3y = [round(rng.uniform(0.10, 0.22), 3) for _ in range(3)]
        net_profit = rng.uniform(1e8, 5e10)
        ocf = net_profit * rng.uniform(0.85, 1.25)
        gm = [round(rng.uniform(0.25, 0.55), 3) for _ in range(3)]
        pe = round(rng.uniform(8.0, 40.0), 2)
        pb = round(rng.uniform(1.0, 6.0), 2)
        pe_pct = round(rng.uniform(0.05, 0.95), 3)
        pb_pct = round(rng.uniform(0.05, 0.95), 3)
        pledge = round(rng.uniform(0.0, 0.35), 3)
        turnover = round(rng.uniform(8e6, 2e9), 2)
        fund = Fundamentals(
            code=code, name=self._name(code), industry=_INDUSTRY.get(code, "其他"),
            roe_3y=roe_3y, ocf=ocf, net_profit=net_profit,
            net_profit_ttm=net_profit, gross_margin_series=gm,
            pe=pe, pb=pb, pe_pct_3y=pe_pct, pb_pct_3y=pb_pct,
            pledge_ratio=pledge, avg_turnover_30d=turnover,
        )
        # 覆盖明确剔除样例
        for k, v in _CURATED.get(code, {}).items():
            setattr(fund, k, v)
        return fund

    # ---------- 指数 / 全市场 ----------
    def get_index_constituents(self, index_code: str) -> List[str]:
        return list(_INDEX_MEMBERS.get(index_code, []))

    def get_universe(self) -> List[str]:
        return list(UNIVERSE)

    # ---------- 交易日历 ----------
    def is_trading_day(self, d: date) -> bool:
        return d.weekday() < 5

    def next_trading_day(self, d: date) -> date:
        nd = d + timedelta(days=1)
        while not self.is_trading_day(nd):
            nd += timedelta(days=1)
        return nd
