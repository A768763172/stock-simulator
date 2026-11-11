"""市场环境因子模块（四维学习之「政策 / 消息 / 情绪」三维市场级输入）。

设计原则（贴合 A 股「政策性、消息性、情绪性驱动」核心特性）：
- 不照搬美股纯技术驱动逻辑；政策 / 消息 / 情绪作为与个股量价并列的市场级因子，
  共同参与学习、归因与参数优化。
- 数据接入可插拔：默认 MockEnvProvider 用内置事件日历 + 行情派生情绪指标演示；
  真实部署时实现 MarketEnvProvider 接口（对接资讯 / 政策 / 资金流数据源）即可替换，
  学习模块其余逻辑无需改动。

对外输出统一的 EnvState：
  policy    : {level, score, window_strength, tag, active}
  news      : {net_impact, nature, magnitude, half_life_days, tag, active}
  sentiment : {index, phase, indicators}
"""
import json
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from config.strategy import StrategyConfig


# ============ 环境状态结构 ============
@dataclass
class EnvState:
    """某一时点的市场环境快照（四维学习的环境输入）。"""
    day: str = ""
    # 政策面：0=无、1=国家、2=部委、3=行业；score∈[-1,1]（负=调控，正=扶持）
    policy_level: int = 0
    policy_score: float = 0.0
    policy_window_strength: float = 0.0
    policy_tag: str = ""
    policy_active: bool = False
    # 消息面：net_impact∈[-1,1]（负=净利空，正=净利好）
    news_net_impact: float = 0.0
    news_nature: str = "neutral"        # bullish / bearish / neutral
    news_magnitude: float = 0.0
    news_tag: str = ""
    news_active: bool = False
    # 情绪面：index∈[-1,1]；phase∈{冰点,恐慌,修复,亢奋,中性}
    sentiment_index: float = 0.0
    sentiment_phase: str = "中性"
    sentiment_indicators: Dict[str, float] = field(default_factory=dict)
    # 当日激活的政策/消息条目摘要（供选股层做行业级影响传导）。
    # policy_items: [{id, level, tag, window_strength, industries}]
    # news_items:   [{id, tag, impact, magnitude, industries}]
    # industries 为行业关键词列表（来自盘前摘要的人工判定），选股时按个股
    # 所属行业匹配；无映射时列表为空，选股层自动退化为纯量价。
    policy_items: List[dict] = field(default_factory=list)
    news_items: List[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "day": self.day,
            "policy_level": self.policy_level, "policy_score": round(self.policy_score, 4),
            "policy_window_strength": round(self.policy_window_strength, 4),
            "policy_tag": self.policy_tag, "policy_active": self.policy_active,
            "news_net_impact": round(self.news_net_impact, 4),
            "news_nature": self.news_nature, "news_magnitude": round(self.news_magnitude, 4),
            "news_tag": self.news_tag, "news_active": self.news_active,
            "sentiment_index": round(self.sentiment_index, 4),
            "sentiment_phase": self.sentiment_phase,
            "sentiment_indicators": {k: round(v, 4)
                                     for k, v in self.sentiment_indicators.items()},
            "policy_items": self.policy_items,
            "news_items": self.news_items,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "EnvState":
        s = cls()
        for k, v in d.items():
            if hasattr(s, k):
                setattr(s, k, v)
        return s


# ============ 数据源接口（可插拔） ============
class MarketEnvProvider:
    """环境因子数据源接口。真实部署继承并实现下列方法。

    返回结构约定见 MockEnvProvider 各方法。
    """

    def active_policies(self, day: date) -> List[dict]:
        raise NotImplementedError

    def active_news(self, day: date) -> List[dict]:
        raise NotImplementedError

    def sentiment_raw(self, day: date, market) -> Dict[str, float]:
        """返回各情绪指标原始读数（已归一化到合理区间）。"""
        raise NotImplementedError


# ============ 默认实现：内置事件日历 + 行情派生情绪 ============
class MockEnvProvider(MarketEnvProvider):
    """演示用环境源：内置政策/消息事件日历（按日期窗口激活）+ 行情派生情绪指标。

    真实数据接入时，仅需替换 ``active_policies`` / ``active_news``（对接数据源）、
    及 ``sentiment_raw``（对接资金流 / 涨跌家数等），学习模块其余逻辑不变。
    """

    def __init__(self, cfg: StrategyConfig,
                 policy_events: Optional[List[dict]] = None,
                 news_flow: Optional[List[dict]] = None,
                 # 可注入确定性激活窗口（测试 / 演示用），否则按内置日历默认窗口
                 force_policy_days: Optional[List[str]] = None,
                 force_news_days: Optional[List[str]] = None,
                 force_sentiment: Optional[float] = None):
        self.cfg = cfg
        self.policy_events = list(policy_events if policy_events is not None
                                  else list(cfg.policy_events))
        self.news_flow = list(news_flow if news_flow is not None
                              else list(cfg.news_flow))
        self._force_policy_days = set(force_policy_days or [])
        self._force_news_days = set(force_news_days or [])
        self._force_sentiment = force_sentiment

    # ---- 政策：内置事件为「候选库」，需数据源推送生效；默认不激活，
    #      仅 force_policy_days 注入演示（避免默认永远「利好」失真） ----
    def active_policies(self, day: date) -> List[dict]:
        if day.isoformat() not in self._force_policy_days:
            return []
        out: List[dict] = []
        for ev in self.policy_events:
            wd = int(ev.get("window_days", 10))
            # 强制激活视为发布当日（强度满格），按窗口线性衰减
            for age in range(0, wd + 1):
                decay = 1.0 - age / (wd + 1)
                out.append({
                    "id": ev["id"], "level": ev["level"],
                    "strength": float(ev["strength"]),
                    "window_strength": float(ev["strength"]) * decay,
                    "tag": ev.get("tag", ""), "age": age, "window_days": wd,
                    "industries": [str(x) for x in (ev.get("industries") or [])],
                })
        return out

    # ---- 消息：内置消息流同为此理，默认不激活，仅 force_news_days 注入 ----
    def active_news(self, day: date) -> List[dict]:
        if day.isoformat() not in self._force_news_days:
            return []
        out: List[dict] = []
        for nv in self.news_flow:
            hl = float(nv.get("half_life_days", 3))
            for age in range(0, max(1, int(hl * 4)) + 1):
                decay = 0.5 ** (age / hl) if hl > 0 else 1.0
                sign = {"bullish": 1.0, "bearish": -1.0, "neutral": 0.0
                        }.get(nv.get("nature", "neutral"), 0.0)
                out.append({
                    "id": nv["id"], "nature": nv.get("nature", "neutral"),
                    "magnitude": float(nv.get("magnitude", 0.5)),
                    "impact": sign * float(nv.get("magnitude", 0.5)) * decay,
                    "tag": nv.get("tag", ""), "age": age, "half_life_days": hl,
                    "industries": [str(x) for x in (nv.get("industries") or [])],
                })
        return out

    # ---- 情绪：从行情派生 8 项原始指标，归一化到 [-1,1] ----
    def sentiment_raw(self, day: date, market) -> Dict[str, float]:
        if self._force_sentiment is not None:
            # 演示/测试：直接给定全局情绪指数，各分项按比例填充
            s = float(self._force_sentiment)
            return {
                "turnover": s, "advance_decline": s, "northbound": s,
                "margin": s, "amplitude_rate": s * 0.8, "limit_up_down": s,
                "连板高度": s * 0.8, "panic_euphoria": s,
            }
        raw: Dict[str, float] = {}
        try:
            idx = self.cfg.priority_indexes[0] if self.cfg.priority_indexes else None
            closes = market.get_close_history(idx, 21) if idx else []
        except Exception:
            closes = []
        if len(closes) >= 5:
            ret5 = closes[-1] / closes[-5] - 1.0 if closes[-5] > 0 else 0.0
            vol20 = market_annual_vol(closes[-20:]) if len(closes) >= 20 else 0.0
            # 价格动量→乐观度
            raw["turnover"] = max(-1.0, min(1.0, ret5 / 0.05))
            # 波动率→恐慌反向（波动越高越恐慌）
            raw["panic_euphoria"] = max(-1.0, min(1.0, -vol20 / 0.40 + 0.3))
        else:
            raw["turnover"] = 0.0
            raw["panic_euphoria"] = 0.0
        # 其余指标（涨跌家数/北向/两融/涨跌停/振幅/连板）无数据源时**不返回键**，
        # 由 sensor 聚合时自动把权重让渡给有读数项（RealSentimentProvider 会
        # 从市场宽度行情补上其中三项）。
        return raw


# ============ 真实数据源：每日资讯摘要文件 ============
# 摘要文件格式（由每日盘前检索任务生成，落盘到 <项目根>/data/env_digest_YYYY-MM-DD.json）：
# {
#   "day": "2026-10-13",
#   "generated_by": "workbuddy-agent",
#   "policy": [                       # 当日新增/仍在窗口期的政策（无则空列表）
#     {"id": "d-2026-10-13-p1", "level": "national|ministry|industry",
#      "strength": -1~1,              # 负=调控利空，正=扶持利好
#      "window_days": 15,             # 影响持续周期（自然日）
#      "tag": "货币政策适度宽松", "summary": "一句话摘要"}
#   ],
#   "news": [                         # 当日重要消息（无则空列表）
#     {"id": "d-2026-10-13-n1",
#      "nature": "bullish|bearish|neutral",
#      "magnitude": 0~1,              # 冲击量级；>=0.8 视为重磅（触发降频/风控升级）
#      "half_life_days": 3,           # 影响半衰期（自然日）
#      "tag": "行业景气超预期", "summary": "一句话摘要"}
#   ]
# }
# 说明：strength / magnitude / window_days / half_life_days 由检索时人工判定写入；
# 引擎侧只做窗口衰减与合成，不做主观判断。文件缺失或损坏时安全回退中性，
# 不影响交易主链路。

class FileEnvProvider(MarketEnvProvider):
    """从每日资讯摘要文件读取政策/消息的真实环境源。

    - 扫描 digest_dir 下全部 env_digest_*.json，按文件内 day 与查询日计算
      窗口衰减（公式与 MockEnvProvider 一致），跨日窗口自动生效
      （如周一发布的 15 日窗口政策，周五仍按 age=4 衰减计入）。
    - 情绪面仍由内置 MockEnvProvider 从行情派生（本次不改情绪数据层）。
    - 任何文件级异常（缺文件/JSON 损坏/字段缺失）均静默跳过，回退中性。
    """

    def __init__(self, cfg: StrategyConfig, digest_dir: Optional[str] = None,
                 sentiment_provider: Optional[MarketEnvProvider] = None):
        self.cfg = cfg
        if digest_dir:
            self.digest_dir = Path(digest_dir)
        else:
            # 与 storage/local_store.py 同方式锚定到 <项目根>/data，
            # 不依赖运行时 cwd，保证与看板/引擎读写同一位置。
            self.digest_dir = Path(__file__).resolve().parent.parent / "data"
        # 情绪面委托（默认价格派生 + 市场结构指标增强）
        self._sentiment_src = sentiment_provider or RealSentimentProvider(cfg)
        self._digest_cache: Dict[str, Tuple[float, dict]] = {}
        self._skipped_files = 0

    # ---- 内部：加载全部摘要文件（按 mtime 缓存，避免盘中反复解析） ----
    def _load_digests(self) -> List[dict]:
        out: List[dict] = []
        if not self.digest_dir.is_dir():
            return out
        for fp in sorted(self.digest_dir.glob("env_digest_*.json")):
            try:
                mtime = fp.stat().st_mtime
                cached = self._digest_cache.get(fp.name)
                if cached and cached[0] == mtime:
                    out.append(cached[1])
                    continue
                d = json.loads(fp.read_text(encoding="utf-8"))
                if not isinstance(d, dict) or not d.get("day"):
                    raise ValueError("missing day")
                self._digest_cache[fp.name] = (mtime, d)
                out.append(d)
            except Exception:
                self._skipped_files += 1
                continue
        return out

    def active_policies(self, day: date) -> List[dict]:
        out: List[dict] = []
        for d in self._load_digests():
            try:
                d_day = date.fromisoformat(str(d.get("day")))
            except Exception:
                continue
            age = (day - d_day).days
            if age < 0:
                continue  # 未来日期的摘要不生效
            for ev in d.get("policy", []) or []:
                try:
                    wd = int(ev.get("window_days", 10))
                    if age > wd:
                        continue
                    strength = float(ev.get("strength", 0.0))
                    decay = 1.0 - age / (wd + 1)
                    out.append({
                        "id": ev.get("id", f"{d.get('day')}-p"),
                        "level": ev.get("level", "industry"),
                        "strength": strength,
                        "window_strength": strength * decay,
                        "tag": ev.get("tag", ""), "age": age, "window_days": wd,
                        "industries": [str(x) for x in (ev.get("industries") or [])],
                    })
                except Exception:
                    continue
        return out

    def active_news(self, day: date) -> List[dict]:
        out: List[dict] = []
        for d in self._load_digests():
            try:
                d_day = date.fromisoformat(str(d.get("day")))
            except Exception:
                continue
            age = (day - d_day).days
            if age < 0:
                continue
            for nv in d.get("news", []) or []:
                try:
                    hl = float(nv.get("half_life_days", 3))
                    if age > max(1, int(hl * 4)):
                        continue
                    magnitude = float(nv.get("magnitude", 0.5))
                    decay = 0.5 ** (age / hl) if hl > 0 else 1.0
                    sign = {"bullish": 1.0, "bearish": -1.0, "neutral": 0.0
                            }.get(nv.get("nature", "neutral"), 0.0)
                    out.append({
                        "id": nv.get("id", f"{d.get('day')}-n"),
                        "nature": nv.get("nature", "neutral"),
                        "magnitude": magnitude,
                        "impact": sign * magnitude * decay,
                        "tag": nv.get("tag", ""), "age": age, "half_life_days": hl,
                        "industries": [str(x) for x in (nv.get("industries") or [])],
                    })
                except Exception:
                    continue
        return out

    def sentiment_raw(self, day: date, market) -> Dict[str, float]:
        # 情绪面暂不接入文件源，沿用行情派生实现
        return self._sentiment_src.sentiment_raw(day, market)


# ============ 情绪面增强：市场结构指标 ============
class RealSentimentProvider(MarketEnvProvider):
    """在底层价格派生情绪之外，叠加市场结构指标（涨跌家数/涨跌停/振幅）。

    指标口径（均归一化到 [-1,1]）：
    - advance_decline  : (上涨-下跌)/有效总数，市场宽度方向
    - limit_up_down    : (涨停-跌停)/有效总数 ×3，涨跌停结构（A股涨停远多于
                         跌停，×3 后进入有效区间）
    - amplitude_rate   : 平均振幅(高低/昨收) × 市场方向符号——放量上涨为亢奋、
                         放量下跌为恐慌，让振幅带上方向
    - turnover/panic_euphoria：委托底层（指数动量 / 波动率反向）
    - northbound/margin/连板高度：无实时数据源，**不返回键**（权重自动重分配
      给有读数指标，避免恒 0 项稀释情绪指数）

    市场宽度数据来自 market.market_breadth()（行情层从已预热 quote 缓存零成本
    提取）；为空时自动降级为底层价格派生指标。
    """

    # 平均振幅归一化基准：全市场平均振幅约 2%~5%，取 4% 为满刻度
    AMP_FULL_SCALE = 0.04
    # 涨跌停归一化放大系数
    LIMIT_SCALE = 3.0

    def __init__(self, cfg: StrategyConfig,
                 base: Optional[MarketEnvProvider] = None):
        self.cfg = cfg
        self.base = base or MockEnvProvider(cfg)

    def sentiment_raw(self, day: date, market) -> Dict[str, float]:
        raw = dict(self.base.sentiment_raw(day, market))
        breadth = []
        try:
            breadth = market.market_breadth() if market is not None else []
        except Exception:
            breadth = []
        if not breadth:
            return raw  # 无市场宽度数据：仅保留价格派生指标

        up = down = lu = ld = 0
        amp_sum = 0.0
        amp_n = 0
        for b in breadth:
            chg = b.get("change_pct")
            if chg is None:
                price, prev = b.get("price", 0.0), b.get("prev_close", 0.0)
                if price > 0 and prev > 0:
                    chg = (price - prev) / prev * 100.0
                else:
                    chg = 0.0
            if chg > 0.01:
                up += 1
            elif chg < -0.01:
                down += 1
            # 涨跌停判定：价格触板（容差 0.5%）或涨幅达 ±9.8%（北交所/科创
            # 20% 板由 ceiling/floor 自动覆盖）
            price, ceil_p = b.get("price", 0.0), b.get("ceiling", 0.0)
            floor_p = b.get("floor", 0.0)
            if ceil_p > 0 and price >= ceil_p * 0.995:
                lu += 1
            elif floor_p > 0 and price <= floor_p * 1.005 and floor_p > 0:
                ld += 1
            hi, lo, prev = b.get("high", 0.0), b.get("low", 0.0), \
                b.get("prev_close", 0.0)
            if hi > 0 and lo > 0 and prev > 0:
                amp_sum += (hi - lo) / prev
                amp_n += 1

        total = up + down
        ad = (up - down) / total if total > 0 else 0.0
        n_all = max(1, len(breadth))
        limit = (lu - ld) / n_all * self.LIMIT_SCALE
        amp = (amp_sum / amp_n) if amp_n > 0 else 0.0
        # 振幅带方向：与市场宽度同号
        amp_dir = amp / self.AMP_FULL_SCALE * (1.0 if ad >= 0 else -1.0)

        raw["advance_decline"] = max(-1.0, min(1.0, ad))
        raw["limit_up_down"] = max(-1.0, min(1.0, limit))
        raw["amplitude_rate"] = max(-1.0, min(1.0, amp_dir))
        return raw

    def active_policies(self, day: date) -> List[dict]:
        return self.base.active_policies(day)

    def active_news(self, day: date) -> List[dict]:
        return self.base.active_news(day)


def market_annual_vol(closes: List[float]) -> float:
    """局部年化波动率（与 factors.annualized_vol 同口径，供情绪派生）。"""
    if len(closes) < 2:
        return 0.0
    r = []
    for i in range(1, len(closes)):
        if closes[i - 1] > 0:
            r.append(closes[i] / closes[i - 1] - 1.0)
    if len(r) < 2:
        return 0.0
    mean = sum(r) / len(r)
    var = sum((x - mean) ** 2 for x in r) / (len(r) - 1)
    return (var ** 0.5) * (242.0 ** 0.5)


# ============ 环境合成器 ============
class MarketEnvSensor:
    """将 Provider 的原始读数合成为统一 EnvState（含政策/消息/情绪三维）。"""

    def __init__(self, cfg: StrategyConfig, provider: MarketEnvProvider):
        self.cfg = cfg
        self.provider = provider

    def sense(self, day: date, market=None) -> EnvState:
        st = EnvState(day=day.isoformat())

        # —— 政策面：取激活事件的「层级加权×强度」合成 score ——
        policies = self.provider.active_policies(day)
        if policies:
            score = 0.0
            top = policies[0]
            for p in policies:
                lw = self.cfg.policy_level_weight.get(p["level"], 0.3)
                score += p["window_strength"] * lw
                if p["window_strength"] * lw > top["window_strength"] * \
                        self.cfg.policy_level_weight.get(top["level"], 0.3):
                    top = p
            st.policy_score = max(-1.0, min(1.0, score))
            st.policy_level = {"national": 1, "ministry": 2,
                               "industry": 3}.get(top["level"], 0)
            st.policy_window_strength = top["window_strength"]
            st.policy_tag = top["tag"]
            st.policy_active = True
            # 条目摘要（按窗口强度降序，供选股层行业匹配）
            st.policy_items = [
                {"id": p["id"], "level": p["level"], "tag": p["tag"],
                 "window_strength": round(p["window_strength"], 4),
                 "industries": [str(x) for x in (p.get("industries") or [])]}
                for p in sorted(policies,
                                key=lambda x: -x["window_strength"])
            ]

        # —— 消息面：净冲击合成 ——
        news = self.provider.active_news(day)
        if news:
            net = sum(n["impact"] for n in news)
            st.news_net_impact = max(-1.0, min(1.0, net))
            st.news_magnitude = max(n["magnitude"] for n in news)
            st.news_nature = ("bullish" if net > 0.05 else
                              "bearish" if net < -0.05 else "neutral")
            st.news_tag = max(news, key=lambda n: n["magnitude"])["tag"]
            st.news_active = True
            # 条目摘要（按冲击降序，供选股层行业匹配）
            st.news_items = [
                {"id": n["id"], "tag": n["tag"],
                 "impact": round(n["impact"], 4),
                 "magnitude": n["magnitude"],
                 "industries": [str(x) for x in (n.get("industries") or [])]}
                for n in sorted(news, key=lambda x: -abs(x["impact"]))
            ]

        # —— 情绪面：加权合成指数，并判定周期 ——
        # 只聚合「有读数」的指标：无数据源的项（北向/两融/连板）不返回键，
        # 其权重自动重分配给有读数项，避免恒 0 项稀释情绪指数。
        raw = self.provider.sentiment_raw(day, market)
        st.sentiment_indicators = raw
        idx = 0.0
        wsum = 0.0
        for k, w in self.cfg.sentiment_indicator_weights.items():
            if k not in self.cfg.sentiment_indicators:
                continue
            v = raw.get(k)
            if v is None:
                continue  # 无读数：权重让渡给其他指标
            idx += float(v) * w
            wsum += w
        st.sentiment_index = max(-1.0, min(1.0, idx / wsum)) if wsum > 0 else 0.0
        st.sentiment_phase = self._phase(st.sentiment_index)
        return st

    def _phase(self, idx: float) -> str:
        if idx <= self.cfg.sentiment_ice_thr:
            return "冰点"
        if idx <= self.cfg.sentiment_panic_thr:
            return "恐慌"
        if idx >= self.cfg.sentiment_euphoria_thr:
            return "亢奋"
        if idx > 0.3:
            return "修复"
        return "中性"

    # —— 环境「标签」用于策略类型偏好匹配 ——
    def strategy_env_tag(self, st: EnvState) -> Optional[str]:
        if st.policy_active and st.policy_score >= 0.4:
            return "policy_bull"
        if st.sentiment_phase in ("恐慌", "冰点"):
            return "sentiment_panic"
        if st.sentiment_phase in ("中性",) and abs(st.sentiment_index) < 0.2 \
                and not st.policy_active and not st.news_active:
            return "oscillation"
        return None
