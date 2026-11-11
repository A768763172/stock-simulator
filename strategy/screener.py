"""选股器：选股池 + 多因子 → 排序候选与建议目标权重（要求 1+2）。

输出为一个目标组合：按综合得分降序，在单股≤5%、单行业≤15%、总仓≤基准目标
（默认 60%）的约束下贪心分配权重，体现稳定复利的均衡分散思路。
"""
from dataclasses import dataclass, field
from typing import Dict, List

from config.strategy import StrategyConfig
from market.base import MarketProvider
from strategy.stock_pool import StockPool
from strategy.factors import compute_factor_scores, annualized_vol


@dataclass
class Candidate:
    code: str
    name: str
    industry: str
    score: float
    factors: Dict[str, float]
    target_weight: float = 0.0        # 占总资金的目标权重
    env_adj: float = 0.0              # 环境调整分（政策/消息/情绪加成，量价分之外）


class Screener:
    def __init__(self, cfg: StrategyConfig, market: MarketProvider):
        self.cfg = cfg
        self.market = market
        self.pool = StockPool(cfg, market)

    def _warm(self, codes) -> None:
        """并行预热给定标的的 fund/close/quote 缓存（westock 支持时）。

        避免 build() 后续串行逐只冷启动 CLI（每只 ~8s）导致选股耗时数分钟。
        best-effort：行情源不支持 / 异常时静默跳过（退回串行，行为不变）。
        """
        codes = list(dict.fromkeys(codes or []))
        if not codes:
            return
        m = self.market
        if hasattr(m, "warm_universe"):
            try:
                m.warm_universe(codes, max_workers=10)
            except Exception:
                pass

    @staticmethod
    def _industry_hit(industry: str, keywords: List[str]) -> bool:
        """个股所属行业与政策/消息行业关键词匹配（双向子串）。

        行业名来自基本面接口（如「证券」「光伏设备」「互联网服务」），
        关键词来自盘前摘要人工判定。双向包含可兼容「券商」vs「资本市场服务」
        这类粒度差异；任一侧为空视为不命中。
        """
        if not industry or not keywords:
            return False
        ind = industry.strip()
        for kw in keywords:
            k = str(kw).strip()
            if not k:
                continue
            if k in ind or ind in k:
                return True
        return False

    def _env_adjustment(self, env, dim_weights: Dict[str, float],
                        industry: str) -> float:
        """个股级环境调整分：政策/消息按**行业命中**加权（不是大盘常数）。

        与大盘级加成不同——只有行业被政策/消息点名时才加分，未命中为 0，
        保证调整分对选股排序有区分度；全部未命中时退化为纯量价选股。
        幅度 = env_score_sens × Σ(四维学习权重 × 条目强度/冲击)，
        权重来自收盘归因再平衡——「环境值多少分」由历史盈亏学习决定。
        """
        if env is None or not dim_weights:
            return 0.0
        adj = 0.0
        w_pol = float(dim_weights.get("policy", 0.0) or 0.0)
        w_news = float(dim_weights.get("news", 0.0) or 0.0)
        if w_pol:
            for it in getattr(env, "policy_items", []) or []:
                if self._industry_hit(industry, it.get("industries")):
                    adj += w_pol * float(it.get("window_strength", 0.0) or 0.0)
        if w_news:
            for it in getattr(env, "news_items", []) or []:
                if self._industry_hit(industry, it.get("industries")):
                    adj += w_news * float(it.get("impact", 0.0) or 0.0)
        return self.cfg.env_score_sens * max(-1.0, min(1.0, adj))

    def build(self, env=None, dim_weights: Dict[str, float] = None) -> List[Candidate]:
        passing = self.pool.screen()
        if not passing:
            return []
        # 并行预热 passing 标的的 fund/close/quote，使下方因子计算命中缓存、
        # 消除串行冷启动 CLI 的秒级耗时（防御态跳过本分支时支出为 0）。
        self._warm([p["code"] for p in passing])

        # 1) 先算全市场波动率分布（用于低波动因子分位阈值）
        vol_universe: Dict[str, float] = {}
        for p in passing:
            try:
                closes = self.market.get_close_history(p["code"], 126)
                vol_universe[p["code"]] = annualized_vol(closes)
            except Exception:
                vol_universe[p["code"]] = 0.0

        # 2) 计算因子与综合得分
        scored = []
        for p in passing:
            fs = compute_factor_scores(p["code"], self.market, self.cfg, vol_universe)
            scored.append((p, fs))

        # 2.5) 个股级环境调整分（政策/消息行业命中加成；env 缺失或全未命中为 0）
        def _industry_of(p) -> str:
            fund = p.get("fundamentals")
            return ((fund.industry if fund else "") or "其他").strip()

        # 3) 按「量价分 + 环境调整分」降序
        scored.sort(
            key=lambda x: x[1]["score"] + self._env_adjustment(
                env, dim_weights or {}, _industry_of(x[0])),
            reverse=True,
        )

        # 4) 贪心分配目标权重（单股≤5%、单行业≤15%、总仓≤base_total_target）
        total_budget = self.cfg.base_total_target
        industry_used: Dict[str, float] = {}
        candidates: List[Candidate] = []
        for p, fs in scored:
            if total_budget <= 1e-9:
                break
            industry = _industry_of(p)
            ind_rem = self.cfg.single_industry_max - industry_used.get(industry, 0.0)
            w = min(self.cfg.single_stock_max, ind_rem, total_budget)
            if w <= 1e-9:
                continue
            candidates.append(Candidate(
                code=p["code"], name=p["name"], industry=industry,
                score=fs["score"], factors=fs, target_weight=round(w, 4),
                env_adj=round(self._env_adjustment(
                    env, dim_weights or {}, industry), 4),
            ))
            total_budget -= w
            industry_used[industry] = industry_used.get(industry, 0.0) + w

        return candidates

    def report(self, env=None, dim_weights: Dict[str, float] = None) -> dict:
        cands = self.build(env=env, dim_weights=dim_weights)
        return {
            "count": len(cands),
            "candidates": [
                {
                    "code": c.code, "name": c.name, "industry": c.industry,
                    "score": c.score, "target_weight": c.target_weight,
                    "env_adj": c.env_adj,
                    "factors": c.factors,
                } for c in cands
            ],
        }
