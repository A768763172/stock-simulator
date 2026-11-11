"""量化交易策略总配置（稳定复利风格）。

全部阈值 / 权重集中于此，便于回测调参与审查。
专家审核要点：
- 选股池：以沪深主板为主；剔除科创板(688)/创业板(300/301)/北交所；ST、亏损、高质押、低成交。可选纳入 ETF（include_etfs，复利友好：跟踪指数、波动更平滑、可长期持有）。
- 多因子：质量35% / 估值25% / 低波动25% / 动量15%（基础权重，学习系统会按行情连续再平衡）。
- 仓位（自适应）：总仓中枢 / 单股 / 单行业上限默认由自学习引擎依据行情连续调整，无硬编码上下限（仅物理边界 0-100%，不可杠杆）；风控铁律仍作应急刹车。分批 40/30/30。
- 买卖：中线 5-15 日；盈利 8% 止盈 50%、15% 清仓；浮亏 3% 止损；周新开≤5、周交易≤5。
- 风控铁律：日回撤≥1.5% 降至<30% 且次日禁开；周回撤≥3% 只卖≤20%；高点回撤≥5% 全面防守≤10%。
- 自适应：开启 adaptive_position / adaptive_factors 后，配置比例完全由自学习系统按实时行情驱动，无需预设上下限；关闭后回退为下方硬性默认比例。
"""
from dataclasses import dataclass, field
from typing import Dict, Optional, Tuple


@dataclass
class StrategyConfig:
    # ===================== 选股池 =====================
    # 彻底剔除的板块前缀（科创板 / 创业板）
    exclude_boards: Tuple[str, ...] = ("sh688", "sz300", "sz301")
    # 主板认可的代码前缀（用于正向判定，避免误收北交所等）
    main_boards: Tuple[str, ...] = ("sh60", "sz00", "sz002", "sz003")
    exclude_st: bool = True                       # 剔除 ST / *ST
    exclude_loss_net_profit: bool = True           # 剔除近一年净利润亏损
    max_pledge_ratio: float = 0.50                 # 股权质押率上限（>50% 剔除）
    min_avg_turnover_30d: float = 5_000_000.0      # 近30日日均成交额下限（500万元）
    # 优先指数池（沪深300 / 中证500）；成分内再剔除科创/创业
    priority_indexes: Tuple[str, ...] = ("sh000300", "sz399905")

    # ===================== ETF 可选池 =====================
    # 复利友好：ETF 跟踪指数、波动更平滑、可长期持有，适合作为稳定复利的配置层。
    include_etfs: bool = True                       # 选股池是否纳入 ETF
    etf_boards: Tuple[str, ...] = ("sh51", "sh58", "sz15", "sz16")  # ETF 代码前缀
    etf_industry: str = "ETF"                       # ETF 归入的行业桶（受单行业≤15% 约束）
    # 种子 ETF：流动性好、以宽基/风格为主；实际能否入选仍以行情源返回的流动性为准
    etf_seeds: Tuple[str, ...] = (
        "sh510300",   # 沪深300ETF（华泰柏瑞，宽基、最活跃）
        "sh510500",   # 中证500ETF（南方，中盘宽基）
        "sz159915",   # 创业板ETF（易方达，成长）
        "sh588000",   # 科创50ETF（华夏，科技）
        "sh510880",   # 红利ETF（华泰柏瑞，高股息、复利友好）
        "sz159919",   # 沪深300ETF（嘉实，宽基）
        "sh512880",   # 证券ETF（国泰，行业）
        "sh515030",   # 新能源车ETF（华夏，主题）
    )

    # ===================== 多因子权重 =====================
    factor_weights: Dict[str, float] = field(default_factory=lambda: {
        "quality": 0.35,
        "valuation": 0.25,
        "low_vol": 0.25,
        "momentum": 0.15,
    })
    # 质量因子子项
    min_roe_3y: float = 0.10                       # 连续3年 ROE>10%
    ocf_cover_net_profit: bool = True              # 经营现金流覆盖净利润
    margin_stable_band: float = 0.10               # 毛利率稳定（变异系数阈值）
    # 估值因子
    valuation_pct_max: float = 0.30                # PE/PB 近3年分位 ≤30%
    # 低波动因子
    vol_quantile_max: float = 0.40                 # 波动率低于全市场前 40%
    max_drawdown_stage: float = 0.15               # 阶段最大回撤 <15%
    # 动量因子
    momentum_excess_min: float = 0.0               # 近1月超额收益 >0（小幅跑赢）
    momentum_no_chase: float = 0.15                # 不追高：月度绝对涨幅上限

    # ===================== 仓位管理 =====================
    # —— 自适应模式（默认开启）：以下「目标比例」由自学习引擎依据实时行情连续驱动，
    #    不设固定的总仓上下限 / 单股 / 单行业硬编码比例；仅保留物理边界
    #    （总仓 ∈ [0,1]，即不可杠杆、不可为负）。风控铁律（回撤状态机）仍作为
    #    不可突破的应急刹车保留。关闭 adaptive_position 时回退为下方硬性默认。
    adaptive_position: bool = True                 # 总仓中枢 / 单股 / 单行业上限 自适应
    total_pos_min: Optional[float] = None          # 总仓位下限（None=自适应，无硬性下限）
    total_pos_max: Optional[float] = None          # 总仓位上限（None=自适应，无硬性上限）
    min_cash_reserve: float = 0.0                  # 备用现金硬下限（自适应时由 base_total_target 反推；置 0=全交学习系统）
    base_total_target: float = 0.60                # 正常市况目标总仓位（学习系统会按行情调整）
    single_stock_max: float = 0.05                 # 单股目标权重（学习系统按波动连续调整）
    single_industry_max: float = 0.15              # 单行业目标权重（学习系统按波动连续调整）
    batch_plan: Tuple[float, ...] = (0.40, 0.30, 0.30)  # 分批建仓比例
    batch_pullback: float = 0.02                   # 每回调 2% 加下一批
    allow_avg_down: bool = False                   # 亏损头寸绝不补仓
    allow_pyramid_win: bool = True                 # 仅盈利头寸可加仓

    # ===================== 买卖交易规则 =====================
    hold_min_days: int = 5                         # 最小持股周期（交易日）
    hold_max_days: int = 15                        # 最大持股周期（交易日）
    tp1_pct: float = 0.08                          # 盈利 8% 触发
    tp1_ratio: float = 0.50                        # 止盈 50% 仓位
    tp2_pct: float = 0.15                          # 盈利 15% 清仓
    stop_loss_pct: float = 0.03                    # 浮亏 3% 止损
    max_new_open_per_week: int = 5                 # 单周新开仓≤5 只
    max_trades_per_week: int = 5                   # 单周总交易次数≤5

    # ===================== 风控铁律（不可突破） =====================
    dd_daily_max: float = 0.015                    # 单日账户回撤≥1.5%
    dd_daily_reduce_to: float = 0.30               # 当日强制降至<30%
    dd_weekly_max: float = 0.03                    # 单周账户回撤≥3%
    dd_weekly_pos_cap: float = 0.20                # 防守只卖，总仓≤20%
    dd_peak_max: float = 0.05                      # 历史高点回撤≥5%
    dd_peak_pos_cap: float = 0.10                  # 全面防守，底仓≤10%

    # ===================== 自学习进化（稳定复利 · 夏普优先） =====================
    # 优化目标优先级：① 控制回撤 ② 提升胜率 ③ 提升收益；统一以夏普比率最大化为目标函数。
    # 自适应模式下，配置比例（因子权重 / 总仓中枢 / 单股单行业上限）由学习系统按实时
    # 行情连续驱动，不设固定上下限；仅做物理裁剪（总仓∈[0,1]、权重非负）与归一化。
    adaptive_factors: bool = True                 # 因子权重自适应（无上下限，仅归一化）
    review_hour: int = 15                         # 收盘后复盘时刻（15:30）
    learn_lr: float = 0.15                        # 因子权重迭代学习率
    learn_eff_scale: float = 0.10                 # 因子有效性归一化尺度（pnl×score 累计量）
    factor_weight_min: Optional[float] = None     # 单因子权重下限（None=自适应，无硬性下限）
    factor_weight_max: Optional[float] = None     # 单因子权重上限（None=自适应，无硬性上限）
    learn_backtest_every: int = 5                 # 每 N 个交易日做一次历史回测优化
    learn_max_history: int = 126                  # 回测所用历史长度（交易日）
    # 自适应行情读取窗口（交易日）
    adaptive_trend_window: int = 60               # 趋势 / 均线偏离窗口
    adaptive_vol_window: int = 20                 # 年化波动率窗口
    adaptive_lookback_dd: int = 20                # 近期最大回撤观察窗口
    # 连续因子倾斜灵敏度（牛巿偏动量、高波动 / 回撤偏质量与低波）
    tilt_quality: float = 0.04
    tilt_valuation: float = 0.03
    tilt_low_vol: float = 0.04
    tilt_momentum: float = 0.06
    # 总仓中枢对行情的灵敏度（连续调制，无硬性上下限）
    pos_trend_sens: float = 0.50                  # 趋势每 +1σ 中枢 ×(1+0.5)
    pos_vol_sens: float = 0.60                    # 波动率每 +1 中枢 ×(1-0.6)
    pos_dd_sens: float = 1.20                      # 近期回撤每 +1 中枢 ×(1-1.2)
    # 单股 / 单行业上限对波动的灵敏度（高波动更分散）
    cap_vol_sens: float = 0.50                     # 单股上限随波动下调幅度
    cap_ind_vol_sens: float = 0.40                 # 单行业上限随波动下调幅度
    # 回测参数搜索网格（止损 / 止盈 / 总仓中枢建议 / 单股上限）
    backtest_sl_grid: Tuple[float, ...] = (0.02, 0.03, 0.04)
    backtest_tp1_grid: Tuple[float, ...] = (0.06, 0.08, 0.10)
    backtest_tp2_grid: Tuple[float, ...] = (0.12, 0.15, 0.18)
    backtest_base_grid: Tuple[float, ...] = (0.50, 0.60, 0.70)
    backtest_stock_grid: Tuple[float, ...] = (0.04, 0.05, 0.06)

    # ===================== 四维学习驱动（量价+政策+消息+情绪） =====================
    # 彻底摒弃「纯量价单因子」学习主线：四类因子并列为核心决策输入，共同参与
    # 策略学习、盈亏归因与参数优化。学习总目标 = 风险调整后收益最大化（夏普优先）。
    # 禁止照搬美股纯技术驱动逻辑、禁止脱离 A 股「政策/消息/情绪驱动」特性的过度拟合。
    enable_four_dim: bool = True                 # 四维学习总开关
    # 四维顶层权重（量价 / 政策 / 消息 / 情绪），学习系统会连续再平衡，和为 1
    four_dim_weights: Dict[str, float] = field(default_factory=lambda: {
        "price": 0.40, "policy": 0.20,
        "news": 0.15, "sentiment": 0.25,
    })
    # 四维权重再平衡学习率（独立于因子子权重 lr，仅作用于顶层四维权重）
    four_dim_lr: float = 0.12

    # ---------- 政策面因子 ----------
    # 政策事件日历（内置样例，真实接入时替换为数据源推送）。level: 国家/部委/行业。
    # strength: -1(调控利空)~+1(扶持利好)；window_days: 影响持续周期估计。
    policy_events: Tuple[dict, ...] = field(default_factory=lambda: (
        {"id": "p1", "level": "national", "strength": 0.9, "window_days": 20,
         "tag": "货币宽松"},
        {"id": "p2", "level": "national", "strength": 0.7, "window_days": 15,
         "tag": "产业扶持"},
        {"id": "p3", "level": "ministry", "strength": -0.4, "window_days": 10,
         "tag": "行业规范"},
        {"id": "p4", "level": "industry", "strength": 0.5, "window_days": 8,
         "tag": "行业利好"},
    ))
    policy_level_weight: Dict[str, float] = field(default_factory=lambda: {
        "national": 1.0, "ministry": 0.6, "industry": 0.35,
    })
    policy_weight_sens: float = 0.30             # 政策强度→四维政策权重灵敏度
    policy_pos_boost: float = 0.15               # 政策利好窗口期总仓中枢上浮上限
    policy_tp_relax: float = 0.04                # 政策利好窗口期首档止盈放宽幅度

    # ---------- 消息面因子 ----------
    # 消息流（内置样例，真实接入时替换为资讯源）。nature: 利好/利空/中性；
    # magnitude: 0~1 冲击量级；half_life_days: 影响半衰期。
    news_flow: Tuple[dict, ...] = field(default_factory=lambda: (
        {"id": "n1", "nature": "bullish", "magnitude": 0.8,
         "half_life_days": 3, "tag": "行业景气超预期"},
        {"id": "n2", "nature": "bearish", "magnitude": 0.7,
         "half_life_days": 3, "tag": "利空公告"},
        {"id": "n3", "nature": "bullish", "magnitude": 0.5,
         "half_life_days": 2, "tag": "外围市场传导利好"},
        {"id": "n4", "nature": "bearish", "magnitude": 0.9,
         "half_life_days": 4, "tag": "突发事件冲击"},
    ))
    news_weight_sens: float = 0.25               # 消息净冲击→四维消息权重灵敏度
    major_news_magnitude: float = 0.8            # ≥此量级视为「重磅消息」，触发降频与风控升级
    major_news_freq_cut: float = 0.5             # 重磅消息窗口期新开仓频率折扣（降到 50%）
    # 政策/消息行业命中对选股得分的加成上限（得分尺度 0~1，0.15 约当于
    # 一个因子子分的 1/3，可改变排序但不颠覆量价主导；加成幅度再乘以
    # 四维学习权重——「环境影响多大」由归因学习决定，此处只定标）
    env_score_sens: float = 0.15

    # ---------- 情绪面因子 ----------
    # 情绪指标采集项（从行情/市场数据派生），每项带权重，合成情绪指数 [-1,1]。
    sentiment_indicators: Tuple[str, ...] = (
        "turnover", "advance_decline", "northbound", "margin",
        "amplitude_rate", "limit_up_down", "连板高度", "panic_euphoria",
    )
    sentiment_indicator_weights: Dict[str, float] = field(default_factory=lambda: {
        "turnover": 0.15, "advance_decline": 0.20, "northbound": 0.15,
        "margin": 0.10, "amplitude_rate": 0.10, "limit_up_down": 0.15,
        "连板高度": 0.10, "panic_euphoria": 0.15,
    })
    # 情绪周期阈值：冰点<=-0.5、恐慌<=-0.2、修复∈(-0.2,0.3)、亢奋>=0.5
    sentiment_panic_thr: float = -0.2
    sentiment_euphoria_thr: float = 0.5
    sentiment_ice_thr: float = -0.5
    sentiment_weight_sens: float = 0.28         # 情绪强度→四维情绪权重灵敏度
    panic_pos_cut: float = 0.20                  # 情绪恐慌期总仓中枢下调上限
    panic_sl_tighten: float = 0.015             # 情绪恐慌期止损收紧幅度（叠加到基准）
    euphoria_tp_relax: float = 0.03              # 情绪亢奋期止盈放宽（让利润奔跑）

    # ---------- 策略类型自动切换 ----------
    # 三类策略：趋势跟踪 / 反转交易 / 网格交易；学习系统动态分配资金权重（和为 1）。
    strategy_types: Tuple[str, ...] = ("trend_follow", "reversal", "grid")
    strategy_weights: Dict[str, float] = field(default_factory=lambda: {
        "trend_follow": 0.50, "reversal": 0.25, "grid": 0.25,
    })
    strategy_type_lr: float = 0.10               # 策略权重再平衡学习率
    # 各环境偏好：政策驱动期偏趋势、情绪博弈期偏反转、震荡无主线偏网格
    env_strategy_pref: Dict[str, Tuple[str, float]] = field(default_factory=lambda: {
        "policy_bull": ("trend_follow", 0.70),
        "sentiment_panic": ("reversal", 0.55),
        "oscillation": ("grid", 0.50),
    })

    # ---------- 风控联动升级（学习模块主动触发） ----------
    risk_escalate_on_policy: bool = True         # 重大政策落地前提升熔断敏感度
    risk_escalate_on_extreme_sentiment: bool = True  # 极端情绪（亢奋/恐慌）升级
    risk_escalate_on_major_news: bool = True     # 重磅消息发布前升级
    dd_escalate_mult: float = 1.5                # 升级时回撤触发阈值乘以该系数（更敏感）

    # ---------- 全场景回测验证 ----------
    scenario_backtest_on: bool = True            # 优化后须经全场景回测验证方可生效
    scenario_policy_days: int = 30               # 政策事件场景回测窗口
    scenario_sentiment_days: int = 30            # 情绪周期场景回测窗口
    scenario_news_days: int = 20                 # 消息冲击场景回测窗口
    scenario_pass_min_sharpe: float = 0.5        # 验证通过最低夏普
    scenario_pass_max_dd: float = 0.20           # 验证通过最大回撤上限

    # ---------- 策略适配性档案 ----------
    profile_keep_days: int = 120                 # 环境-策略表现档案保留交易日
