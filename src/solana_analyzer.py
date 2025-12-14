from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from .models import ChainConfig

logger = logging.getLogger("ca_filter_bot.solana_analyzer")

# 排除名单 (DEX, Router, Burn, MEV)
# 遇到这些地址作为 Sender 时，不视为老鼠仓分发源
WHITELIST = {
    "5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5wDbuXB",  # Raydium Authority
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",  # Token Program
    "11111111111111111111111111111111",  # System Program
    "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4",  # Jupiter
    "TSLvdd1pWpHVjahSpsvCXUbgwsL3JAcvokwaKt1eokM",  # Tensor / Marketplace
    "So11111111111111111111111111111111111111112",  # SOL
    "SysvarRent111111111111111111111111111111111",  # Rent Sysvar
    "SysvarC1ock11111111111111111111111111111111",  # Clock Sysvar
}

# 并发限制 (防止 RPC 429 报错)
SEM = asyncio.Semaphore(10)


class SolanaRoughAnalyzer:
    """
    使用资金同源分析（Funding Source Trace）计算老鼠仓和捆绑占比
    核心思路：从"猜时间"升级到"查资金"
    - Level 1: 抓取开盘前交易，找出早期买入者
    - Level 2: 对这些可疑地址，查它们的第一笔SOL是谁转进来的
    - Level 3: 自动剔除DEX、Router、MEV Bot等干扰项
    """

    def __init__(self, rpc_url: str, client):
        self.rpc_url = rpc_url
        self.client = client

    async def _rpc_call(self, method: str, params: list) -> Optional[dict]:
        """异步RPC调用，带并发限制"""
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": method,
            "params": params
        }
        async with SEM:  # 限制并发
            try:
                r = await self.client.post(self.rpc_url, json=payload, timeout=15)
                if r.status_code == 200:
                    return r.json().get("result")
            except Exception as e:
                logger.debug(f"RPC call failed {method}: {e}")
            return None

    async def _get_token_supply(self, mint_address: str) -> float:
        """获取代币总供应量"""
        data = await self._rpc_call("getTokenSupply", [mint_address])
        if data and 'value' in data:
            amount = float(data['value']['amount'])
            decimals = data['value'].get('decimals', 9)
            return amount / (10 ** decimals)
        return 0.0

    async def _get_largest_accounts(self, mint_address: str, limit: int = 20) -> List[dict]:
        """获取前N名持仓大户"""
        data = await self._rpc_call("getTokenLargestAccounts", [mint_address])
        if data and 'value' in data:
            return data['value'][:limit]
        return []

    async def _get_account_owner(self, pubkey: str) -> Optional[str]:
        """解析 Token Account 的真正 Owner"""
        data = await self._rpc_call("getAccountInfo", [pubkey, {"encoding": "jsonParsed"}])
        try:
            result = data.get("value")
            if not result:
                return None
            parsed = result.get("data", {}).get("parsed", {})
            info = parsed.get("info", {})
            return info.get("owner")
        except:
            return None

    async def _get_signatures(self, address: str, limit: int = 200, before: Optional[str] = None) -> List[dict]:
        """获取地址的交易签名列表"""
        params = [address, {"limit": limit}]
        if before:
            params[1]["before"] = before
        return await self._rpc_call("getSignaturesForAddress", params) or []

    async def _get_parsed_tx(self, signature: str) -> Optional[dict]:
        """获取解析后的交易详情"""
        return await self._rpc_call(
            "getTransaction",
            [signature, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}]
        )

    async def _analyze_funding_source(self, wallets: List[str]) -> Dict[str, List[str]]:
        """
        🕵️‍♂️ 资金溯源（高准确度核心）
        查这些钱包的第一笔交易，看 SOL 是谁给的。
        返回: {funding_source: [wallet1, wallet2, ...]}
        """
        funding_map = defaultdict(list)

        async def check_one(wallet: str):
            try:
                # 查最近50笔交易（假设是新钱包，第一笔通常在最近50笔内）
                sigs = await self._get_signatures(wallet, limit=50)
                if not sigs:
                    return

                # 取最早的一笔（通常是 Funding 或第一笔买入）
                earliest_sig = sigs[-1]["signature"]
                tx = await self._get_parsed_tx(earliest_sig)

                if not tx:
                    return

                # 分析谁转账给了这个钱包 SOL
                # 查找 SystemProgram Transfer
                try:
                    transaction = tx.get("transaction", {})
                    message = transaction.get("message", {})
                    instructions = message.get("instructions", [])

                    sender = "Unknown"
                    for instr in instructions:
                        parsed = instr.get("parsed", {})
                        if parsed.get("type") == "transfer" and parsed.get("program") == "system":
                            info = parsed.get("info", {})
                            if info.get("destination") == wallet:
                                sender = info.get("source")
                                break

                    if sender != "Unknown" and sender not in WHITELIST:
                        funding_map[sender].append(wallet)
                        logger.debug(f"  💰 {wallet[:8]}... funded by {sender[:8]}...")
                except Exception as e:
                    logger.debug(f"  ⚠️ Failed to parse funding for {wallet[:8]}: {e}")
            except Exception as e:
                logger.debug(f"  ⚠️ Error checking funding for {wallet[:8]}: {e}")

        # 并发检查所有钱包
        await asyncio.gather(*[check_one(w) for w in wallets])
        return funding_map

    async def analyze(self, mint_address: str) -> Tuple[Optional[float], Optional[float]]:
        """
        分析代币，返回 (bundled_ratio, rat_ratio)
        返回值为小数形式（0.23 = 23%）
        """
        try:
            logger.info(f"🔍 Starting funding source trace analysis for {mint_address[:8]}...")

            # 1. 获取总供应量
            total_supply = await self._get_token_supply(mint_address)
            if total_supply == 0:
                logger.warning("Cannot get token supply")
                return None, None

            # 2. 获取早期交易（寻找开盘瞬间）
            logger.debug("  - 正在抓取早期交易...")
            sigs = await self._get_signatures(mint_address, limit=300)
            if not sigs:
                logger.warning("  ❌ 无法获取交易数据")
                return None, None

            # 倒序，找到开盘那几笔
            sigs.sort(key=lambda x: x.get("blockTime", 0))
            launch_time = sigs[0].get("blockTime", 0) if sigs else 0
            if launch_time == 0:
                logger.warning("  ❌ 无法确定开盘时间")
                return None, None

            logger.debug(f"  - 发现开盘时间戳: {launch_time} ({datetime.fromtimestamp(launch_time)})")

            # 3. 解析前100笔交易，提取买入者（开盘5分钟内）
            logger.debug("  - 解析交易行为，寻找狙击手...")
            early_buyers = set()
            suspicious_txs = []

            # 并发获取交易详情
            tasks = [self._get_parsed_tx(s["signature"]) for s in sigs[:100]]
            txs = await asyncio.gather(*tasks)

            for tx in txs:
                if not tx:
                    continue
                try:
                    meta = tx.get("meta", {})
                    bt = tx.get("blockTime", 0)
                    if bt == 0:
                        continue

                    # 谁买入了？(PostBalance > PreBalance)
                    # Solana RPC 返回格式：postTokenBalances 和 preTokenBalances
                    post_balances = meta.get("postTokenBalances", [])
                    pre_balances = meta.get("preTokenBalances", [])

                    # 创建余额映射（兼容不同的数据格式）
                    pre_balance_map = {}
                    for b in pre_balances:
                        if b.get("mint") == mint_address:
                            owner = b.get("owner")
                            if owner:
                                # 兼容不同的金额字段名
                                token_amount = b.get("uiTokenAmount", {}) or b.get("tokenAmount", {})
                                amount = token_amount.get("uiAmount") or token_amount.get("amount", 0)
                                if isinstance(amount, str):
                                    try:
                                        amount = float(amount)
                                    except:
                                        amount = 0
                                pre_balance_map[owner] = float(amount)

                    post_balance_map = {}
                    for b in post_balances:
                        if b.get("mint") == mint_address:
                            owner = b.get("owner")
                            if owner:
                                token_amount = b.get("uiTokenAmount", {}) or b.get("tokenAmount", {})
                                amount = token_amount.get("uiAmount") or token_amount.get("amount", 0)
                                if isinstance(amount, str):
                                    try:
                                        amount = float(amount)
                                    except:
                                        amount = 0
                                post_balance_map[owner] = float(amount)

                    # 找出余额增加的地址（买入者）
                    for owner, post_amt in post_balance_map.items():
                        if owner in WHITELIST:
                            continue
                        pre_amt = pre_balance_map.get(owner, 0)
                        if post_amt > pre_amt:
                            # 记录开盘5分钟内的买入者
                            time_diff = bt - launch_time
                            if 0 <= time_diff < 300:  # 5分钟 = 300秒
                                early_buyers.add(owner)
                                suspicious_txs.append({"owner": owner, "time": bt, "slot": tx.get("slot", 0)})
                                logger.debug(f"  🎯 Early buyer: {owner[:8]}... at {time_diff}s after launch")
                except Exception as e:
                    logger.debug(f"  ⚠️ Error parsing tx: {e}")
                    continue

            logger.debug(f"  - 锁定开盘狙击地址数: {len(early_buyers)}")

            # 4. 资金同源分析（最耗时但最准）
            # 为了速度，只取前20个疑似地址进行溯源
            logger.debug("  - 🕵️‍♂️ 执行资金同源追踪 (Funding Source Trace)...")
            sample_suspects = list(early_buyers)[:20]
            if not sample_suspects:
                logger.debug("  ⚠️ No early buyers found")
                return 0.0, 0.0

            funding_map = await self._analyze_funding_source(sample_suspects)

            # 5. 获取当前持仓大户（验证他们是否还没跑）
            logger.debug("  - 检查当前持仓分布...")
            top_accs = await self._get_largest_accounts(mint_address, limit=20)

            # 解析 Top Accounts 的 Owner
            top_owners = {}  # owner -> amount
            owner_tasks = [self._get_account_owner(acc["address"]) for acc in top_accs]
            owners_res = await asyncio.gather(*owner_tasks)

            for i, owner in enumerate(owners_res):
                if owner:
                    # 兼容不同的金额字段格式
                    acc_data = top_accs[i]
                    amount = acc_data.get("uiAmount") or acc_data.get("amount", 0)
                    if isinstance(amount, str):
                        try:
                            amount = float(amount)
                        except:
                            amount = 0
                    amt = float(amount)
                    if amt > 0:
                        top_owners[owner] = top_owners.get(owner, 0) + amt
                        logger.debug(f"  📊 Top holder: {owner[:8]}... holding {amt:.2f} tokens")

            # ================= 计算最终指标 =================

            # 🐭 计算老鼠仓占比 (Rat Ratio)
            # 定义：开盘5分钟买入，且目前在前20持仓中的人
            rat_holding_amount = 0.0
            confirmed_rats = []

            for owner, amt in top_owners.items():
                if owner in early_buyers:
                    rat_holding_amount += amt
                    confirmed_rats.append(owner)
                    logger.debug(f"  🐭 Rat trader: {owner[:8]}... holding {amt:.2f} tokens")

            rat_ratio = (rat_holding_amount / total_supply) if total_supply > 0 else 0.0

            # 🔗 计算捆绑占比 (Bundle Ratio)
            # 定义：资金来源相同的地址簇，持有的代币总量
            # 改进：不仅统计被资助地址，还要统计资金源本身（如果也在持仓中）
            bundle_holding_amount = 0.0
            bundle_clusters = 0
            bundled_addresses = set()  # 已统计的地址，避免重复

            # 打印同源集群
            logger.debug("  🔍 发现资金同源集群:")
            for funder, kids in funding_map.items():
                if len(kids) > 1:  # 至少2个钱包来自同一资金源
                    logger.debug(f"    - 资金源 {funder[:8]}... 资助了 {len(kids)} 个钱包")
                    bundle_clusters += 1
                    
                    # 检查是否有被资助的地址在持仓中
                    has_holder = any(kid in top_owners for kid in kids)
                    # 或者资金源本身在持仓中
                    funder_holding = top_owners.get(funder, 0)
                    
                    if has_holder or funder_holding > 0:
                        # 统计所有被资助地址的持仓（只要有一个在持仓中，就统计全部）
                        cluster_amount = 0.0
                        for kid in kids:
                            if kid not in bundled_addresses and kid in top_owners:
                                amt = top_owners[kid]
                                cluster_amount += amt
                                bundled_addresses.add(kid)
                                logger.debug(f"      📦 {kid[:8]}... holding {amt:.2f} tokens")
                        
                        # 如果资金源也在持仓中，也统计进去
                        if funder not in bundled_addresses and funder_holding > 0:
                            cluster_amount += funder_holding
                            bundled_addresses.add(funder)
                            logger.debug(f"      📦 资金源 {funder[:8]}... holding {funder_holding:.2f} tokens")
                        
                        bundle_holding_amount += cluster_amount
                        logger.debug(f"      ✅ 集群总持仓: {cluster_amount:.2f} tokens ({cluster_amount/total_supply*100:.2f}%)")

            bundle_ratio = (bundle_holding_amount / total_supply) if total_supply > 0 else 0.0
            
            # 如果资金同源分析没有找到结果，回退到时间聚类法（作为备选方案）
            if bundle_ratio == 0.0 and bundle_clusters == 0 and len(early_buyers) > 0:
                logger.debug("  ⚠️ 资金同源分析未找到结果，使用时间聚类法作为备选...")
                # 使用时间聚类：开盘30秒内买入的地址视为捆绑
                time_clusters = defaultdict(list)
                for tx_info in suspicious_txs:
                    owner = tx_info["owner"]
                    tx_time = tx_info["time"]
                    if owner in top_owners:
                        # 将时间相近的交易分组（30秒窗口）
                        found_cluster = False
                        for cluster_time in list(time_clusters.keys()):
                            if abs(cluster_time - tx_time) <= 30:
                                time_clusters[cluster_time].append(owner)
                                found_cluster = True
                                break
                        if not found_cluster:
                            time_clusters[tx_time] = [owner]
                
                # 统计时间簇的持仓
                for cluster_time, owners in time_clusters.items():
                    if len(owners) >= 2:  # 至少2个地址在同一时间窗口
                        cluster_amount = sum(top_owners.get(owner, 0) for owner in owners)
                        if cluster_amount > 0:
                            bundle_holding_amount += cluster_amount
                            logger.debug(f"  ⏰ 时间簇 ({len(owners)} addresses): {cluster_amount:.2f} tokens")
                
                bundle_ratio = (bundle_holding_amount / total_supply) if total_supply > 0 else 0.0
                if bundle_ratio > 0:
                    logger.debug(f"  ✅ 时间聚类法找到捆绑占比: {bundle_ratio*100:.2f}%")

            # 转换为小数形式（0.23 = 23%）
            logger.info(f"✅ Analysis complete: bundled={bundle_ratio:.4f} ({bundle_ratio*100:.2f}%), rat={rat_ratio:.4f} ({rat_ratio*100:.2f}%)")
            logger.debug(f"  Bundled clusters: {bundle_clusters}, confirmed rats: {len(confirmed_rats)}")

            return bundle_ratio, rat_ratio

        except Exception as e:
            logger.warning(f"❌ Solana analysis failed: {e}")
            import traceback
            logger.debug(f"Traceback: {traceback.format_exc()}")
            return None, None


async def calculate_rat_and_bundled(
    mint_address: str,
    sol_config: Optional[ChainConfig],
    client
) -> Tuple[Optional[float], Optional[float]]:
    """
    便捷函数：计算老鼠仓和捆绑占比
    返回 (rat_ratio, bundled_ratio)，值为小数形式（0.23 = 23%）
    """
    if not sol_config or not sol_config.rpc_url:
        return None, None

    analyzer = SolanaRoughAnalyzer(sol_config.rpc_url, client)
    bundled, rat = await analyzer.analyze(mint_address)
    return rat, bundled
