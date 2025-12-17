from __future__ import annotations

from datetime import datetime
from typing import List, Tuple

from .models import FilterConfig, TokenMetrics
from .utils import check_range


def apply_filters(metrics: TokenMetrics, cfg: FilterConfig) -> Tuple[bool, List[str]]:
    reasons: List[str] = []

    checks = [
        ("market_cap_usd", metrics.market_cap, cfg.market_cap_usd),
        ("liquidity_usd", metrics.liquidity_usd, cfg.liquidity_usd),
        ("top10_ratio", metrics.top10_ratio, cfg.top10_ratio),
        ("holder_count", metrics.holders, cfg.holder_count),
        ("max_holder_ratio", metrics.max_holder_ratio, cfg.max_holder_ratio),
        ("trades_5m", metrics.trades_5m, cfg.trades_5m),
    ]

    for name, value, fr in checks:
        ok, msg = check_range(_convert_to_float(value), fr)
        if not ok:
            reasons.append(f"{name} {msg}")

    # pool open minutes
    # 优先使用第一个K线的时间（真正的开盘时间），如果没有则使用 pool_created_at
    if cfg.open_minutes.is_set():
        open_time = metrics.first_trade_at or metrics.pool_created_at
        if open_time is None:
            reasons.append("open_minutes missing")
        else:
            minutes = (datetime.utcnow() - open_time).total_seconds() / 60
            ok, msg = check_range(minutes, cfg.open_minutes)
            if not ok:
                reasons.append(f"open_minutes {msg}")

    return len(reasons) == 0, reasons


def _convert_to_float(value):
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None

