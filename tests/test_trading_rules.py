import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import datetime
from config.settings import TradingRuleConfig
from core.trading_rules import TradingRules


def _rules():
    return TradingRules(TradingRuleConfig())


def test_validate_lot():
    r = _rules()
    assert r.validate_lot(100) is True
    assert r.validate_lot(150) is False
    assert r.validate_lot(0) is False
    assert r.validate_lot(-100) is False


def test_price_limit_pct_unified():
    r = _rules()
    # 稳定复利策略仅交易沪深主板：普通主板 ±10%，ST/*ST ±5%
    assert r.price_limit_pct("sh600519") == 0.10      # 主板
    assert r.price_limit_pct("sz300750") == 0.10      # 创业板（统一按主板 10%）
    assert r.price_limit_pct("sh688981") == 0.10      # 科创板（统一按主板 10%）
    assert r.price_limit_pct("bj830799") == 0.10      # 北交所（统一按主板 10%）
    assert r.price_limit_pct("sh600000", "ST 某某") == 0.05  # ST


def test_compute_price_limit():
    r = _rules()
    ceil, floor = r.compute_price_limit("sh600519", 100.0)
    assert ceil == 110.0 and floor == 90.0


def test_validate_price_limit():
    r = _rules()
    assert r.validate_price_limit(100.0, 110.0, 90.0) is True
    assert r.validate_price_limit(120.0, 110.0, 90.0) is False
    # 无涨跌停数据时不限制
    assert r.validate_price_limit(999.0, 0.0, 0.0) is True


def test_is_trading_time():
    r = _rules()
    assert r.is_trading_time(datetime(2026, 9, 21, 10, 0, 0)) is True
    assert r.is_trading_time(datetime(2026, 9, 21, 11, 30, 0)) is True
    assert r.is_trading_time(datetime(2026, 9, 21, 12, 0, 0)) is False   # 午休
    assert r.is_trading_time(datetime(2026, 9, 21, 15, 0, 0)) is True
    assert r.is_trading_time(datetime(2026, 9, 21, 15, 1, 0)) is False   # 收盘后


if __name__ == "__main__":
    for k, v in list(globals().items()):
        if k.startswith("test_") and callable(v):
            v()
            print("OK", k)
