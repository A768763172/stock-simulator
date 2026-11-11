"""行情展示解析（看板只读消费 live_prices.json 快照，纯函数，不依赖 streamlit）。

提供两件事：
1) resolve_display_price(live, code)：把行情快照解析为展示所需字段，并判定数据就绪状态
   （READY / NOT_READY / SUSPENDED）。空快照 / 异常价一律返回 NOT_READY，
   杜绝「静默回退 Mock 静态价」造成的展示价与成交判定价两套数据偏差。
2) market_phase_label(now)：返回交易阶段中文标签（盘前、集合竞价、交易中 · 连续竞价、
   午休、休市）。本系统撮合引擎仅在连续竞价时段执行；9:00–9:15 为盘前、9:15–9:30 为
   集合竞价准备时段，不误标为「午休」。

所有展示价旁均携带 updated_at 时间戳与 source 数据源状态，便于看板显式标注时效。
"""
from datetime import datetime, time as dtime
from typing import Dict, Optional


def resolve_display_price(live: Dict, code: str) -> Dict:
    """解析单个标的的展示行情。

    返回字段：
      price(float|None), ready(bool), status("READY"|"NOT_READY"|"SUSPENDED"),
      name, industry, prev_close, open, high, low, volume, change_pct,
      ceiling, floor, limit_hit("UP"|"DOWN"|"NONE"),
      is_suspended(bool), updated_at(str), source(str), warn(str|None)
    """
    entry = (live or {}).get(code)
    if not entry or not entry.get("price"):
        # 空快照 / 异常价：显式「行情数据未就绪」，绝不回退虚构演示价
        return {
            "price": None, "ready": False, "status": "NOT_READY",
            "name": (entry or {}).get("name", code),
            "industry": (entry or {}).get("industry", "其他"),
            "prev_close": 0.0, "open": 0.0, "high": 0.0, "low": 0.0,
            "volume": 0.0, "change_pct": 0.0, "ceiling": 0.0, "floor": 0.0,
            "limit_hit": "NONE",
            "is_suspended": bool((entry or {}).get("is_suspended", False)),
            "updated_at": (entry or {}).get("updated_at", ""),
            "source": (entry or {}).get("source", ""),
            "warn": "行情数据未就绪",
        }
    is_suspended = bool(entry.get("is_suspended", False))
    ceiling = float(entry.get("ceiling", 0) or 0)
    floor = float(entry.get("floor", 0) or 0)
    price = float(entry["price"])
    limit_hit = "NONE"
    if not is_suspended:
        if ceiling > 0 and abs(price - ceiling) < 1e-6:
            limit_hit = "UP"
        elif floor > 0 and abs(price - floor) < 1e-6:
            limit_hit = "DOWN"
    return {
        "price": price, "ready": True,
        "status": "SUSPENDED" if is_suspended else "READY",
        "name": entry.get("name", code),
        "industry": entry.get("industry", "其他"),
        "prev_close": float(entry.get("prev_close", 0) or 0),
        "open": float(entry.get("open", 0) or 0),
        "high": float(entry.get("high", 0) or 0),
        "low": float(entry.get("low", 0) or 0),
        "volume": float(entry.get("volume", 0) or 0),
        "change_pct": float(entry.get("change_pct", 0) or 0),
        "ceiling": ceiling, "floor": floor,
        "limit_hit": limit_hit, "is_suspended": is_suspended,
        "updated_at": entry.get("updated_at", ""),
        "source": entry.get("source", ""),
        "warn": entry.get("_warn"),
    }


def market_phase_label(now: Optional[datetime] = None) -> Dict[str, str]:
    """返回交易阶段中文标签与刷新提示。

    口径声明：本系统撮合引擎仅在连续竞价时段（9:30–11:30、13:00–15:00）执行；
    9:00–9:15 为盘前、9:15–9:30 为集合竞价准备时段，该时段不撮合，不误标为「午休」。
    """
    now = now or datetime.now()
    if now.weekday() >= 5:
        return {"state": "CLOSED", "label": "休市 · 收盘总结", "refresh": "60s"}
    t = now.time()
    if dtime(9, 30) <= t <= dtime(11, 30) or dtime(13, 0) <= t <= dtime(15, 0):
        return {"state": "LIVE", "label": "交易中 · 连续竞价", "refresh": "3s"}
    if dtime(9, 15) <= t < dtime(9, 30):
        return {"state": "PRE", "label": "集合竞价", "refresh": "60s"}
    if dtime(9, 0) <= t < dtime(9, 15):
        return {"state": "PRE", "label": "盘前", "refresh": "60s"}
    if dtime(11, 30) < t < dtime(13, 0):
        return {"state": "PAUSE", "label": "午休", "refresh": "60s"}
    return {"state": "CLOSED", "label": "休市 · 收盘总结", "refresh": "60s"}
