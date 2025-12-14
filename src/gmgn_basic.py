from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any, Dict, Optional

import tls_client
from fake_useragent import UserAgent
from tenacity import retry, stop_after_attempt, wait_exponential

from .models import TokenMetrics

logger = logging.getLogger("ca_filter_bot.gmgn_basic")


class GMGNBasicFetcher:
    """
    轻量版 GMGN 抓取器，复用 gmgn_complete_fetcher.py 的基础接口逻辑：
    - 仅调用 /api/v1/mutil_window_token_info
    - 兼容秒/毫秒时间戳
    - 支持重试机制
    - 尽量少字段，速度快，适合并行调用
    """

    BASE_URL = "https://gmgn.ai"

    def __init__(self, extra_headers: Optional[Dict[str, str]] = None):
        self.session = tls_client.Session(
            client_identifier="chrome_124",
            random_tls_extension_order=True,
        )
        self.session.timeout_seconds = 20
        self.extra_headers = extra_headers or {}

    def _headers(self, chain_code: str) -> Dict[str, str]:
        try:
            ua = UserAgent().random
        except Exception:
            ua = (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            )
        headers = {
            "Host": "gmgn.ai",
            "accept": "application/json, text/plain, */*",
            "accept-language": "en-US,en;q=0.9",
            "referer": f"https://gmgn.ai/?chain={chain_code}",
            "user-agent": ua,
            "Content-Type": "application/json",
        }
        headers.update(self.extra_headers)
        return headers

    def _safe_float(self, value: Any) -> float:
        try:
            if value is None:
                return 0.0
            return float(value)
        except Exception:
            return 0.0

    def _normalize_timestamp(self, ts: Any) -> Optional[datetime]:
        """兼容秒/毫秒的时间戳，无法解析时返回 None"""
        try:
            if ts is None:
                return None
            if isinstance(ts, str):
                ts = ts.strip()
                if not ts:
                    return None
                ts = float(ts)
            if ts > 1e12:  # 毫秒
                ts = ts / 1000.0
            return datetime.fromtimestamp(ts)
        except Exception:
            return None

    def _to_metrics(self, chain: str, address: str, basic: Dict[str, Any]) -> TokenMetrics:
        """完全按照 gmgn_complete_fetcher.py 的逻辑提取数据"""
        # 1. 价格处理（完全一致）
        raw_price = basic.get("price")
        price = 0.0
        if isinstance(raw_price, dict):
            price = self._safe_float(raw_price.get("price"))
        else:
            price = self._safe_float(raw_price)
        
        price_obj = basic.get("price") or {}
        pool_obj = basic.get("pool") or {}
        dev_obj = basic.get("dev") or {}
        
        # 2. 市值计算（完全一致）
        total_supply = self._safe_float(basic.get("total_supply"))
        market_cap = self._safe_float(basic.get("market_cap"))
        if market_cap == 0 and price > 0 and total_supply > 0:
            market_cap = price * total_supply

        # 3. 池子大小（完全一致）
        liquidity = self._safe_float(pool_obj.get("liquidity"))

        # 4. 开盘时间（完全一致）
        ts_candidates = [
            basic.get("open_timestamp"),
            basic.get("launch_time"),
            pool_obj.get("open_timestamp") if isinstance(pool_obj, dict) else None,
            price_obj.get("open_timestamp") if isinstance(price_obj, dict) else None,
        ]
        open_dt = None
        for ts in ts_candidates:
            open_dt = self._normalize_timestamp(ts)
            if open_dt:
                break

        # 5. 前十持仓（完全一致：直接获取，不做百分比转换）
        top10_raw = dev_obj.get("top_10_holder_rate")
        top10_ratio = self._safe_float(top10_raw)
        # 如果值 > 1，说明是百分比形式（如14.98），需要除以100转换为小数（0.1498）
        if top10_ratio > 1:
            top10_ratio = top10_ratio / 100.0
        # 如果为0，保持0.0，不要返回None

        # 6. 5分钟交易（完全一致）
        trades_5m = 0
        raw_swaps = basic.get("price", {})
        if isinstance(raw_swaps, dict):
            swaps = raw_swaps.get("swaps_5m")
            trades_5m = int(swaps or 0)

        # 7. 最大持仓（完全一致：使用前十的一半）
        max_holder_ratio = top10_ratio / 2.0 if top10_ratio > 0 else 0.0

        return TokenMetrics(
            chain=chain,
            address=address,
            symbol=basic.get("symbol", "") or "",
            name=basic.get("name"),
            price_usd=price,
            price_change_5m=self._safe_float(price_obj.get("price_5m")),
            market_cap=market_cap,
            liquidity_usd=liquidity,
            pool_created_at=open_dt,
            trades_5m=trades_5m,
            holders=int(basic.get("holder_count") or 0),
            top10_ratio=top10_ratio,  # 保持0.0而不是None
            max_holder_ratio=max_holder_ratio,  # 保持0.0而不是None
            extra={"source": "gmgn_basic"},
        )

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=5))
    def _fetch_sync(self, chain: str, address: str) -> Optional[TokenMetrics]:
        """同步获取，带重试机制"""
        chain_code = "sol" if chain.lower() in ("solana", "sol") else chain.lower()
        url = f"{self.BASE_URL}/api/v1/mutil_window_token_info"
        payload = {"chain": chain_code, "addresses": [address]}

        try:
            resp = self.session.post(url, json=payload, headers=self._headers(chain_code))
            if resp.status_code != 200:
                logger.warning(f"GMGN basic API returned {resp.status_code} for {address[:8]}")
                return None
            data = resp.json()
            if data.get("code") != 0 or not data.get("data"):
                logger.debug(f"GMGN basic API error: code={data.get('code')}, msg={data.get('msg')}")
                return None
            basic = data["data"][0]
            # 提取 pairAddress 用于图表
            pair_address = None
            if "pool" in basic and isinstance(basic["pool"], dict):
                pool = basic["pool"]
                pair_address = pool.get("pair_address") or pool.get("address") or pool.get("pairAddress")
                logger.debug(f"📊 Pool keys: {list(pool.keys())}, pair_address: {pair_address}")
            else:
                logger.debug(f"📊 No pool data in basic info, pool type: {type(basic.get('pool'))}")
            metrics = self._to_metrics(chain, address, basic)
            if pair_address and metrics:
                metrics.extra["pairAddress"] = pair_address
                logger.debug(f"✅ Extracted pairAddress: {pair_address[:16]}...")
            else:
                logger.warning(f"⚠️ Failed to extract pairAddress from GMGN basic info")
            return metrics
        except Exception as e:
            logger.warning(f"GMGN basic fetch error for {address[:8]}: {e}")
            raise  # 让 retry 机制处理

    async def fetch(self, chain: str, address: str) -> Optional[TokenMetrics]:
        """异步包装，避免阻塞事件循环"""
        return await asyncio.to_thread(self._fetch_sync, chain, address)

