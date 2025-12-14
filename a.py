# ⚠️ 教学 / 原型级示例
# 输入一个 Solana CA（Mint Address）
# 输出：老鼠仓占比、捆绑占比
# 使用真实「免费可用」API：Solscan Public API（无需 key，限速较低）

import time
import requests
from collections import defaultdict
from statistics import median

SOLSCAN_API = "https://api.zan.top/node/v1/solana/mainnet/f54291080a01405dbcfa9af1d244166d"
HEADERS = {
    "accept": "application/json",
    "user-agent": "Mozilla/5.0"
}

# -----------------------------
# 基础 API 调用
# -----------------------------

def get_token_holders(mint, limit=200):
    url = f"{SOLSCAN_API}/token/holders"
    params = {
        "token": mint,
        "offset": 0,
        "limit": limit
    }
    r = requests.get(url, params=params, headers=HEADERS, timeout=10)
    r.raise_for_status()
    return r.json().get("data", [])


def get_token_transfers(mint, limit=1000):
    url = f"{SOLSCAN_API}/token/transfers"
    params = {
        "token": mint,
        "offset": 0,
        "limit": limit
    }
    r = requests.get(url, params=params, headers=HEADERS, timeout=10)
    r.raise_for_status()
    return r.json().get("data", [])

# -----------------------------
# 老鼠仓计算
# -----------------------------

def calc_rat_trading_ratio(mint):
    transfers = get_token_transfers(mint)
    if not transfers:
        return 0.0

    # 1️⃣ 找到开盘时间（第一笔 transfer）
    transfers.sort(key=lambda x: x["blockTime"])
    launch_time = transfers[0]["blockTime"]

    # 2️⃣ 前 60 分钟买入的钱包
    early_wallets = []
    buy_amounts = []

    for tx in transfers:
        if tx["blockTime"] - launch_time <= 3600:
            if tx.get("changeType") == "inc":
                early_wallets.append(tx)
                buy_amounts.append(float(tx.get("amount", 0)))

    if not buy_amounts:
        return 0.0

    mid_amount = median(buy_amounts)

    # 3️⃣ 打老鼠仓分
    rat_wallets = set()
    for tx in early_wallets:
        score = 0.0
        if tx["blockTime"] - launch_time <= 600:
            score += 0.4
        else:
            score += 0.2

        amt = float(tx.get("amount", 0))
        if mid_amount * 0.7 <= amt <= mid_amount * 1.3:
            score += 0.2

        rat_wallets.add(tx["dst"] if score >= 0.6 else None)

    rat_wallets.discard(None)

    # 4️⃣ 用持仓占比来算
    holders = get_token_holders(mint)
    total_supply = sum(float(h["amount"]) for h in holders)

    rat_supply = sum(
        float(h["amount"])
        for h in holders
        if h["owner"] in rat_wallets
    )

    return round(rat_supply / total_supply * 100, 2) if total_supply else 0.0

# -----------------------------
# 捆绑占比计算
# -----------------------------

def calc_bundle_ratio(mint):
    transfers = get_token_transfers(mint)
    holders = get_token_holders(mint)

    wallet_events = defaultdict(list)
    for tx in transfers:
        wallet_events[tx.get("dst")].append(tx)

    # 1️⃣ 根据「首买时间 + 金额相近」做粗聚类
    clusters = []
    used = set()

    wallets = list(wallet_events.keys())

    for w in wallets:
        if w in used:
            continue
        base = wallet_events[w][0]
        group = [w]
        used.add(w)

        for other in wallets:
            if other in used:
                continue
            tx = wallet_events[other][0]
            if abs(tx["blockTime"] - base["blockTime"]) <= 120:
                if abs(float(tx["amount"]) - float(base["amount"])) / float(base["amount"]) < 0.05:
                    group.append(other)
                    used.add(other)

        if len(group) >= 3:
            clusters.append(group)

    # 2️⃣ 统计捆绑持仓
    total_supply = sum(float(h["amount"]) for h in holders)
    bundle_supply = 0.0

    for cluster in clusters:
        for h in holders:
            if h["owner"] in cluster:
                bundle_supply += float(h["amount"])

    return round(bundle_supply / total_supply * 100, 2) if total_supply else 0.0

# -----------------------------
# 主入口
# -----------------------------

def analyze_ca(mint):
    rat_ratio = calc_rat_trading_ratio(mint)
    time.sleep(1)  # 避免被限速
    bundle_ratio = calc_bundle_ratio(mint)

    return {
        "mint": mint,
        "rat_trading_ratio_percent": rat_ratio,
        "bundle_ratio_percent": bundle_ratio,
        "note": "heuristic_estimation / 非官方" 
    }


if __name__ == "__main__":
    CA = "ydDccyq66xKtfqn5bsRpfFXz4WeF4fh3bgQBx1npump"
    result = analyze_ca(CA)
    print(result)
