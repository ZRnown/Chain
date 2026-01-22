from __future__ import annotations

from datetime import datetime
from typing import List, Tuple

from .models import FilterConfig, TokenMetrics
from .utils import check_range


def apply_filters(metrics: TokenMetrics, cfg: FilterConfig, include_risk: bool = True) -> Tuple[bool, List[str]]:
    """
    应用所有筛选条件
    include_risk: 是否包含风险评分筛选（默认True）
    """
    reasons: List[str] = []

    # 基础筛选条件
    basic_checks = [
        ("market_cap_usd", metrics.market_cap, cfg.market_cap_usd),
        ("liquidity_usd", metrics.liquidity_usd, cfg.liquidity_usd),
        ("top10_ratio", metrics.top10_ratio, cfg.top10_ratio),
        ("holder_count", metrics.holders, cfg.holder_count),
        ("max_holder_ratio", metrics.max_holder_ratio, cfg.max_holder_ratio),
        ("trades_5m", metrics.trades_5m, cfg.trades_5m),
    ]

    for name, value, fr in basic_checks:
        ok, msg = check_range(_convert_to_float(value), fr)
        if not ok:
            reasons.append(f"{name} {msg}")

    # pool open minutes
    if cfg.open_minutes.is_set():
        open_time = metrics.first_trade_at or metrics.pool_created_at
        if open_time is None:
            reasons.append("open_minutes missing")
        else:
            minutes = (datetime.utcnow() - open_time).total_seconds() / 60
            ok, msg = check_range(minutes, cfg.open_minutes)
            if not ok:
                reasons.append(f"open_minutes {msg}")

    # 风险评分筛选（可选）
    if include_risk:
        risk_checks = [
            ("sol_sniffer_score", metrics.sol_sniffer_score, cfg.sol_sniffer_score),
            ("token_sniffer_score", metrics.token_sniffer_score, cfg.token_sniffer_score),
        ]
        for name, value, fr in risk_checks:
            ok, msg = check_range(_convert_to_float(value), fr)
            if not ok:
                reasons.append(f"{name} {msg}")

    return len(reasons) == 0, reasons


def apply_basic_filters(metrics: TokenMetrics, cfg: FilterConfig) -> Tuple[bool, List[str]]:
    """应用基础筛选条件（不包含风险评分）"""
    return apply_filters(metrics, cfg, include_risk=False)


def apply_risk_filters(metrics: TokenMetrics, cfg: FilterConfig) -> Tuple[bool, List[str]]:
    """
    只应用风险评分筛选条件
    注意：如果 API 没有返回数据（值为 None），则跳过该筛选条件（不过滤）
    """
    reasons: List[str] = []

    risk_checks = [
        ("sol_sniffer_score", metrics.sol_sniffer_score, cfg.sol_sniffer_score),
        ("token_sniffer_score", metrics.token_sniffer_score, cfg.token_sniffer_score),
    ]

    for name, value, fr in risk_checks:
        # 如果没有设置筛选条件，跳过
        if not fr.is_set():
            continue
        # 如果 API 没有返回数据（值为 None），跳过该筛选条件（不过滤）
        float_value = _convert_to_float(value)
        if float_value is None:
            continue
        # 正常检查范围
        ok, msg = check_range(float_value, fr)
        if not ok:
            reasons.append(f"{name} {msg}")

    return len(reasons) == 0, reasons


def need_risk_check(cfg: FilterConfig) -> bool:
    """检查是否需要进行风险评分筛选"""
    return cfg.sol_sniffer_score.is_set() or cfg.token_sniffer_score.is_set()


def _convert_to_float(value):
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None

