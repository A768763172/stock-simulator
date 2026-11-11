"""量化绩效指标（纯函数，供回测优化与学习进度评估）。

优化目标统一为夏普比率最大化，约束优先级：① 控制回撤 ② 提升胜率 ③ 提升收益。
所有函数无副作用、可单测。
"""
import math
from typing import List, Sequence


def daily_returns(equity: Sequence[float]) -> List[float]:
    """由资产曲线求日收益率序列。"""
    out = []
    for i in range(1, len(equity)):
        if equity[i - 1] > 0:
            out.append(equity[i] / equity[i - 1] - 1.0)
    return out


def sharpe_ratio(returns: Sequence[float], periods: int = 242,
                 rf: float = 0.0) -> float:
    """年化夏普比率（无风险利率默认 0；periods=242 为 A股年交易日）。"""
    r = [x - rf for x in returns]
    n = len(r)
    if n < 2:
        return 0.0
    mean = sum(r) / n
    var = sum((x - mean) ** 2 for x in r) / (n - 1)
    if var <= 0:
        return 0.0
    return mean / math.sqrt(var) * math.sqrt(periods)


def max_drawdown(equity: Sequence[float]) -> float:
    """区间最大回撤（正值表示回撤幅度，0 表示无回撤）。"""
    peak = 0.0
    mdd = 0.0
    for p in equity:
        if p > peak:
            peak = p
        if peak > 0:
            mdd = max(mdd, (peak - p) / peak)
    return mdd


def cumulative_return(equity: Sequence[float], initial: float = None) -> float:
    """累计收益率（相对初始资金或曲线首值）。"""
    if not equity:
        return 0.0
    base = initial if initial and initial > 0 else equity[0]
    if base <= 0:
        return 0.0
    return equity[-1] / base - 1.0


def win_rate(pnls: Sequence[float]) -> float:
    """胜率：盈利笔数 / 总笔数。"""
    pnls = [p for p in pnls if p is not None]
    if not pnls:
        return 0.0
    wins = sum(1 for p in pnls if p > 0)
    return wins / len(pnls)


def avg_return(pnls: Sequence[float]) -> float:
    pnls = [p for p in pnls if p is not None]
    if not pnls:
        return 0.0
    return sum(pnls) / len(pnls)


def objective_score(sharpe: float, mdd: float, wr: float, ret: float) -> float:
    """统一目标函数（越大越好）：夏普优先，其次压回撤，再胜率，最后收益。

    采用字典序思想：以夏普为主项，回撤/胜率/收益以惩罚-奖励项加权叠加，
    回撤惩罚系数最高，确保「优先控制回撤」。
    """
    return sharpe - 3.0 * mdd + 0.5 * wr + 0.3 * ret


def rank_key(sharpe: float, mdd: float, wr: float, ret: float):
    """字典序排序键：先比夏普（降），再比回撤（升），再比胜率（降），再比收益（降）。

    返回元组供 max()/sorted() 使用（回撤取负以实现「越小越好」）。
    """
    return (round(sharpe, 6), -round(mdd, 6), round(wr, 6), round(ret, 6))
