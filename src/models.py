from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class HolderShare(BaseModel):
    address: str
    percent: float


class TokenMetrics(BaseModel):
    chain: str
    address: str
    symbol: str
    name: Optional[str]
    price_usd: Optional[float]
    price_change_5m: Optional[float] = None
    market_cap: Optional[float] = None
    liquidity_usd: Optional[float] = None
    pool_created_at: Optional[datetime] = None
    first_trade_at: Optional[datetime] = None  # 第一个K线的时间（真正的开盘时间）
    trades_5m: Optional[int] = None
    holders: Optional[int] = None
    top10_ratio: Optional[float] = None
    max_holder_ratio: Optional[float] = None
    top5: List[HolderShare] = Field(default_factory=list)
    extra: Dict[str, Any] = Field(default_factory=dict)


class FilterRange(BaseModel):
    min: Optional[float] = None
    max: Optional[float] = None

    def is_set(self) -> bool:
        return self.min is not None or self.max is not None


class FilterConfig(BaseModel):
    market_cap_usd: FilterRange = FilterRange()
    liquidity_usd: FilterRange = FilterRange()
    open_minutes: FilterRange = FilterRange()
    top10_ratio: FilterRange = FilterRange()
    holder_count: FilterRange = FilterRange()
    max_holder_ratio: FilterRange = FilterRange()
    trades_5m: FilterRange = FilterRange()


class ChainConfig(BaseModel):
    rpc_url: str
    explorer: Optional[str] = None



