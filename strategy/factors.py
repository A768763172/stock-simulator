"""多因子选股模型（要求 2）。

四因子按权重排序（默认）：质量 35% / 估值 25% / 低波动 25% / 动量 15%。
各因子输出归一化子分 [0,1]，加权求和得综合得分。
纯函数实现，便于单元测试与回测复用。
"""
import math
from typing import Dict, List

from config.strategy import StrategyConfig
from market.base import MarketProvider, Fundamentals


# ---------- 序列统计工具 ----------
def daily_returns(closes: List[float]) -> List[float]:
    out = []
    for i in range(1, len(closes)):
        if closes[i - 1] > 0:
            out.append(closes[i] / closes[i - 1] - 1.0)
    return out


def annualized_vol(closes: List[float]) -> float:
    """日收益率年化波动率（*sqrt(242)）。"""
    r = daily_returns(closes)
    if len(r) < 2:
        return 0.0
    mean = sum(r) / len(r)
    var = sum((x - mean) ** 2 for x in r) / (len(r) - 1)
    return math.sqrt(var) * math.sqrt(242.0)


def max_drawdown(closes: List[float]) -> float:
    """区间最大回撤（正值表示回撤幅度，0 表示无回撤）。"""
    peak = 0.0
    mdd = 0.0
    for p in closes:
        if p > peak:
            peak = p
        if peak > 0:
            mdd = max(mdd, (peak - p) / peak)
    return mdd


# ---------- 单因子打分 ----------
def quality_score(fund: Fundamentals, cfg: StrategyConfig) -> float:
    """质量因子：ROE 连续3年>10% + 经营现金流覆盖净利润 + 毛利率稳定。"""
    roe_ok = all(r > cfg.min_roe_3y for r in fund.roe_3y) if fund.roe_3y else False
    ocf_ok = (not cfg.ocf_cover_net_profit) or (
        fund.net_profit > 0 and fund.ocf >= fund.net_profit
    )
    gm = fund.gross_margin_series
    margin_ok = True
    if cfg.margin_stable_band and len(gm) >= 2:
        mu = sum(gm) / len(gm)
        margin_ok = mu > 0 and (max(gm) - min(gm)) / mu < cfg.margin_stable_band
    s_roe = 1.0 if roe_ok else 0.0
    s_ocf = 1.0 if ocf_ok else 0.0
    s_margin = 1.0 if margin_ok else 0.0
    return 0.5 * s_roe + 0.25 * s_ocf + 0.25 * s_margin


def valuation_score(fund: Fundamentals, cfg: StrategyConfig) -> float:
    """估值因子：PE/PB 近3年分位越低越好，≤阈值得满分。"""
    worst = max(fund.pe_pct_3y, fund.pb_pct_3y)
    if worst <= cfg.valuation_pct_max:
        return 1.0
    if cfg.valuation_pct_max >= 1.0:
        return 0.0
    return max(0.0, 1.0 - (worst - cfg.valuation_pct_max) /
               (1.0 - cfg.valuation_pct_max))


def low_vol_score(vol: float, vol_threshold: float, mdd: float,
                  cfg: StrategyConfig) -> float:
    """低波动因子：波动率越低越好；阶段最大回撤须 <15%。"""
    if vol_threshold <= 0:
        s_vol = 1.0 if vol <= 0 else 0.5
    else:
        s_vol = max(0.0, 1.0 - vol / vol_threshold)  # 低于阈值=高分
    s_dd = 1.0 if mdd < cfg.max_drawdown_stage else 0.0
    return 0.7 * s_vol + 0.3 * s_dd


def momentum_score(ret: float, idx_ret: float, cfg: StrategyConfig) -> float:
    """动量因子：近1月小幅跑赢指数且不追高。"""
    excess = ret - idx_ret
    if ret > cfg.momentum_no_chase:
        excess = min(excess, 0.0)          # 追高（月涨幅过大）→ 不给分
    if excess <= cfg.momentum_excess_min:
        return 0.0
    # 超额收益在 (0, no_chase] 内线性映射到 (0,1]
    return min(1.0, excess / cfg.momentum_no_chase)


# ---------- ETF 识别 ----------
def is_etf_code(code: str, cfg: StrategyConfig) -> bool:
    """ETF 识别（与 StockPool.is_etf 共用配置前缀）。"""
    if not getattr(cfg, "include_etfs", False):
        return False
    c = (code or "").lower()
    return any(c.startswith(b) for b in cfg.etf_boards)


# ---------- 综合 ----------
def compute_factor_scores(code: str, market: MarketProvider, cfg: StrategyConfig,
                          vol_universe: Dict[str, float] = None) -> dict:
    """计算单只标的的四因子子分与加权综合分。

    vol_universe: {code: 年化波动率}，用于计算全市场波动率分位阈值。
    返回 {quality, valuation, low_vol, momentum, score, vol, mdd, ret}。

    ETF 分支：ETF 跟踪指数、无个股财报，质量/估值因子不适用，
    给中性分 0.5；真实区分度来自低波动与动量（契合 ETF 复利/低波特性）。
    """
    fund = market.get_fundamentals(code)
    closes = market.get_close_history(code, 126)
    vol = annualized_vol(closes)
    mdd = max_drawdown(closes)

    # 动量需指数基准：取优先指数之一的近1月收益
    idx_ret = 0.0
    if cfg.priority_indexes:
        try:
            idx_closes = market.get_close_history(cfg.priority_indexes[0], 21)
            if len(idx_closes) >= 2:
                idx_ret = idx_closes[-1] / idx_closes[0] - 1.0
        except Exception:
            idx_ret = 0.0
    ret = closes[-1] / closes[0] - 1.0 if len(closes) >= 2 else 0.0

    # 波动率分位阈值：全市场前 40%（即 40 分位处的波动率）
    vol_threshold = None
    if vol_universe:
        ordered = sorted(vol_universe.values())
        k = max(0, int(len(ordered) * cfg.vol_quantile_max) - 1)
        vol_threshold = ordered[k] if ordered else vol
    else:
        vol_threshold = vol  # 无基准时退化为自身

    sl = low_vol_score(vol, vol_threshold or vol, mdd, cfg)
    sm = momentum_score(ret, idx_ret, cfg)

    if is_etf_code(code, cfg):
        # ETF：质量/估值不适用 → 中性分，避免被 0 分永久压制
        sq = 0.5
        sv = 0.5
    else:
        sq = quality_score(fund, cfg)
        sv = valuation_score(fund, cfg)

    w = cfg.factor_weights
    score = (w["quality"] * sq + w["valuation"] * sv +
             w["low_vol"] * sl + w["momentum"] * sm)

    return {
        "quality": round(sq, 4), "valuation": round(sv, 4),
        "low_vol": round(sl, 4), "momentum": round(sm, 4),
        "score": round(score, 4),
        "vol": round(vol, 4), "mdd": round(mdd, 4), "ret": round(ret, 4),
    }
