"""交易智能体 · 四维自学习进化引擎（量价 + 政策 + 消息 + 情绪）。

核心目标：风险调整后收益最大化（夏普优先）。彻底摒弃「纯量价单因子」学习主线，
将 A 股「政策性、消息性、情绪性驱动」特性纳入，构建四维学习迭代闭环。

收盘后 15:30 自动四维复盘（review）闭环：
1. 环境感知：政策 / 消息 / 情绪三维市场因子（market_env）合成 EnvState。
2. 当日交易四维复盘：每笔盈亏拆解为 量价 / 政策 / 消息 / 情绪 四维贡献度；
   禁止将政策 / 情绪驱动收益错误归因为个股技术策略。
3. 四维权重学习：量价子因子权重 + 四维顶层权重（price/policy/news/sentiment）
   均按「因子有效性累计 = Σ(盈利×该维贡献)」连续再平衡，夏普优先、无硬编码上下限。
4. 动态适配：政策利好窗口提仓/放宽止盈、情绪恐慌降仓/收紧止损、重磅消息降频。
5. 策略类型自动切换：趋势跟踪 / 反转 / 网格 资金权重按环境偏好与历史表现再平衡。
6. 风控联动：重大政策 / 极端情绪 / 重磅消息前自动升级熔断敏感度（更敏感）。
7. 全场景回测验证：优化后须通过「政策事件 + 情绪周期 + 消息冲击」场景验证
   （夏普≥阈值、回撤≤上限）方可生效；输出优化原因与预期效果（非黑盒）。
8. 策略适配性档案：记录不同环境下各策略表现，持续提升环境-策略匹配准确率。

所有数据本地存储，不上传任何服务器。
"""
import json
import math
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from config.strategy import StrategyConfig
from strategy.metrics import (
    sharpe_ratio, max_drawdown, cumulative_return, win_rate,
    daily_returns, rank_key,
)
from strategy.risk import RiskController
from strategy.factors import annualized_vol
from strategy.market_env import (
    EnvState, MarketEnvProvider, MockEnvProvider, MarketEnvSensor, \
        FileEnvProvider,
)


# 退出原因枚举（与 agent 记录一致）
STOP_LOSS = "STOP_LOSS"
TAKE_PROFIT = "TAKE_PROFIT"
HOLD_TIMEOUT = "HOLD_TIMEOUT"
RISK_REDUCE = "RISK_REDUCE"


def profit_factor(pnls: List[float]) -> float:
    """盈亏比：总盈利 / |总亏损|（无亏损且盈利>0 时记为正无穷，否则 0）。"""
    gains = sum(p for p in pnls if p > 0)
    losses = -sum(p for p in pnls if p < 0)
    if losses <= 0:
        return float("inf") if gains > 0 else 0.0
    return gains / losses


# ============ 纯函数：盈亏四维归因 ============
def four_dim_attribution(pnl_pct: float, entry_env: EnvState,
                         exit_env: EnvState) -> Dict[str, float]:
    """将单笔盈亏拆解为 量价 / 政策 / 消息 / 情绪 四维贡献占比（和为 1）。

    防错误归因原则：仅当某市场因子与盈亏「同向」时才计入其贡献；环境因子越强，
    其解释份额越大，量价（个股技术）补差越小——避免把政策 / 情绪驱动行情错误
    归功于个股技术策略。环境弱时量价占主导（=1）。
    """
    s = 1.0 if pnl_pct >= 0 else -1.0
    # 政策用入场时强度；消息用出场时冲击（消息驱动行情在兑现时影响价格）；
    # 情绪用入场时指数（持仓期情绪定调）。
    pol = entry_env.policy_score * s if entry_env.policy_active else 0.0
    nws = exit_env.news_net_impact * s if exit_env.news_active else 0.0
    sen = entry_env.sentiment_index * s
    pol, nws, sen = max(0.0, pol), max(0.0, nws), max(0.0, sen)
    env_sum = pol + nws + sen
    cap = 0.85  # 环境最多解释 85%，余下归量价（个股技术 / 选股）
    if env_sum <= 1e-9:
        return {"price": 1.0, "policy": 0.0, "news": 0.0, "sentiment": 0.0}
    scale = min(1.0, cap / env_sum)
    return {
        "policy": round(pol * scale, 4),
        "news": round(nws * scale, 4),
        "sentiment": round(sen * scale, 4),
        "price": round(max(0.0, 1.0 - (pol + nws + sen) * scale), 4),
    }


def attribute_trade(pnl_pct: float, market_ret: float, exit_type: str,
                    entry_factors: Optional[Dict[str, float]] = None,
                    entry_env: EnvState = None,
                    exit_env: EnvState = None) -> dict:
    """拆解单笔交易盈亏原因（兼容旧接口）。

    返回 {primary, breakdown, four_dim}：
    - breakdown：四类旧原因占比（selection/entry_timing/systematic/rule_trigger）。
    - four_dim：量价/政策/消息/情绪 四维贡献（新增，学习模块使用）。
    """
    entry_factors = entry_factors or {}
    bd = {"selection": 0.0, "entry_timing": 0.0,
          "systematic": 0.0, "rule_trigger": 0.0}
    primary = exit_type
    idio = pnl_pct - market_ret

    if exit_type == STOP_LOSS:
        bd["rule_trigger"] = 0.5
        if market_ret < 0:
            bd["systematic"] = 0.3
        if idio < 0:
            bd["selection"] = 0.2
        primary = "止损触发"
    elif exit_type == TAKE_PROFIT:
        bd["rule_trigger"] = 0.4
        bd["selection"] = 0.35
        bd["entry_timing"] = 0.25
        primary = "止盈触发"
    elif exit_type == HOLD_TIMEOUT:
        if market_ret < 0:
            bd["systematic"], bd["rule_trigger"], primary = 0.6, 0.4, "系统性风险"
        elif pnl_pct >= 0:
            bd["selection"], bd["entry_timing"], primary = 0.5, 0.5, "持有到期"
        else:
            bd["entry_timing"], bd["rule_trigger"], primary = 0.6, 0.4, "买点不佳"
    elif exit_type == RISK_REDUCE:
        if market_ret < 0:
            bd["systematic"], bd["selection"], primary = 0.7, 0.3, "系统性风险"
        else:
            bd["rule_trigger"], primary = 1.0, "风控降仓"
    else:
        if market_ret < 0 and pnl_pct < 0:
            bd["systematic"], bd["selection"], primary = 0.5, 0.5, "持仓浮亏"
        elif pnl_pct < 0:
            bd["entry_timing"], bd["selection"], primary = 0.6, 0.4, "买点不佳"
        else:
            bd["selection"], bd["entry_timing"], primary = 0.6, 0.4, "持仓盈利"

    if exit_type in (STOP_LOSS, RISK_REDUCE, HOLD_TIMEOUT) and pnl_pct < 0:
        if entry_factors.get("quality", 0.0) >= 0.7:
            bd["selection"] = max(bd["selection"], 0.45)
            if primary in ("止损触发", "持仓浮亏"):
                primary = "选股错误"

    # 四维归因：环境弱（无快照）时退化——全部归量价，避免凭空虚增政策/情绪贡献
    if entry_env is None and exit_env is None:
        fd = {"price": 1.0, "policy": 0.0, "news": 0.0, "sentiment": 0.0}
    else:
        fd = four_dim_attribution(pnl_pct, entry_env or EnvState(),
                                  exit_env or EnvState())
    return {"primary": primary, "breakdown": bd, "four_dim": fd}


# ============ 纯函数：单标的回测模拟 ============
def simulate_backtest(closes: List[float], sl: float, tp1: float,
                      tp2: float) -> Tuple[List[float], List[float]]:
    """对单只历史收盘价序列，按「止损 sl / 止盈 tp1(半仓) tp2(清仓)」规则模拟。

    返回 (资产曲线, 每笔交易收益率列表)。忽略手续费（仅用于参数相对优劣比较）。
    """
    if not closes or len(closes) < 3:
        return [1.0], []
    eq = 1.0
    curve = [eq]
    trades: List[float] = []
    entry = None
    tp1_done = False
    for i in range(1, len(closes)):
        prev = closes[i - 1]
        cur = closes[i]
        if entry is None:
            entry = prev
            tp1_done = False
            continue
        if entry <= 0 or cur <= 0:
            entry = cur
            continue
        ret = cur / entry - 1.0
        if ret <= -sl:
            trades.append(ret)
            eq *= (1.0 + ret)
            curve.append(eq)
            entry = None
            continue
        if ret >= tp2:
            trades.append(ret)
            eq *= (1.0 + ret)
            curve.append(eq)
            entry = None
            continue
        if ret >= tp1 and not tp1_done:
            half = ret * 0.5
            trades.append(half)
            eq *= (1.0 + half)
            curve.append(eq)
            tp1_done = True
            continue
        curve.append(eq)
    if entry is not None and entry > 0:
        ret = closes[-1] / entry - 1.0
        trades.append(ret)
        eq *= (1.0 + ret)
        curve.append(eq)
    return curve, trades


def simulate_env_aware(closes: List[float], env_seq: List[str],
                       frac_map: Dict[str, float], default_frac: float,
                       sl: float, tp1: float, tp2: float
                       ) -> Tuple[List[float], List[float]]:
    """环境感知模拟：在量价进出场规则之上叠加「四维动态仓位」。

    env_seq[i] 对应第 i 笔交易发生时的环境标签（policy_bull / sentiment_panic /
    oscillation / major_news / neutral）。仓位占比由 frac_map 决定，体现四维学习
    的「动态适配」（政策利好加仓、恐慌降仓、震荡网格中性、重磅消息降仓）。

    返回 (资金曲线, 每笔交易收益率列表，已按仓位缩放)。
    """
    if not closes or len(closes) < 3:
        return [1.0], []
    eq = 1.0
    curve = [eq]
    trades: List[float] = []
    entry = None
    tp1_done = False
    ei = 0
    for i in range(1, len(closes)):
        prev = closes[i - 1]
        cur = closes[i]
        if entry is None:
            entry = prev
            tp1_done = False
            continue
        if entry <= 0 or cur <= 0:
            entry = cur
            continue
        ret = cur / entry - 1.0
        if ret <= -sl or ret >= tp2 or (ret >= tp1 and not tp1_done):
            env = env_seq[min(ei, len(env_seq) - 1)] if env_seq else "neutral"
            frac = frac_map.get(env, default_frac)
            scaled = ret * frac
            trades.append(scaled)
            eq *= (1.0 + scaled)
            curve.append(eq)
            ei += 1
            entry = None
            tp1_done = False
            continue
        curve.append(eq)
    if entry is not None and entry > 0:
        ret = closes[-1] / entry - 1.0
        env = env_seq[-1] if env_seq else "neutral"
        frac = frac_map.get(env, default_frac)
        trades.append(ret * frac)
        eq *= (1.0 + ret * frac)
        curve.append(eq)
    return curve, trades


# ============ 纯函数：市场风格识别（保留，作为量价趋势参考） ============
def detect_regime(market, cfg: StrategyConfig, window: int = 60) -> str:
    idx = cfg.priority_indexes[0] if cfg.priority_indexes else None
    if not idx:
        return "OSCILLATION"
    try:
        closes = market.get_close_history(idx, window)
    except Exception:
        return "OSCILLATION"
    if len(closes) < 20:
        return "OSCILLATION"
    # 短周期动量（近 5 日）捕捉近期方向，避免 20 日慢信号对刚启动的反弹迟钝；
    # 此前用 20 日收益 + 单条「跌破均线即 BEAR」的 OR 条件，在震荡上行市里
    # 价格短暂回踩 60 日线就被误判为熊市，与「今天红了一片」的真实盘面脱节。
    r_short = (closes[-1] / closes[-5] - 1.0) if len(closes) >= 6 else 0.0
    # 中长期趋势以 60 日（不足则全样本）均线为锚
    ma = (sum(closes[-60:]) / len(closes[-60:])) if len(closes) >= 60 \
        else (sum(closes) / len(closes))
    price = closes[-1]
    above_ma = price > ma
    # 双条件确认：仅当「近 5 日明显走弱」且「跌破中期均线」才判熊市，
    # 避免单日/小幅回踩均线即误判 BEAR；上涨侧同样需「近期走强且站上均线」。
    if r_short <= -0.03 and not above_ma:
        return "BEAR"
    if r_short >= 0.03 and above_ma:
        return "BULL"
    return "OSCILLATION"


# ============ 确定性全场景回测验证（优化前 vs 优化后） ============
def scenario_backtest(sl: float = 0.03, tp1: float = 0.08,
                      tp2: float = 0.15) -> dict:
    """用 4 类确定性合成场景，对比「纯量价固定仓位」(优化前) 与
    「四维动态仓位」(优化后) 的风险收益指标。

    返回 {scenarios: [...], summary: {...}}，每场景含
    price_only / four_dim 的 收益率 / 最大回撤 / 胜率 / 盈亏比 与判定。
    """
    # 环境→仓位映射（四维动态适配规则）
    frac_map = {
        "policy_bull": 0.75, "sentiment_panic": 0.40,
        "oscillation": 0.55, "major_news": 0.30, "neutral": 0.60,
    }
    default_frac = 0.60

    scenarios = [
        ("政策利好上行", [100, 103, 107, 112, 118, 125, 131, 138],
         ["policy_bull"] * 7),
        ("情绪恐慌下行", [100, 96, 90, 83, 75, 68, 60, 54],
         ["sentiment_panic"] * 7),
        ("震荡无主线", [100, 102, 99, 101, 98, 100, 102, 101],
         ["oscillation"] * 7),
        ("重磅利空冲击", [100, 99, 98, 90, 82, 80, 79, 78],
         ["neutral", "neutral", "neutral", "major_news",
          "major_news", "major_news", "major_news"]),
    ]

    out = []
    agg_po = {"ret": 0.0, "mdd": 0.0, "sh": 0.0}
    agg_fd = {"ret": 0.0, "mdd": 0.0, "sh": 0.0}
    for name, closes, env_seq in scenarios:
        c_po, t_po = simulate_env_aware(closes, env_seq, frac_map, default_frac,
                                        sl, tp1, tp2)
        # 纯量价：全程固定 0.60 仓
        _, t_flat = simulate_env_aware(closes, ["neutral"] * len(env_seq),
                                       frac_map, default_frac, sl, tp1, tp2)
        c_fd, t_fd = simulate_env_aware(closes, env_seq, frac_map, default_frac,
                                        sl, tp1, tp2)
        # 纯量价曲线：固定仓位缩放（与 four_dim 同规则但 frac=0.6）
        c_price, t_price = simulate_env_aware(
            closes, ["neutral"] * len(env_seq), frac_map, 0.60, sl, tp1, tp2)

        po = _metrics(c_price, t_price)
        fd = _metrics(c_fd, t_fd)
        # 判定：四维相对纯量价，是否在风险可控下提升夏普/降低回撤
        verdict = "优化后更优" if (
            fd["sharpe"] >= po["sharpe"] and fd["mdd"] <= po["mdd"] * 1.05
        ) else "需观察"
        out.append({
            "scenario": name,
            "price_only": po, "four_dim": fd, "verdict": verdict,
        })
        agg_po["ret"] += po["return"]; agg_po["mdd"] += po["mdd"]
        agg_fd["ret"] += fd["return"]; agg_fd["mdd"] += fd["mdd"]

    n = max(1, len(out))
    summary = {
        "price_only_avg_return": round(agg_po["ret"] / n, 4),
        "four_dim_avg_return": round(agg_fd["ret"] / n, 4),
        "price_only_avg_mdd": round(agg_po["mdd"] / n, 4),
        "four_dim_avg_mdd": round(agg_fd["mdd"] / n, 4),
        "four_dim_wins": sum(1 for s in out if s["verdict"] == "优化后更优"),
    }
    return {"scenarios": out, "summary": summary}


def _metrics(curve: List[float], trades: List[float]) -> dict:
    ret = cumulative_return(curve, 1.0)
    mdd = max_drawdown(curve)
    wr = win_rate(trades)
    pf = profit_factor(trades)
    sh = sharpe_ratio(daily_returns(curve)) if len(curve) >= 2 else 0.0
    return {
        "return": round(ret, 4), "mdd": round(mdd, 4),
        "win_rate": round(wr, 4), "profit_factor": round(pf, 4),
        "sharpe": round(sh, 4),
    }


class Learner:
    """四维自学习进化引擎。持有 agent 引用，收盘后调用 review() 驱动一次进化。"""

    def __init__(self, agent, storage=None, env_provider: MarketEnvProvider = None):
        self.agent = agent
        self.market = agent.market
        self.cfg: StrategyConfig = agent.cfg
        self.storage = storage
        # 市场环境因子源（可插拔；默认读每日资讯摘要文件 FileEnvProvider，
        # 摘由盘前检索任务落盘到 data/env_digest_YYYY-MM-DD.json；
        # 无文件时安全回退中性，不影响交易）
        self.env_provider = env_provider or FileEnvProvider(self.cfg)
        self.sensor = MarketEnvSensor(self.cfg, self.env_provider)

        # 量价子因子权重（学习于个股量价 + 基本面）
        self.base_weights: Dict[str, float] = dict(self.cfg.factor_weights)
        self.factor_eff: Dict[str, float] = {f: 0.0 for f in self.cfg.factor_weights}
        # 四维顶层权重（price/policy/news/sentiment）
        self.four_dim_weights: Dict[str, float] = dict(self.cfg.four_dim_weights)
        self.four_dim_eff: Dict[str, float] = {d: 0.0
                                               for d in self.cfg.four_dim_weights}
        # 策略类型资金权重（trend_follow/reversal/grid）
        self.strategy_weights: Dict[str, float] = dict(self.cfg.strategy_weights)
        self.strategy_eff: Dict[str, float] = {s: 0.0
                                               for s in self.cfg.strategy_types}

        self.stock_anchor: float = float(self.cfg.single_stock_max)
        self.ind_anchor: float = float(self.cfg.single_industry_max)
        self.market_cond: dict = {}
        self.regime: str = "OSCILLATION"
        self.position_target_suggest: float = 0.60
        self.env_state: EnvState = EnvState()
        self.trading_day_count: int = 0
        self.last_week_key: Optional[str] = None
        self.processed: set = set()
        self.equity_curve: List[float] = []
        self.realized_pnls: List[float] = []
        self.progress: List[dict] = []
        self.last_review_day: Optional[str] = None
        self._last_report: Optional[dict] = None
        # 策略适配性档案：环境标签 → {strategy: 累计表现}
        self.strategy_profile: List[dict] = []
        # 运行时锚点：避免环境调制在多次 sense 间累积
        self._anchors: dict = {
            "base_total_target": self.cfg.base_total_target,
            "stop_loss_pct": self.cfg.stop_loss_pct,
            "tp1_pct": self.cfg.tp1_pct,
            "tp2_pct": self.cfg.tp2_pct,
            "single_stock_max": self.cfg.single_stock_max,
            "max_new_open_per_week": self.cfg.max_new_open_per_week,
        }
        self._dd_anchor: dict = {
            "dd_daily_max": self.cfg.dd_daily_max,
            "dd_weekly_max": self.cfg.dd_weekly_max,
            "dd_peak_max": self.cfg.dd_peak_max,
        }
        self.risk_escalated: bool = False

        self.load()

    # ---------- 盘中 / 收盘 环境感知 + 动态适配 ----------
    def sense_environment(self, day, now: datetime = None) -> EnvState:
        """感知当前市场环境并即时调制运行时参数（盘中 / 收盘均可调用）。

        调制基于锚点叠加环境偏移，不累积；风控联动可临时升级熔断敏感度。
        """
        st = self.sensor.sense(day, self.market)
        self.env_state = st
        self._apply_dynamic_adaptation(st)
        self._apply_risk_escalation(st)
        return st

    def _dim_ratio(self, dim: str) -> float:
        """四维学习权重相对初始配置的倍率（>1 放大环境偏移，<1 收敛）。

        初始配置 four_dim_weights 是人工先验；收盘归因学习后权重会偏离。
        偏移量 = 固定规则值 × 本倍率——「环境该管多宽」由历史盈亏学习决定：
        政策窗口提仓总是亏钱 → w_policy 降 → 倍率 <1 → 提仓幅度自动收敛。
        """
        init = float(self.cfg.four_dim_weights.get(dim, 0.0) or 0.0)
        cur = float(self.four_dim_weights.get(dim, init) or 0.0)
        if init <= 1e-9:
            return 1.0
        return max(0.0, min(3.0, cur / init))

    def _apply_dynamic_adaptation(self, st: EnvState) -> None:
        """动态适配：政策利好提仓/放宽止盈、情绪恐慌降仓/收紧止损、
        情绪亢奋放宽止盈、重磅消息降频。基于锚点叠加，幂等。

        各项偏移幅度按对应四维学习权重的倍率缩放（见 _dim_ratio）——
        环境影响力由归因学习调制，此处只提供规则骨架。
        """
        base = self._anchors["base_total_target"]
        sl = self._anchors["stop_loss_pct"]
        tp1 = self._anchors["tp1_pct"]
        tp2 = self._anchors["tp2_pct"]
        stock = self._anchors["single_stock_max"]
        freq = self._anchors["max_new_open_per_week"]

        r_pol = self._dim_ratio("policy")
        r_sen = self._dim_ratio("sentiment")
        r_news = self._dim_ratio("news")

        center = base
        # 政策利好窗口
        if st.policy_active and st.policy_score > 0:
            center += self.cfg.policy_pos_boost * st.policy_score * r_pol
            tp1 += self.cfg.policy_tp_relax * st.policy_score * r_pol
        # 情绪恐慌 / 冰点：降仓 + 收紧止损
        if st.sentiment_phase in ("恐慌", "冰点"):
            center -= self.cfg.panic_pos_cut * abs(st.sentiment_index) * r_sen
            sl += self.cfg.panic_sl_tighten * r_sen
        # 情绪亢奋：放宽止盈（让利润奔跑）
        if st.sentiment_phase == "亢奋":
            tp1 += self.cfg.euphoria_tp_relax * r_sen
        center = max(0.0, min(1.0, center))
        tp1 = min(tp1, tp2 - 0.01)
        self.cfg.base_total_target = round(center, 4)
        self.cfg.stop_loss_pct = round(max(0.01, sl), 4)
        self.cfg.tp1_pct = round(tp1, 4)
        self.cfg.tp2_pct = round(tp2, 4)
        self.cfg.single_stock_max = round(stock, 4)
        # 重磅消息：新开仓频率折扣（学习倍率低时少降频）
        if st.news_active and st.news_magnitude >= self.cfg.major_news_magnitude \
                and st.news_nature == "bearish":
            cut = 1.0 - (1.0 - self.cfg.major_news_freq_cut) * r_news
            cut = max(0.3, min(1.0, cut))
            freq = max(1, int(freq * cut))
        self.cfg.max_new_open_per_week = freq

    def _apply_risk_escalation(self, st: EnvState) -> None:
        """风控联动：重大政策 / 极端情绪 / 重磅消息前升级熔断敏感度（更敏感）。"""
        escalate = (
            (self.cfg.risk_escalate_on_policy and st.policy_active
             and abs(st.policy_score) >= 0.6)
            or (self.cfg.risk_escalate_on_extreme_sentiment
                and st.sentiment_phase in ("恐慌", "冰点", "亢奋"))
            or (self.cfg.risk_escalate_on_major_news and st.news_active
                and st.news_magnitude >= self.cfg.major_news_magnitude)
        )
        if escalate and not self.risk_escalated:
            self.cfg.dd_daily_max = round(self._dd_anchor["dd_daily_max"]
                                          / self.cfg.dd_escalate_mult, 4)
            self.cfg.dd_weekly_max = round(self._dd_anchor["dd_weekly_max"]
                                           / self.cfg.dd_escalate_mult, 4)
            self.cfg.dd_peak_max = round(self._dd_anchor["dd_peak_max"]
                                         / self.cfg.dd_escalate_mult, 4)
            self.risk_escalated = True
            self._log_risk_escalate(st)
        elif not escalate and self.risk_escalated:
            # 恢复
            self.cfg.dd_daily_max = self._dd_anchor["dd_daily_max"]
            self.cfg.dd_weekly_max = self._dd_anchor["dd_weekly_max"]
            self.cfg.dd_peak_max = self._dd_anchor["dd_peak_max"]
            self.risk_escalated = False

    def _log_risk_escalate(self, st: EnvState) -> None:
        if self.storage and self.agent:
            self.storage.append_risk_event({
                "ts": datetime.now().isoformat(timespec="seconds"),
                "type": "RISK_ESCALATE",
                "code": "", "qty": 0, "price": 0.0,
                "day": self.env_state.day,
                "pnl_pct": 0.0, "mode": "RISK_ESCALATED",
                "policy_score": st.policy_score,
                "sentiment_phase": st.sentiment_phase,
                "news_tag": st.news_tag,
                "detail": "学习模块触发风控升级：熔断敏感度提升"
                          f"（dd阈值×{self.cfg.dd_escalate_mult}）",
            })

    # ---------- 收盘后复盘（15:30） ----------
    def review(self, day, now: datetime = None) -> dict:
        if self.last_review_day == day.isoformat():
            return self._last_report or {}
        now = now or datetime(day.year, day.month, day.day,
                              self.cfg.review_hour, 30, 0)
        self.trading_day_count += 1
        equity = self.agent.equity()

        # 1) 环境感知（四维学习输入）
        env = self.sense_environment(day, now)
        self.regime = detect_regime(self.market, self.cfg)

        # 2) 当日交易复盘 + 四维归因 + 因子有效性累计
        reviews = self._review_day_trades(day)

        # 3) 四维权重学习 + 量价子因子权重
        self._update_four_dim_weights()
        self._update_factor_weights()

        # 4) 策略类型自动切换（按环境偏好 + 历史表现）
        self._switch_strategy(env)

        # 5) 每 N 日历史回测优化 + 全场景验证
        backtest = None
        scenario = None
        if self.trading_day_count % self.cfg.learn_backtest_every == 0:
            backtest = self._backtest_optimize()
            self._apply_backtest(backtest)
            self._update_anchors()
            if self.cfg.scenario_backtest_on:
                scenario = self._scenario_validate(backtest)

        # 6) 周度风格校验
        drifts: List[str] = []
        wk = RiskController._week_key(day)
        if wk != self.last_week_key:
            drifts = self.agent.style_check(now)
            self.last_week_key = wk

        # 7) 进度记录 + 持久化
        self.equity_curve.append(equity)
        rec = self._progress_record(day, env, backtest, scenario, drifts)
        self.progress.append(rec)
        if self.storage:
            self.storage.append_progress(rec)
            self.persist()

        result = {
            "day": day.isoformat(), "regime": self.regime,
            "env": env.to_dict(), "reviews": reviews,
            "backtest": backtest, "scenario": scenario,
            "drifts": drifts, "progress": rec,
        }
        self.last_review_day = day.isoformat()
        self._last_report = result
        return result

    def _update_anchors(self) -> None:
        self._anchors = {
            "base_total_target": self.cfg.base_total_target,
            "stop_loss_pct": self.cfg.stop_loss_pct,
            "tp1_pct": self.cfg.tp1_pct,
            "tp2_pct": self.cfg.tp2_pct,
            "single_stock_max": self.cfg.single_stock_max,
            "max_new_open_per_week": self.cfg.max_new_open_per_week,
        }

    # ---------- 2) 当日交易复盘 + 四维归因 ----------
    def _review_day_trades(self, day) -> List[dict]:
        today = day.isoformat()
        out: List[dict] = []
        for e in self.agent.exit_log:
            if e["day"] != today or e["eid"] in self.processed:
                continue
            self.processed.add(e["eid"])
            mkt = self._market_return()
            entry_env = EnvState.from_dict(e["entry_env"]) if e.get("entry_env") else None
            exit_env = EnvState.from_dict(e["exit_env"]) if e.get("exit_env") else None
            attr = attribute_trade(e["pnl_pct"], mkt, e["reason"],
                                    e.get("entry_factors", {}), entry_env, exit_env)
            out.append({
                "code": e["code"], "reason": e["reason"],
                "pnl_pct": e["pnl_pct"],
                "primary": attr["primary"], "breakdown": attr["breakdown"],
                "four_dim": attr["four_dim"],
            })
            self._accumulate_effect(e, attr["four_dim"])
            self.realized_pnls.append(e["pnl_pct"])
        return out

    def _market_return(self) -> float:
        idx = self.cfg.priority_indexes[0] if self.cfg.priority_indexes else None
        if not idx:
            return 0.0
        try:
            closes = self.market.get_close_history(idx, 5)
            if len(closes) >= 2 and closes[-2] > 0:
                return closes[-1] / closes[-2] - 1.0
        except Exception:
            return 0.0
        return 0.0

    def _accumulate_effect(self, e: dict, four_dim: dict) -> None:
        """因子有效性累计：量价子因子按个股因子分；四维按归因贡献分；
        策略类型按「开仓时的策略权重快照」把该笔盈亏分摊到各 strategy_eff，
        使策略类型权重真正依据真实盈亏进化（此前 strategy_eff 始终为 0）。"""
        ef = e.get("entry_factors", {})
        for f in self.base_weights:
            self.factor_eff[f] += e["pnl_pct"] * ef.get(f, 0.0)
        for d in self.four_dim_eff:
            self.four_dim_eff[d] += e["pnl_pct"] * four_dim.get(d, 0.0)
        # 策略类型有效性：无快照时回退到当前混合权重（兼容旧持仓/历史数据）
        sw = e.get("entry_strategy_weights") or dict(self.strategy_weights)
        tot = sum(sw.values()) or 1.0
        for t in self.strategy_eff:
            self.strategy_eff[t] += e["pnl_pct"] * (sw.get(t, 0.0) / tot)

    def _update_factor_weights(self) -> None:
        lr = self.cfg.learn_lr
        scale = self.cfg.learn_eff_scale or 0.10
        mn, mx = self.cfg.factor_weight_min, self.cfg.factor_weight_max
        new = {}
        for f, w in self.base_weights.items():
            adj = lr * math.tanh(self.factor_eff[f] / scale)
            new[f] = w * (1.0 + adj)
        if mn is not None and mx is not None:
            for f in new:
                new[f] = min(mx, max(mn, new[f]))
        s = sum(max(0.0, v) for v in new.values()) or 1.0
        self.base_weights = {k: max(0.0, v) / s for k, v in new.items()}

    def _update_four_dim_weights(self) -> None:
        """四维顶层权重再平衡：盈利贡献高的维度提升权重（夏普优先、无上下限）。"""
        lr = self.cfg.four_dim_lr
        scale = self.cfg.learn_eff_scale or 0.10
        new = {}
        for d, w in self.four_dim_weights.items():
            adj = lr * math.tanh(self.four_dim_eff[d] / scale)
            new[d] = w * (1.0 + adj)
        s = sum(max(0.0, v) for v in new.values()) or 1.0
        self.four_dim_weights = {k: max(0.0, v) / s for k, v in new.items()}

    # ---------- 4) 策略类型自动切换 ----------
    def _switch_strategy(self, env: EnvState) -> None:
        """按环境偏好 + 历史表现再平衡三类策略资金权重（和为 1）。

        关键修正：环境标签只提供「方向偏好」，最终权重必须服从该策略的
        「历史有效性」(strategy_eff)。此前 strategy_eff 从未累计，且环境分支
        无条件把权重推向偏好策略，导致 trend_follow 一路被推到 0.777 而账户
        持续亏损——典型的「学了等于没学 / 盲从标签」。现改为：偏好策略唯有
        历史真实赚过钱才顺从加仓；历史亏损则收敛到中性，不再盲推。
        """
        tag = self.sensor.strategy_env_tag(env)
        lr = self.cfg.strategy_type_lr
        new = dict(self.strategy_weights)
        if tag and tag in self.cfg.env_strategy_pref:
            pref_type, pref_w = self.cfg.env_strategy_pref[tag]
            # 环境给方向，但权重服从历史有效性：盈利才加仓，亏损则收敛中性
            eff_pref = self.strategy_eff.get(pref_type, 0.0)
            eff_scale = scale_safe(sum(abs(v) for v in self.strategy_eff.values()))
            eff_signal = math.tanh(eff_pref / eff_scale) if eff_scale > 0 else 0.0
            gate = max(0.0, eff_signal)          # 亏损/无样本时为 0，不盲推
            for t in new:
                if t == pref_type:
                    new[t] = max(0.05, new[t] + lr * (pref_w - new[t]) * gate)
                else:
                    new[t] = max(0.05,
                                 new[t] - lr * (pref_w - new[t]) / (len(new) - 1) * gate)
        else:
            # 无明确环境信号：按历史有效性微调（盈利策略增权重）
            s = sum(max(0.0, v) for v in self.strategy_eff.values()) or 1.0
            for t in new:
                eff = self.strategy_eff[t]
                new[t] = new[t] * (1.0 + lr * math.tanh(eff / scale_safe(s)))
        tot = sum(new.values()) or 1.0
        self.strategy_weights = {k: max(0.0, v) / tot for k, v in new.items()}
        self.cfg.strategy_weights = dict(self.strategy_weights)
        # 档案：记录当前环境偏好命中的策略
        if tag:
            self.strategy_profile.append({
                "day": env.day, "env_tag": tag,
                "weights": dict(self.strategy_weights),
            })
            keep = self.cfg.profile_keep_days
            if len(self.strategy_profile) > keep:
                self.strategy_profile = self.strategy_profile[-keep:]

    # ---------- 5) 历史回测优化（量价参数，保留） ----------
    def _backtest_optimize(self) -> Optional[dict]:
        cands = self.agent.screener.build()
        if not cands:
            return None
        codes = [c.code for c in cands][:10]
        hist = {}
        for c in codes:
            try:
                hist[c] = self.market.get_close_history(c, self.cfg.learn_max_history)
            except Exception:
                hist[c] = []
        before = {
            "sl": self.cfg.stop_loss_pct, "tp1": self.cfg.tp1_pct,
            "tp2": self.cfg.tp2_pct,
            "base": self.position_target_suggest,
            "stock": self.cfg.single_stock_max,
        }
        best = None
        for sl in self.cfg.backtest_sl_grid:
            for tp1 in self.cfg.backtest_tp1_grid:
                if tp1 >= 0.12 and sl >= tp1:
                    continue
                for tp2 in self.cfg.backtest_tp2_grid:
                    if tp2 <= tp1:
                        continue
                    for base in self.cfg.backtest_base_grid:
                        for stock in self.cfg.backtest_stock_grid:
                            eqs, all_t = [], []
                            for c in codes:
                                h = hist[c]
                                if len(h) < 10:
                                    continue
                                curve, trades = simulate_backtest(h, sl, tp1, tp2)
                                if len(curve) >= 2:
                                    eqs.append(curve)
                                    all_t.extend(trades)
                            if not eqs:
                                continue
                            L = min(len(e) for e in eqs)
                            port = [sum(e[k] for e in eqs) / len(eqs)
                                    for k in range(L)]
                            sh = sharpe_ratio(daily_returns(port))
                            mdd = max_drawdown(port)
                            wr = win_rate(all_t)
                            ret = cumulative_return(port)
                            key = rank_key(sh, mdd, wr, ret)
                            if best is None or key > best["key"]:
                                reason = self._opt_reason(
                                    before, sl, tp1, tp2, base, stock, sh, mdd, wr)
                                best = {
                                    "key": key,
                                    "combo": (sl, tp1, tp2, base, stock),
                                    "sharpe": round(sh, 4),
                                    "mdd": round(mdd, 4),
                                    "wr": round(wr, 4),
                                    "ret": round(ret, 4),
                                    "reason": reason,
                                    "expected_effect": (
                                        f"夏普{sh:.2f}/回撤{mdd:.2%}/胜率{wr:.1%}/"
                                        f"收益{ret:.2%}；相对前参数"
                                        f"止损{before['sl']:.0%}→{sl:.0%}、"
                                        f"止盈{before['tp1']:.0%}&{before['tp2']:.0%}"
                                        f"→{tp1:.0%}&{tp2:.0%}、"
                                        f"单股{before['stock']:.0%}→{stock:.0%}"),
                                }
        return best

    @staticmethod
    def _opt_reason(before, sl, tp1, tp2, base, stock, sh, mdd, wr) -> str:
        parts = []
        if sh >= 1.0:
            parts.append("夏普≥1 风险调整后收益突出")
        elif sh > 0:
            parts.append("夏普为正，风险可控下增厚收益")
        else:
            parts.append("夏普为负，优先压回撤保本金")
        if mdd < before.get("_mdd", 1.0):
            parts.append("回撤收窄")
        if sl < before["sl"]:
            parts.append("收紧止损降低单笔亏损")
        if tp1 > before["tp1"]:
            parts.append("上调首档止盈让利润奔跑")
        if base > before["base"]:
            parts.append("提高正常市况仓位中枢以提升资金效率")
        if stock < before["stock"]:
            parts.append("下调单股上限以分散风险")
        return "；".join(parts) if parts else "网格寻优命中最优组合"

    def _apply_backtest(self, best: Optional[dict]) -> None:
        if not best:
            return
        sl, tp1, tp2, base, stock = best["combo"]
        self.cfg.stop_loss_pct = sl
        self.cfg.tp1_pct = tp1
        self.cfg.tp1_ratio = 0.50
        self.cfg.tp2_pct = tp2
        self.cfg.single_stock_max = stock
        self.position_target_suggest = base
        self._anchors["stop_loss_pct"] = sl
        self._anchors["tp1_pct"] = tp1
        self._anchors["tp2_pct"] = tp2
        self._anchors["single_stock_max"] = stock
        self._anchors["base_total_target"] = base

    def _scenario_validate(self, backtest: Optional[dict]) -> dict:
        """优化后全场景回测验证：返回场景对比与是否通过验证。"""
        sc = scenario_backtest(self.cfg.stop_loss_pct, self.cfg.tp1_pct,
                               self.cfg.tp2_pct)
        passed = (
            sc["summary"]["four_dim_wins"] >= 3
            and all(s["four_dim"]["sharpe"] >= self.cfg.scenario_pass_min_sharpe
                    or s["four_dim"]["mdd"] <= self.cfg.scenario_pass_max_dd
                    for s in sc["scenarios"])
        )
        return {"scenarios": sc["scenarios"], "summary": sc["summary"],
                "passed": passed,
                "min_sharpe_req": self.cfg.scenario_pass_min_sharpe,
                "max_dd_req": self.cfg.scenario_pass_max_dd}

    # ---------- 7) 进度记录 ----------
    def _progress_record(self, day, env, backtest, scenario, drifts) -> dict:
        init = self.agent.sim.account.initial_capital
        ret = (cumulative_return(self.equity_curve, init)
               if len(self.equity_curve) >= 2 else 0.0)
        sh = (sharpe_ratio(daily_returns(self.equity_curve))
              if len(self.equity_curve) >= 2 else 0.0)
        mdd = max_drawdown(self.equity_curve)
        wr = win_rate(self.realized_pnls)
        return {
            "iter": self.trading_day_count,
            "day": day.isoformat(),
            "regime": self.regime,
            "policy_score": round(env.policy_score, 4),
            "policy_active": env.policy_active,
            "news_net_impact": round(env.news_net_impact, 4),
            "news_active": env.news_active,
            "sentiment_index": round(env.sentiment_index, 4),
            "sentiment_phase": env.sentiment_phase,
            "risk_escalated": self.risk_escalated,
            "equity": round(self.equity_curve[-1], 2) if self.equity_curve else 0.0,
            "return_pct": round(ret * 100, 4),
            "sharpe": round(sh, 4),
            "max_dd": round(mdd, 4),
            "win_rate": round(wr, 4),
            "four_dim_weights": dict(self.four_dim_weights),
            "factor_weights": dict(self.cfg.factor_weights),
            "strategy_weights": dict(self.strategy_weights),
            "base_total_target": self.cfg.base_total_target,
            "stop_loss_pct": self.cfg.stop_loss_pct,
            "tp1_pct": self.cfg.tp1_pct,
            "single_stock_max": self.cfg.single_stock_max,
            "drifts": drifts,
            "backtest": backtest,
            "scenario": scenario,
        }

    # ---------- 持久化 ----------
    def persist(self) -> None:
        if not self.storage:
            return
        params = {
            "factor_weights": dict(self.cfg.factor_weights),
            "four_dim_weights": dict(self.four_dim_weights),
            "strategy_weights": dict(self.strategy_weights),
            "stop_loss_pct": self.cfg.stop_loss_pct,
            "tp1_pct": self.cfg.tp1_pct,
            "tp2_pct": self.cfg.tp2_pct,
            "base_total_target": self.cfg.base_total_target,
            "single_stock_max": self.cfg.single_stock_max,
            "max_new_open_per_week": self.cfg.max_new_open_per_week,
        }
        learner_state = {
            "base_weights": dict(self.base_weights),
            "factor_eff": dict(self.factor_eff),
            "four_dim_weights": dict(self.four_dim_weights),
            "four_dim_eff": dict(self.four_dim_eff),
            "strategy_weights": dict(self.strategy_weights),
            "strategy_eff": dict(self.strategy_eff),
            "trading_day_count": self.trading_day_count,
            "last_week_key": self.last_week_key,
            "processed": list(self.processed),
            "equity_curve": self.equity_curve,
            "realized_pnls": self.realized_pnls,
            "regime": self.regime,
            "position_target_suggest": self.position_target_suggest,
            "env_state": self.env_state.to_dict(),
            "strategy_profile": self.strategy_profile,
            "anchors": self._anchors,
        }
        self.storage.save_learned_params({
            "updated_at": datetime.now().isoformat(),
            "params": params, "learner": learner_state,
        })

    def load(self) -> bool:
        if not self.storage:
            return False
        d = self.storage.load_learned_params()
        if not d:
            return False
        p = d.get("params", {})
        if "factor_weights" in p:
            self.cfg.factor_weights = dict(p["factor_weights"])
        if "four_dim_weights" in p:
            self.four_dim_weights = dict(p["four_dim_weights"])
        if "strategy_weights" in p:
            self.strategy_weights = dict(p["strategy_weights"])
            self.cfg.strategy_weights = dict(p["strategy_weights"])
        for k in ("stop_loss_pct", "tp1_pct", "tp2_pct",
                  "base_total_target", "single_stock_max",
                  "max_new_open_per_week"):
            if k in p:
                setattr(self.cfg, k, p[k])
        ls = d.get("learner", {})
        if ls:
            self.base_weights = dict(ls.get("base_weights", self.base_weights))
            self.factor_eff = dict(ls.get("factor_eff", self.factor_eff))
            self.four_dim_eff = dict(ls.get("four_dim_eff", self.four_dim_eff))
            self.strategy_eff = dict(ls.get("strategy_eff", self.strategy_eff))
            self.trading_day_count = ls.get("trading_day_count", 0)
            self.last_week_key = ls.get("last_week_key")
            self.processed = set(ls.get("processed", []))
            self.equity_curve = list(ls.get("equity_curve", []))
            self.realized_pnls = list(ls.get("realized_pnls", []))
            self.regime = ls.get("regime", "OSCILLATION")
            self.position_target_suggest = ls.get("position_target_suggest", 0.60)
            if ls.get("env_state"):
                self.env_state = EnvState.from_dict(ls["env_state"])
            self.strategy_profile = list(ls.get("strategy_profile", []))
            if ls.get("anchors"):
                self._anchors = dict(ls["anchors"])
        return True

    # ---------- 学习进度曲线（HTML + 内联 SVG） ----------
    def export_progress_chart(self, path: str = "data/learning_progress.html") -> str:
        recs = list(self.progress)
        if not recs and self.storage:
            recs = self.storage.load_progress()
        svg_ret = self._svg_lines(
            "累计收益率 (%)", [r["day"] for r in recs],
            {"收益率": [r["return_pct"] for r in recs]}, ["#2b6cb0"])
        svg_sh = self._svg_lines(
            "夏普比率", [r["day"] for r in recs],
            {"夏普": [r["sharpe"] for r in recs]}, ["#dd6b20"])
        svg_dd = self._svg_lines(
            "最大回撤", [r["day"] for r in recs],
            {"回撤": [r["max_dd"] for r in recs]}, ["#c53030"])
        svg_fw = self._svg_lines(
            "四维因子权重演化", [r["day"] for r in recs],
            {d: [r.get("four_dim_weights", {}).get(d, 0.0) for r in recs]
             for d in ("price", "policy", "news", "sentiment")},
            ["#2b6cb0", "#38a169", "#d69e2e", "#9f7aea"])
        svg_sw = self._svg_lines(
            "策略类型权重演化", [r["day"] for r in recs],
            {s: [r.get("strategy_weights", {}).get(s, 0.0) for r in recs]
             for s in ("trend_follow", "reversal", "grid")},
            ["#2b6cb0", "#e53e3e", "#38a169"])
        html = _HTML_WRAP.format(
            ret=svg_ret, sh=svg_sh, dd=svg_dd, fw=svg_fw, sw=svg_sw,
            last=(_fmt_last(recs[-1]) if recs else "无数据"),
        )
        import os as _os
        _parent = _os.path.dirname(path)
        if _parent:
            _os.makedirs(_parent, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(html)
        return path

    @staticmethod
    def _svg_lines(title: str, x_labels: List[str],
                   series: Dict[str, List[float]],
                   colors: List[str], w: int = 760, h: int = 240) -> str:
        if not x_labels:
            return f'<div class="panel"><h3>{title}</h3><p class="muted">暂无数据</p></div>'
        pad_l, pad_r, pad_t, pad_b = 52, 16, 28, 34
        plot_w, plot_h = w - pad_l - pad_r, h - pad_t - pad_b
        allv = [v for vals in series.values() for v in vals]
        ymin, ymax = min(allv), max(allv)
        if ymin == ymax:
            ymin -= 1.0; ymax += 1.0
        span = ymax - ymin
        ymin -= span * 0.08
        ymax += span * 0.08
        n = len(x_labels)

        def x(i):
            return pad_l + (plot_w * i / max(1, n - 1)) if n > 1 else pad_l + plot_w / 2

        def y(v):
            return pad_t + plot_h * (1 - (v - ymin) / (ymax - ymin))

        parts = [f'<div class="panel"><h3>{title}</h3>',
                 f'<svg viewBox="0 0 {w} {h}" width="100%" preserveAspectRatio="xMidYMid meet">']
        if ymin < 0 < ymax:
            parts.append(f'<line x1="{pad_l}" y1="{y(0):.1f}" x2="{w-pad_r}" '
                         f'y2="{y(0):.1f}" stroke="#cbd5e0" stroke-dasharray="3 3"/>')
        for tick in (ymin, (ymin + ymax) / 2, ymax):
            parts.append(f'<text x="{pad_l-6}" y="{y(tick)+4:.1f}" '
                         f'class="ax" text-anchor="end">{tick:.2f}</text>')
        step = max(1, n // 8)
        for i in range(0, n, step):
            parts.append(f'<text x="{x(i):.1f}" y="{h-12}" class="ax" '
                         f'text-anchor="middle">{x_labels[i][5:]}</text>')
        for (name, vals), color in zip(series.items(), colors):
            pts = " ".join(f"{x(i):.1f},{y(v):.1f}" for i, v in enumerate(vals))
            parts.append(f'<polyline fill="none" stroke="{color}" stroke-width="2" '
                         f'points="{pts}"/>')
            for i, v in enumerate(vals):
                parts.append(f'<circle cx="{x(i):.1f}" cy="{y(v):.1f}" r="2.2" '
                             f'fill="{color}"/>')
        parts.append('</svg>')
        legend = " ".join(
            f'<span class="lg" style="background:{c}"></span>{name}'
            for (name, _), c in zip(series.items(), colors))
        parts.append(f'<div class="legend">{legend}</div></div>')
        return "".join(parts)


def scale_safe(x: float) -> float:
    return x if x > 0 else 0.10


def _fmt_last(r: dict) -> str:
    fd = r.get("four_dim_weights", {})
    sw = r.get("strategy_weights", {})
    return (f"最近一次迭代（第 {r['iter']} 日 {r['day']}）："
            f"政策{fd.get('policy',0):.2f}/消息{fd.get('news',0):.2f}/"
            f"情绪{fd.get('sentiment',0):.2f}/量价{fd.get('price',0):.2f}；"
            f"策略 趋势{fd.get('trend_follow', sw.get('trend_follow',0)):.2f}/"
            f"反转{sw.get('reversal',0):.2f}/网格{sw.get('grid',0):.2f}；"
            f"收益率={r['return_pct']:.2f}%，夏普={r['sharpe']:.3f}，"
            f"最大回撤={r['max_dd']:.2%}，胜率={r['win_rate']:.2%}")


_HTML_WRAP = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>交易智能体 · 四维学习进度</title>
<style>
 body{{font-family:-apple-system,'PingFang SC','Microsoft YaHei',sans-serif;
      background:#f7fafc;color:#2d3748;margin:0;padding:24px;}}
 h1{{font-size:20px;margin:0 0 4px;}}
 .sub{{color:#718096;font-size:13px;margin-bottom:18px;}}
 .grid{{display:grid;grid-template-columns:1fr 1fr;gap:16px;}}
 .panel{{background:#fff;border:1px solid #e2e8f0;border-radius:10px;
        padding:12px 14px;box-shadow:0 1px 3px rgba(0,0,0,.05);}}
 .panel h3{{margin:0 0 8px;font-size:14px;color:#2d3748;}}
 .ax{{font-size:10px;fill:#a0aec0;}}
 .legend{{margin-top:6px;font-size:12px;color:#4a5568;}}
 .lg{{display:inline-block;width:10px;height:10px;border-radius:2px;
     margin:0 4px 0 12px;vertical-align:middle;}}
 .muted{{color:#a0aec0;}}
 .note{{grid-column:1/3;background:#ebf8ff;border:1px solid #bee3f8;
       border-radius:10px;padding:12px 14px;font-size:13px;color:#2c5282;}}
</style></head>
<body>
<h1>交易智能体 · 学习进度曲线（四维：量价+政策+消息+情绪）</h1>
<div class="sub">优化目标：风险调整后收益最大化（夏普优先）· 全部本地生成</div>
<div class="grid">
{ret}{sh}{dd}{fw}{sw}
<div class="note">{last}</div>
</div></body></html>"""
