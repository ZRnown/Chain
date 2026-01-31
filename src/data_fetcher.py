from __future__ import annotations

import logging
import random
import asyncio
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import httpx
from curl_cffi import requests as curl_requests
from tenacity import retry, stop_after_attempt, wait_exponential

from .models import TokenMetrics
from .gmgn_basic import GMGNBasicFetcher

logger = logging.getLogger("ca_filter_bot.data_fetcher")


DEX_TOKEN_URL = "https://api.dexscreener.com/latest/dex/tokens/{address}"
# 默认 API Key（如果 state 中没有设置，则使用这些默认值）
DEFAULT_SOL_SNIFFER_API_KEY = "gbnyroq3tsblgsm8c9nofinecwmecd"
DEFAULT_TOKEN_SNIFFER_API_KEY = "d69930b10c2b535db46463568fcfa38a7d9c5e95"


class DataFetcher:
    def __init__(
        self,
        session: Optional[httpx.AsyncClient] = None,
        gmgn_headers: Optional[Dict[str, str]] = None,
        get_api_key: Optional[callable] = None,
    ):
        # verify=False 仅用于调试，生产环境建议设为 True
        self.client = session or httpx.AsyncClient(timeout=15, verify=True)
        self.gmgn_headers = gmgn_headers or {}
        self.gmgn_basic = GMGNBasicFetcher(extra_headers=self.gmgn_headers)
        # 获取 API Key 的回调函数，签名: async def get_api_key(key_name: str) -> Optional[str]
        self._get_api_key = get_api_key

    async def fetch_all(self, chain: str, address: str) -> TokenMetrics:
        logger.info(f"🔍 Fetching data for {chain} - {address[:8]}...")

        # 1) 优先使用 GMGN 基础接口（tls_client，带重试，快速）
        metrics = await self.gmgn_basic.fetch(chain, address)
        if metrics:
            logger.info("✅ GMGN basic interface success")
            # 即使基础接口成功，也尝试获取 top holders 数据来更新 max_holder_ratio
            holders_data = await self._fetch_gmgn_top_holders(chain, address)
            if holders_data and holders_data.get("max_holder_ratio") is not None:
                metrics.max_holder_ratio = holders_data["max_holder_ratio"]
                logger.info(f"✅ Updated max_holder_ratio from top holders: {metrics.max_holder_ratio:.4f}")
        else:
            # 2) GMGN 基础接口失败，尝试全量接口（curl_cffi）
            logger.info("⚠️ GMGN basic failed, trying full interface...")
            metrics = await self._fetch_gmgn(chain, address)
            if not metrics:
                # 3) DexScreener 回退
                logger.info("⚠️ GMGN failed, switching to DexScreener...")
                metrics = await self._fetch_dex(chain, address)

        # 注意：风险评分不再在这里获取，改为在筛选通过后单独调用 fetch_risk_scores
        return metrics
    

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=0.5, min=0.5, max=4))
    async def _fetch_dex(self, chain: str, address: str) -> TokenMetrics:
        url = DEX_TOKEN_URL.format(address=address)
        r = await self.client.get(url)
        r.raise_for_status()
        data = r.json()
        pairs = data.get("pairs") or []
        
        if not pairs:
            raise ValueError("No pairs found on DexScreener")
            
        pair = _select_pair(pairs, chain)
        
        # 提取字段
        market_cap = _to_float(pair.get("fdv")) or _to_float(pair.get("marketCap"))
        liquidity = _to_float(pair.get("liquidity", {}).get("usd"))
        trades_5m = _to_int(pair.get("txns", {}).get("m5", {}).get("buys", 0)) + \
                    _to_int(pair.get("txns", {}).get("m5", {}).get("sells", 0))

        metrics = TokenMetrics(
            chain=pair.get("chainId", chain),
            address=address,
            symbol=pair.get("baseToken", {}).get("symbol", ""),
            name=pair.get("baseToken", {}).get("name"),
            price_usd=_to_float(pair.get("priceUsd")),
            price_change_5m=_to_float(pair.get("priceChange", {}).get("m5")),
            market_cap=market_cap,
            liquidity_usd=liquidity,
            trades_5m=trades_5m,
            pool_created_at=_to_datetime(pair.get("pairCreatedAt")),
            # 这里的 pairAddress 很重要，用于后续查 K 线
            extra={"pairAddress": pair.get("pairAddress"), "source": "dex"},
        )
        return metrics

    # --- GeckoTerminal OHLCV（1m，1小时内） ---
    def _gecko_network(self, chain: str) -> Optional[str]:
        """
        将内部链名称映射到 GeckoTerminal 的 network 参数。
        目前主要支持：Solana、BSC，其他链可按需扩展。
        """
        c = chain.lower()
        if c in ("sol", "solana"):
            return "solana"
        if c in ("bsc", "bscscan", "bnb"):
            return "bsc"
        if c in ("eth", "ethereum"):
            return "eth"
        return None
    
    async def fetch_chart_by_address(self, chain: str, address: str, minutes: int = 60) -> List[Dict[str, Any]]:
        """
        使用 GeckoTerminal API 获取 K 线数据。
        返回格式: {t, o, h, l, c, v}
        """
        return await self._fetch_gecko_ohlcv(chain, address, minutes)
    
    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=0.5, min=0.5, max=4))
    async def fetch_chart(self, metrics: TokenMetrics, minutes: int = 60) -> List[Dict[str, Any]]:
        """
        使用 GeckoTerminal API 获取 K 线数据（兼容旧接口）。
        """
        return await self.fetch_chart_by_address(metrics.chain, metrics.address, minutes)
    
    async def _fetch_gecko_ohlcv(self, chain: str, address: str, minutes: int) -> List[Dict[str, Any]]:
        """
        内部方法：使用 GeckoTerminal API 获取 1m K 线数据（最近 minutes 分钟，通常 60）。
        文档示例：
        1) /api/v2/networks/{network}/tokens/{token}/pools  -> 获取池子
        2) /api/v2/networks/{network}/pools/{pool}/ohlcv/minute?aggregate=1&limit=60&currency=usd
        """
        network = self._gecko_network(chain)
        if not network:
            raise ValueError(f"GeckoTerminal does not support chain: {chain}")
        
        # 1. 根据 token 找到池子（取第一个）
        pools_url = f"https://api.geckoterminal.com/api/v2/networks/{network}/tokens/{address}/pools"
        logger.info(f"📊 Fetching GeckoTerminal pools for {address[:8]}... (network={network})")
        try:
            pools_resp = await self.client.get(pools_url, timeout=10)
            pools_resp.raise_for_status()
            pools_data = pools_resp.json()
        except Exception as e:
            logger.warning(f"⚠️ GeckoTerminal pools API failed: {e}")
            raise ValueError(f"GeckoTerminal: failed to fetch pools - {str(e)}")
        
        pool_list = pools_data.get("data") or []
        if not pool_list:
            raise ValueError("GeckoTerminal: no pools found for token")
        
        pool_attrs = pool_list[0].get("attributes") or {}
        pool_address = pool_attrs.get("address")
        if not pool_address:
            raise ValueError("GeckoTerminal: pool address missing in response")
        
        logger.info(f"✅ GeckoTerminal pool selected: {pool_address}")
        
        # 2. 获取该池子的分钟 OHLCV（limit=minutes，最多60）
        limit = min(max(minutes, 1), 60)
        ohlcv_url = (
            f"https://api.geckoterminal.com/api/v2/networks/{network}/pools/"
            f"{pool_address}/ohlcv/minute?aggregate=1&limit={limit}&currency=usd"
        )
        logger.info(f"📊 Fetching GeckoTerminal OHLCV (1m, last {limit} bars)...")
        try:
            k_resp = await self.client.get(ohlcv_url, timeout=10)
            k_resp.raise_for_status()
            k_data = k_resp.json()
        except Exception as e:
            logger.warning(f"⚠️ GeckoTerminal OHLCV API failed: {e}")
            raise ValueError(f"GeckoTerminal: failed to fetch OHLCV - {str(e)}")
        
        attrs = (k_data.get("data") or {}).get("attributes") or {}
        ohlcv_list = attrs.get("ohlcv_list") or []
        if not ohlcv_list:
            raise ValueError("GeckoTerminal: ohlcv_list is empty")
        
        bars: List[Dict[str, Any]] = []
        for item in ohlcv_list:
            # 预期格式: [timestamp, open, high, low, close, volume]
            if not isinstance(item, (list, tuple)) or len(item) < 5:
                logger.debug(f"⚠️ Skipping invalid Gecko bar: {item}")
                continue
            ts = item[0]
            o, h, l, c = item[1], item[2], item[3], item[4]
            v = item[5] if len(item) > 5 else 0
            try:
                bars.append(
                    {
                        "t": int(ts),        # 秒级时间戳
                        "o": float(o),
                        "h": float(h),
                        "l": float(l),
                        "c": float(c),
                        "v": float(v),
                    }
                )
            except Exception:
                logger.debug(f"⚠️ Failed to convert Gecko bar: {item}")
                continue
        
        if not bars:
            raise ValueError("GeckoTerminal: no valid bars after conversion")
        
        # Gecko 返回通常是按时间升序或降序，这里统一按时间排序
        bars.sort(key=lambda x: x["t"])
        
        # 只保留最近60根K线（1小时）
        if len(bars) > 60:
            bars = bars[-60:]
            logger.info(f"⚠️ GeckoTerminal returned {len(bars)} bars, keeping only last 60 bars")
        
        logger.info(f"✅ GeckoTerminal OHLCV: fetched {len(bars)} bars "
                    f"(from {bars[0]['t']} to {bars[-1]['t']})")
        return bars

    def _get_gmgn_headers(self, referer_path: str) -> Dict[str, str]:
        """构造高仿浏览器头（参考用户提供的方案）"""
        # 随机化 User-Agent
        user_agents = [
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ]
        ua = random.choice(user_agents)
        
        # 合并用户提供的 headers
        headers = {
            "User-Agent": ua,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
            "Origin": "https://gmgn.ai",
            "Referer": f"https://gmgn.ai{referer_path}",
            "Sec-Ch-Ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
            "Sec-Ch-Ua-Mobile": "?0",
            "Sec-Ch-Ua-Platform": '"Windows"',
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
        }
        
        # 如果用户提供了自定义 headers（如 Cookie），合并进去
        if self.gmgn_headers:
            headers.update(self.gmgn_headers)
        
        return headers

    async def _fetch_gmgn_token_info(self, chain: str, address: str, attempt: int = 0) -> Optional[Dict[str, Any]]:
        """请求主接口：/defi/quotation/v1/tokens/sol/{address} - 获取价格、市值等"""
        # 可用的浏览器指纹列表（用于重试时切换）
        fingerprints = ["chrome110", "chrome120", "chrome116", "safari15_3", "safari15_5"]
        
        chain_code = "sol" if chain.lower() == "solana" else "eth"
        if chain.lower() == "bsc":
            chain_code = "bsc"
        
        url = f"https://gmgn.ai/defi/quotation/v1/tokens/{chain_code}/{address}"
        headers = self._get_gmgn_headers(f"/{chain_code}/token/{address}")
        
        try:
            # 使用 curl_cffi 的指纹绕过 Cloudflare，失败时切换指纹
            fingerprint = fingerprints[attempt % len(fingerprints)]
            logger.info(f"🔐 Fetching GMGN token info: {url} (attempt {attempt + 1}, fingerprint: {fingerprint})")
            resp = await asyncio.to_thread(
                curl_requests.get,
                url,
                headers=headers,
                impersonate=fingerprint,
                timeout=10
            )
            
            if resp.status_code == 200:
                data = resp.json()
                
                if data.get("code") == 0:
                    token = data.get("data", {}).get("token", {})
                    if token:
                        logger.info(f"✅ GMGN token info fetched: {token.get('symbol', 'N/A')}")
                        return token
                    else:
                        logger.warning(f"⚠️  GMGN token data is empty")
                else:
                    logger.warning(f"⚠️  GMGN API error: code={data.get('code')}, msg={data.get('msg')}")
            elif resp.status_code == 403:
                logger.warning(f"🚫 GMGN Token Info 403 Blocked (attempt {attempt + 1})")
                # 403错误，切换指纹重试
                if attempt < len(fingerprints) - 1:
                    logger.info(f"🔄 Switching fingerprint due to 403")
                    return await self._fetch_gmgn_token_info(chain, address, attempt + 1)
            elif resp.status_code == 429:
                logger.warning(f"🚫 GMGN Token Info 429 Rate Limit (attempt {attempt + 1})")
                # 429错误，切换指纹重试
                if attempt < len(fingerprints) - 1:
                    logger.info(f"🔄 Switching fingerprint due to 429")
                    return await self._fetch_gmgn_token_info(chain, address, attempt + 1)
            else:
                logger.warning(f"⚠️  GMGN Token Info HTTP {resp.status_code} (attempt {attempt + 1})")
                # 其他错误也尝试切换指纹
                if resp.status_code >= 400 and attempt < len(fingerprints) - 1:
                    logger.info(f"🔄 Switching fingerprint due to HTTP {resp.status_code}")
                    return await self._fetch_gmgn_token_info(chain, address, attempt + 1)
        except Exception as e:
            logger.warning(f"❌ GMGN Token Info Error: {type(e).__name__}: {e} (attempt {attempt + 1})")
            # 异常时也尝试切换指纹重试
            if attempt < len(fingerprints) - 1:
                logger.info(f"🔄 Switching fingerprint due to exception")
                return await self._fetch_gmgn_token_info(chain, address, attempt + 1)
        
        return None
    
    async def _fetch_gmgn_basic_info(self, chain: str, address: str, attempt: int = 0) -> Optional[Dict[str, Any]]:
        """
        备用方案：获取基础信息（你已经能获取到的接口）
        接口: /api/v1/mutil_window_token_info
        """
        # 可用的浏览器指纹列表（用于重试时切换）
        fingerprints = ["chrome110", "chrome120", "chrome116", "safari15_3", "safari15_5"]
        
        chain_code = "sol" if chain.lower() == "solana" else "eth"
        if chain.lower() == "bsc":
            chain_code = "bsc"
        
        url = f"https://gmgn.ai/api/v1/mutil_window_token_info"
        headers = self._get_gmgn_headers(f"/?chain={chain_code}")
        # POST 请求需要 content-type
        headers["Content-Type"] = "application/json"
        payload = {"chain": chain_code, "addresses": [address]}
        
        try:
            fingerprint = fingerprints[attempt % len(fingerprints)]
            logger.info(f"🔐 Fetching GMGN basic info (backup): {url} (attempt {attempt + 1}, fingerprint: {fingerprint})")
            resp = await asyncio.to_thread(
                curl_requests.post,
                url,
                headers=headers,
                json=payload,
                impersonate=fingerprint,
                timeout=10
            )
            
            
            if resp.status_code == 200:
                data = resp.json()
                if data.get("code") == 0 and data.get("data"):
                    basic_info = data["data"][0] if data["data"] else None
                    if basic_info:
                        logger.info(f"✅ GMGN basic info (backup) fetched: {basic_info.get('symbol', 'N/A')}")
                        return basic_info
                else:
                    # API返回错误，尝试切换指纹重试
                    if attempt < len(fingerprints) - 1:
                        logger.info(f"🔄 Switching fingerprint due to API error code={data.get('code')}")
                        return await self._fetch_gmgn_basic_info(chain, address, attempt + 1)
            elif resp.status_code in (403, 429, 401):
                logger.warning(f"🚫 GMGN Basic Info HTTP {resp.status_code} (attempt {attempt + 1})")
                # 403/429错误，切换指纹重试
                if attempt < len(fingerprints) - 1:
                    logger.info(f"🔄 Switching fingerprint due to HTTP {resp.status_code}")
                    return await self._fetch_gmgn_basic_info(chain, address, attempt + 1)
        except Exception as e:
            logger.debug(f"❌ GMGN Basic Info Error: {e} (attempt {attempt + 1})")
            # 异常时也尝试切换指纹重试
            if attempt < len(fingerprints) - 1:
                logger.info(f"🔄 Switching fingerprint due to exception")
                return await self._fetch_gmgn_basic_info(chain, address, attempt + 1)
        
        return None

    async def _fetch_gmgn_top_holders(self, chain: str, address: str, attempt: int = 0) -> Optional[Dict[str, Any]]:
        """请求持仓接口：/vas/api/v1/token_holders/sol/{address} - 获取精确的 Top10 和 Max Holder（参考 Dragon）"""
        # 可用的浏览器指纹列表（用于重试时切换）
        fingerprints = ["chrome110", "chrome120", "chrome116", "safari15_3", "safari15_5"]
        
        chain_code = "sol" if chain.lower() == "solana" else "eth"
        if chain.lower() == "bsc":
            chain_code = "bsc"
        
        # 使用 Dragon 中验证过的接口地址
        url = f"https://gmgn.ai/vas/api/v1/token_holders/{chain_code}/{address}"
        params = {"orderby": "amount_percentage", "direction": "desc", "limit": 20}
        headers = self._get_gmgn_headers(f"/{chain_code}/token/{address}")
        
        try:
            fingerprint = fingerprints[attempt % len(fingerprints)]
            logger.debug(f"🔐 Fetching GMGN top holders (attempt {attempt + 1}, fingerprint: {fingerprint})")
            resp = await asyncio.to_thread(
                curl_requests.get,
                url,
                params=params,
                headers=headers,
                impersonate=fingerprint,
                timeout=10
            )
            
            if resp.status_code == 200:
                data = resp.json()
                # Dragon 使用的接口返回格式可能是 data.list 或 data.data.list
                holders_list = data.get("data", {}).get("list", []) or data.get("data", []) or data.get("list", [])
                
                if holders_list:
                    # 计算 Top 10 和 Max
                    # 注意：GMGN 返回的可能是百分比(如30.5)也可能是小数(0.305)，需要判断
                    top10_sum = 0.0
                    max_holder = 0.0
                    
                    for h in holders_list[:10]:
                        pct = float(h.get("amount_percentage", 0))
                        # 如果值 > 1，说明是百分比形式，需要除以100
                        if pct > 1:
                            pct = pct / 100
                        top10_sum += pct
                    
                    # 获取第二大持仓者的占比（而不是最大的）
                    if len(holders_list) >= 2:
                        # 第二大持仓者是索引1（索引0是最大的）
                        second_max_pct = float(holders_list[1].get("amount_percentage", 0))
                        if second_max_pct > 1:
                            second_max_pct = second_max_pct / 100
                        max_holder = second_max_pct
                    elif len(holders_list) == 1:
                        # 如果只有一个持仓者，使用它的值
                        max_pct = float(holders_list[0].get("amount_percentage", 0))
                        if max_pct > 1:
                            max_pct = max_pct / 100
                        max_holder = max_pct
                    else:
                        max_holder = 0.0
                    
                    logger.info(f"✅ GMGN top holders fetched: top10={top10_sum:.4f}, second_max={max_holder:.4f}")
                    return {
                        "top_10_ratio": top10_sum,
                        "max_holder_ratio": max_holder
                    }
                else:
                    # 没有数据，尝试切换指纹重试
                    if attempt < len(fingerprints) - 1:
                        logger.info(f"🔄 Switching fingerprint due to empty data")
                        return await self._fetch_gmgn_top_holders(chain, address, attempt + 1)
            elif resp.status_code in (403, 429, 401):
                logger.warning(f"🚫 GMGN Top Holders HTTP {resp.status_code} (attempt {attempt + 1})")
                # 403/429错误，切换指纹重试
                if attempt < len(fingerprints) - 1:
                    logger.info(f"🔄 Switching fingerprint due to HTTP {resp.status_code}")
                    return await self._fetch_gmgn_top_holders(chain, address, attempt + 1)
            else:
                logger.warning(f"⚠️  GMGN Top Holders HTTP {resp.status_code} (attempt {attempt + 1})")
                # 其他错误也尝试切换指纹
                if resp.status_code >= 400 and attempt < len(fingerprints) - 1:
                    logger.info(f"🔄 Switching fingerprint due to HTTP {resp.status_code}")
                    return await self._fetch_gmgn_top_holders(chain, address, attempt + 1)
        except Exception as e:
            logger.debug(f"❌ GMGN Top Holders Error: {e} (attempt {attempt + 1})")
            # 异常时也尝试切换指纹重试
            if attempt < len(fingerprints) - 1:
                logger.info(f"🔄 Switching fingerprint due to exception")
                return await self._fetch_gmgn_top_holders(chain, address, attempt + 1)
        
        return None

    async def _fetch_gmgn(self, chain: str, address: str) -> Optional[TokenMetrics]:
        """
        获取 GMGN 完整数据
        策略：
        1. 优先使用主接口
        2. 如果失败，使用备用基础接口
        3. 并行获取持仓数据
        """
        # 并行请求：主接口 + 持仓接口
        token_task = self._fetch_gmgn_token_info(chain, address)
        holders_task = self._fetch_gmgn_top_holders(chain, address)
        
        token_data, holders_data = await asyncio.gather(token_task, holders_task)
        
        # 如果主接口失败，尝试备用基础接口
        if not token_data:
            logger.info(f"⚠️  Main GMGN interface failed, trying backup basic interface...")
            basic_info = await self._fetch_gmgn_basic_info(chain, address)
            if basic_info:
                # 将基础信息转换为与主接口相同的格式
                token_data = self._convert_basic_to_token_format(basic_info)
                logger.info(f"✅ Using backup basic info")
        
        # 如果所有接口都失败
        if not token_data:
            logger.warning(f"🚫 GMGN all endpoints failed. Using DexScreener fallback.")
            return None
        
        # 数据提取与组装
        merged_data = {}
        
        # 1. 市值 (优先用 API 返回的，没有则计算)
        price = _to_float(token_data.get("price")) or 0
        mcap = _to_float(token_data.get("market_cap")) or 0
        if mcap == 0 and price > 0:
            total_supply = _to_float(token_data.get("total_supply")) or 0
            if total_supply > 0:
                mcap = price * total_supply
        merged_data["market_cap"] = mcap
        
        # 2. 池子大小
        merged_data["liquidity"] = _to_float(token_data.get("liquidity")) or 0
        
        # 3. 开盘时间
        open_ts = token_data.get("open_timestamp") or token_data.get("pool_creation_timestamp")
        merged_data["open_timestamp"] = open_ts
        merged_data["pool_creation_timestamp"] = open_ts
        
        # 4. CA 地址
        merged_data["address"] = token_data.get("address", address)
        merged_data["symbol"] = token_data.get("symbol", "")
        merged_data["name"] = token_data.get("name")
        
        # 5. 持有人数
        merged_data["holder_count"] = _to_int(token_data.get("holder_count"))
        
        # 6. 前十持仓占比 (优先用 holders 接口计算，没有则用 token 接口的 dev 字段)
        if holders_data and holders_data.get("top_10_ratio") is not None:
            merged_data["top_10_holder_rate"] = holders_data["top_10_ratio"]
        else:
            # 备用方案：从 token 接口的 dev 字段获取
            dev_data = token_data.get("dev", {})
            merged_data["top_10_holder_rate"] = _to_float(dev_data.get("top_10_holder_rate")) or 0
        
        # 7. 5分钟交易数
        # GMGN 的 swaps_5m 字段，如果没有则用 swaps（可能是24h的）
        merged_data["swaps_5m"] = _to_int(token_data.get("swaps_5m")) or _to_int(token_data.get("swaps")) or 0
        
        # 8. 最大持仓者占比
        if holders_data and holders_data.get("max_holder_ratio") is not None:
            merged_data["max_holder_ratio"] = holders_data["max_holder_ratio"]
        else:
            # 如果没有详细数据，尝试从 top_10_holder_rate 估算
            top10 = merged_data.get("top_10_holder_rate", 0)
            merged_data["max_holder_ratio"] = top10 / 3 if top10 > 0 else None
        
        # 其他字段
        merged_data["price"] = price
        merged_data["price_change_percent5m"] = _to_float(token_data.get("price_change_percent5m"))
        
        return self._gmgn_to_metrics(chain, address, merged_data)
    
    def _convert_basic_to_token_format(self, basic_info: Dict[str, Any]) -> Dict[str, Any]:
        """
        将基础接口返回的数据转换为与主接口相同的格式
        这样后续处理逻辑可以统一
        """
        price_data = basic_info.get("price", {})
        pool_data = basic_info.get("pool", {})
        dev_data = basic_info.get("dev", {})
        
        # 计算市值
        price = _to_float(price_data.get("price")) or 0
        total_supply = float(basic_info.get("total_supply", 0) or 0)
        market_cap = price * total_supply if price > 0 and total_supply > 0 else None
        
        return {
            "address": basic_info.get("address", ""),
            "symbol": basic_info.get("symbol", ""),
            "name": basic_info.get("name"),
            "price": price,
            "price_change_percent5m": _to_float(price_data.get("price_5m")),
            "market_cap": market_cap,
            "total_supply": total_supply,
            "liquidity": _to_float(pool_data.get("liquidity")),
            "open_timestamp": basic_info.get("open_timestamp"),
            "pool_creation_timestamp": pool_data.get("creation_timestamp"),
            "swaps_5m": price_data.get("swaps_5m", 0),
            "swaps": price_data.get("swaps_24h", 0),  # 24h 交易数
            "holder_count": basic_info.get("holder_count"),
            "top_10_holder_rate": _to_float(dev_data.get("top_10_holder_rate")),
            "max_holder_ratio": None,
        }

    def _gmgn_to_metrics(self, chain: str, address: str, t: Dict[str, Any]) -> TokenMetrics:
        """将 GMGN 数据转换为 TokenMetrics"""
        # 处理时间戳
        ts = t.get("open_timestamp") or t.get("pool_creation_timestamp")
        created = None
        if ts:
            try:
                created = datetime.fromtimestamp(ts, tz=timezone.utc).replace(tzinfo=None)
            except: 
                pass

        # 处理前10持仓占比（已经是小数形式，如 0.0082 = 0.82%）
        top10_ratio = _to_float(t.get("top_10_holder_rate"))
        
        # 处理最大持仓占比
        max_holder_ratio = _to_float(t.get("max_holder_ratio"))
        
        # 处理流动性（注意：可能是 SOL 而不是 USD）
        liquidity = _to_float(t.get("liquidity"))
        # TODO: 如果需要转换为 USD，需要获取 SOL 价格并乘以 liquidity
        
        return TokenMetrics(
            chain=chain, 
            address=address, 
            symbol=t.get("symbol", ""), 
            name=t.get("name"),
            price_usd=_to_float(t.get("price")),
            price_change_5m=_to_float(t.get("price_change_percent5m")),
            market_cap=_to_float(t.get("market_cap")),
            liquidity_usd=liquidity,  # 注意：可能需要转换为 USD
            pool_created_at=created,
            trades_5m=_to_int(t.get("swaps_5m")) or _to_int(t.get("swaps")) or 0,
            holders=_to_int(t.get("holder_count")),
            top10_ratio=top10_ratio,  # 已经是小数形式（0.0082 = 0.82%）
            max_holder_ratio=max_holder_ratio,  # 从 top holders 接口获取
            extra={"source": "gmgn"},
        )

    async def _gmgn_ratios(self, chain: str, address: str) -> Tuple[Optional[float], Optional[float]]:
        # 简化版单独获取 - 如果主接口失败，这里也失败
        return None, None

    async def fetch_risk_scores(self, metrics: TokenMetrics) -> None:
        """获取 SolSniffer 和 TokenSniffer 风险评分（公开方法，供外部调用）"""
        # 并行获取两个评分
        sol_task = self._fetch_sol_sniffer_score(metrics.chain, metrics.address)
        token_task = self._fetch_token_sniffer_score(metrics.chain, metrics.address)

        sol_score, token_score = await asyncio.gather(sol_task, token_task)

        metrics.sol_sniffer_score = sol_score
        metrics.token_sniffer_score = token_score

    async def _fetch_sol_sniffer_score(self, chain: str, address: str) -> Optional[float]:
        """获取 SolSniffer 风险评分 (0-100)"""
        try:
            if chain.lower() not in ("sol", "solana"):
                logger.debug(f"SolSniffer: chain={chain} not supported")
                return None

            # 获取 API key（优先从 state 获取，否则使用默认值）
            api_key = None
            if self._get_api_key:
                api_key = await self._get_api_key("sol_sniffer")
            if not api_key:
                api_key = DEFAULT_SOL_SNIFFER_API_KEY
            if not api_key:
                logger.warning("⚠️ SolSniffer API key not configured")
                return None

            # SolSniffer API v2.0 端点：GET /token/{address}
            url = f"https://solsniffer.com/api/v2/token/{address}"

            # 使用 httpx 客户端请求，API key 作为 Header
            headers = {"X-API-KEY": api_key}
            resp = await self.client.get(url, headers=headers, timeout=10)

            if resp.status_code == 200:
                data = resp.json()
                # 根据API文档，返回格式为 tokenData.score
                token_data = data.get("tokenData", {})
                score = token_data.get("score")
                if score is not None and isinstance(score, (int, float)):
                    logger.info(f"✅ SolSniffer score fetched: {score} (token={address[:8]}...)")
                    return float(score)
                else:
                    logger.warning(f"⚠️ SolSniffer API invalid response | token={address[:8]}... | data={str(data)[:200]}")
            else:
                # 详细显示失败信息
                key_hint = f"{api_key[:8]}...{api_key[-4:]}" if api_key and len(api_key) > 12 else "default"
                logger.warning(f"⚠️ SolSniffer API failed | status={resp.status_code} | token={address[:8]}... | key={key_hint} | resp={resp.text[:100]}")

        except Exception as e:
            logger.debug(f"Error fetching SolSniffer score: {e}")

        return None

    async def _fetch_token_sniffer_score(self, chain: str, address: str) -> Optional[float]:
        """获取 TokenSniffer 风险评分 (0-100)"""
        try:
            # TokenSniffer chain ID 映射
            token_sniffer_chain_map = {
                "bsc": 56,
                "ethereum": 1,
                "eth": 1,
                "polygon": 137,
                "matic": 137,
                "solana": "solana",  # 尝试调用，让 API 返回真实错误
                "sol": "solana",
            }

            sniffer_chain_id = token_sniffer_chain_map.get(chain.lower(), chain.lower())

            # 获取 API key
            api_key = None
            if self._get_api_key:
                api_key = await self._get_api_key("token_sniffer")
            if not api_key:
                api_key = DEFAULT_TOKEN_SNIFFER_API_KEY
            if not api_key:
                logger.warning("⚠️ TokenSniffer API key not configured")
                return None

            url = f"https://tokensniffer.com/api/v2/tokens/{sniffer_chain_id}/{address}"

            params = {
                "apikey": api_key,
                "include_metrics": "true",
            }

            # pending 状态重试配置
            max_retries = 6  # 最多重试 6 次
            retry_interval = 5  # 每次等待 5 秒

            for attempt in range(max_retries + 1):
                resp = await self.client.get(url, params=params, timeout=10)

                if resp.status_code == 200:
                    data = resp.json()

                    # 检查是否是 pending 状态（代币正在分析中）
                    status = data.get("status")
                    if status == "pending":
                        if attempt < max_retries:
                            logger.info(f"⏳ TokenSniffer API: token={address[:8]}... 正在分析中，{retry_interval}秒后重试 ({attempt + 1}/{max_retries})")
                            await asyncio.sleep(retry_interval)
                            continue
                        else:
                            logger.warning(f"⚠️ TokenSniffer API: token={address[:8]}... 分析超时，已重试{max_retries}次")
                            return None

                    # 尝试获取评分 - 先检查顶层，再检查 metrics 和 tests
                    score = data.get("score")  # 顶层 score 字段

                    if score is None:
                        metrics = data.get("metrics") or data.get("data", {}).get("metrics", {})
                        if isinstance(metrics, dict):
                            score = metrics.get("score")

                    if score is None:
                        tests = data.get("tests", {})
                        if isinstance(tests, dict):
                            score = tests.get("score")
                    if score is not None and isinstance(score, (int, float)):
                        logger.info(f"✅ TokenSniffer score fetched: {score} (chain={chain}, token={address[:8]}...)")
                        return float(score)
                    else:
                        # 显示完整的返回数据用于调试
                        import json
                        logger.warning(f"⚠️ TokenSniffer API: 无评分数据 | chain={chain} | token={address[:8]}... | status={status}")
                        logger.warning(f"   完整返回: {json.dumps(data, ensure_ascii=False)}")
                        return None
                else:
                    # 详细显示失败信息
                    key_hint = f"{api_key[:8]}...{api_key[-4:]}" if api_key and len(api_key) > 12 else "default"
                    logger.warning(f"⚠️ TokenSniffer API failed | status={resp.status_code} | chain={chain} | token={address[:8]}... | key={key_hint} | resp={resp.text[:100]}")
                    return None

        except Exception as e:
            logger.debug(f"Error fetching TokenSniffer score: {e}")

        return None 



def _select_pair(pairs: List[Dict[str, Any]], chain: str) -> Dict[str, Any]:
    chain_lower = "solana" if chain.lower() == "sol" else chain.lower()
    filtered = [p for p in pairs if str(p.get("chainId", "")).lower() == chain_lower]
    target = filtered or pairs
    target.sort(key=lambda p: _to_float(p.get("liquidity", {}).get("usd") if isinstance(p.get("liquidity"), dict) else 0) or 0, reverse=True)
    return target[0]

def _to_float(v): 
    """转换为float，None返回None，0返回0.0"""
    if v is None:
        return None
    try:
        return float(v)
    except (ValueError, TypeError):
        return None
def _to_int(v): return int(v) if v is not None else None
def _to_datetime(v): 
    if not v: return None
    try: return datetime.fromtimestamp(int(v)/1000, tz=timezone.utc).replace(tzinfo=None)
    except: return None
