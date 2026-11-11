"""全局配置：初始本金、手续费标准、A股交易规则参数。

量化交易专家审核要点：
- 手续费三费结构严格对齐行业平均（佣金/印花税/过户费）。
- 佣金双向收取且单笔最低 5 元；印花税仅卖出单边；过户费双向。
- 交易规则参数（整手、T+1、交易时段、板块涨跌幅）集中在此，便于回测调参。
"""
from dataclasses import dataclass, field
from typing import Dict

# 初始本金：5 万元（50000 元）
INITIAL_CAPITAL = 50000.0


@dataclass(frozen=True)
class FeeConfig:
    """手续费配置（单位：费率或元）。"""
    commission_rate: float = 0.00025   # 券商佣金：成交金额 0.025%，双向
    commission_min: float = 5.0        # 单笔佣金最低 5 元
    stamp_rate: float = 0.001          # 印花税：成交金额 0.1%，仅卖出单边
    transfer_rate: float = 0.00001     # 过户费：成交金额 0.001%，双向
    # 滑点：仅作用于市价单（限价单按限价精确成交，滑点为 0）。
    # 买入支付更高、卖出收到更低，模拟实盘不利成交；计入成交金额与费用基数。
    slippage_rate: float = 0.0005      # 市价单滑点 5bps（买 + / 卖 -）


@dataclass(frozen=True)
class TradingRuleConfig:
    """交易规则配置。"""
    lot_size: int = 100                # 最小交易单位：1 手 = 100 股
    t_plus: int = 1                    # T+1：当日买入下一交易日可卖

    # 交易时段（时, 分, 秒）
    morning_open: tuple = (9, 30, 0)
    morning_close: tuple = (11, 30, 0)
    afternoon_open: tuple = (13, 0, 0)
    afternoon_close: tuple = (15, 0, 0)

    # 各板块涨跌幅限制比例（稳定复利策略仅交易沪深主板，统一：
    # 普通主板 ±10%，ST/*ST ±5%）
    price_limit_pct: Dict[str, float] = field(default_factory=lambda: {
        "main": 0.10,        # 主板（沪 60* / 深 00* / 002 / 003）：±10%
        "star": 0.05,        # ST / *ST：±5%
    })
    default_limit_pct: float = 0.10


@dataclass(frozen=True)
class SimulatorConfig:
    """模拟器总配置。"""
    initial_capital: float = INITIAL_CAPITAL
    fee: FeeConfig = field(default_factory=FeeConfig)
    rules: TradingRuleConfig = field(default_factory=TradingRuleConfig)
    storage_dir: str = "data"
    # 行情数据源：westock（对接 westock-data 技能）| mock（离线演示）
    provider: str = "westock"
