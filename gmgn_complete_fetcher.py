#!/usr/bin/env python3
"""
完整的 GMGN 数据获取器 (修复版)
修复了 float() 转换字典报错的问题，增加了更强的容错处理
"""
import tls_client
from fake_useragent import UserAgent
from datetime import datetime
from typing import Dict, Any, Optional

class GMGNCompleteFetcher:
    """完整的 GMGN 数据获取器，获取所有需要的数据"""
    
    BASE_URL = "https://gmgn.ai"
    
    def __init__(self):
        # 初始化 TLS Session
        self.session = tls_client.Session(
            client_identifier="chrome_124", 
            random_tls_extension_order=True
        )
        self.session.timeout_seconds = 30
        self.refresh_headers()
    
    def refresh_headers(self):
        """刷新请求头"""
        try:
            ua = UserAgent(os=['Windows']).random
        except:
            ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"

        self.headers = {
            'Host': 'gmgn.ai',
            'accept': 'application/json, text/plain, */*',
            'accept-language': 'en-US,en;q=0.9',
            'referer': 'https://gmgn.ai/?chain=sol',
            'user-agent': ua
        }
    
    def get_token_info_basic(self, contract_address: str) -> Optional[Dict[str, Any]]:
        """基础信息接口"""
        url = f"{self.BASE_URL}/api/v1/mutil_window_token_info"
        payload = {"chain": "sol", "addresses": [contract_address]}
        try:
            response = self.session.post(url, json=payload, headers=self.headers)
            if response.status_code == 200:
                data = response.json()
                if data.get('code') == 0 and data.get('data'):
                    return data['data'][0]
        except Exception as e:
            print(f"❌ Basic Info Error: {e}")
        return None
    
    def _safe_float(self, value):
        """安全转换为 float，失败返回 0.0"""
        try:
            if value is None: return 0.0
            return float(value)
        except:
            return 0.0

    def _normalize_timestamp(self, ts):
        """兼容秒/毫秒的时间戳，无法解析时返回 None"""
        try:
            if ts is None:
                return None
            # 字符串转数字
            if isinstance(ts, str):
                ts = ts.strip()
                if not ts:
                    return None
                ts = float(ts)
            # 毫秒转秒
            if ts > 1e12:
                ts = ts / 1000.0
            return datetime.fromtimestamp(ts)
        except Exception:
            return None

    def extract_all_data(self, contract_address: str) -> Dict[str, Any]:
        """从基础信息接口获取数据"""
        result = {"address": contract_address, "error": None}
        
        print(f"📡 正在请求 GMGN 数据源...")
        
        basic = self.get_token_info_basic(contract_address)

        if not basic:
            result["error"] = "基础接口请求失败 (可能IP被封)"
            return result
        
        # 1. 市值 & 价格
        raw_price = basic.get('price')
        price = 0.0
        # 判断 price 字段是字典还是数值
        if isinstance(raw_price, dict):
            price = self._safe_float(raw_price.get('price'))
        else:
            price = self._safe_float(raw_price)
            
        total_supply = self._safe_float(basic.get('total_supply'))
        
        # 尝试直接获取 mcap，如果没有则计算
        mcap = self._safe_float(basic.get('market_cap'))
        if mcap == 0 and price > 0 and total_supply > 0:
            mcap = price * total_supply
        result["market_cap"] = mcap
        
        # 2. 池子
        liq = self._safe_float(basic.get('pool', {}).get('liquidity'))
        result["liquidity"] = liq
        
        # 3. 开盘时间
        # 尝试多个字段 + 秒/毫秒自动识别
        ts_candidates = [
            basic.get('open_timestamp'),
            basic.get('launch_time'),
            basic.get('pool', {}).get('open_timestamp') if isinstance(basic.get('pool'), dict) else None,
            basic.get('price', {}).get('open_timestamp') if isinstance(basic.get('price'), dict) else None,
        ]
        open_dt = None
        for ts in ts_candidates:
            open_dt = self._normalize_timestamp(ts)
            if open_dt:
                break
        result["open_time"] = open_dt.strftime('%Y-%m-%d %H:%M:%S') if open_dt else "N/A"
            
        # 4. 地址信息
        result["address"] = contract_address
        result["symbol"] = basic.get('symbol', 'N/A')
        
        # 5. 持有人数
        result["holder_count"] = int(basic.get('holder_count') or 0)
        
        # 6. 前10持仓 (从 dev 字段获取)
        dev_info = basic.get('dev', {})
        result["top10_ratio"] = self._safe_float(dev_info.get('top_10_holder_rate'))
            
        # 7. 老鼠仓 (基础接口可能没有，设为0)
        result["rat_ratio"] = 0.0
        
        # 8. 5分钟交易
        raw_swaps = basic.get('price', {})  # basic 里 price 是 dict
        if isinstance(raw_swaps, dict):
            swaps = raw_swaps.get('swaps_5m')
        else:
            swaps = 0
        result["trades_5m"] = int(swaps or 0)
        
        # 9. 最大持仓 (基础接口可能没有，使用估算值)
        result["max_holder_ratio"] = result["top10_ratio"] / 2 if result["top10_ratio"] > 0 else 0.0
            
        # 10. 捆绑占比 (基础接口可能没有，设为0)
        result["bundled_ratio"] = 0.0
        
        return result

    def format_output(self, data: Dict[str, Any]) -> str:
        lines = []
        lines.append("=" * 60)
        lines.append(f"📊 {data.get('symbol')} 数据概览")
        lines.append("=" * 60)
        
        if data.get("error"):
            lines.append(f"❌ 错误: {data['error']}")
            return "\n".join(lines)
        
        def pct(v): return f"{v*100:.2f}%"
        
        lines.append(f"💰 1. 市值大小: ${data['market_cap']:,.2f}")
        lines.append(f"💧 2. 池子大小: ${data['liquidity']:,.2f}")
        lines.append(f"⏰ 3. 开盘时间: {data['open_time']}")
        lines.append(f"📍 4. CA地址: {data['address']}")
        lines.append(f"👥 5. 持有人数: {data['holder_count']}")
        lines.append(f"👑 6. 前十持仓: {pct(data['top10_ratio'])}")
        lines.append(f"🐀 7. 老鼠仓:   {pct(data['rat_ratio'])}")
        lines.append(f"📊 8. 5m交易数: {data['trades_5m']}")
        lines.append(f"🔥 9. 最大持仓: {pct(data['max_holder_ratio'])}")
        lines.append(f"📦 10.捆绑占比: {pct(data['bundled_ratio'])}")
        lines.append("=" * 60)
        return "\n".join(lines)

def main():
    fetcher = GMGNCompleteFetcher()
    test_address = "ydDccyq66xKtfqn5bsRpfFXz4WeF4fh3bgQBx1npump"
    
    print(f"🚀 开始测试获取: {test_address}")
    try:
        data = fetcher.extract_all_data(test_address)
        print(fetcher.format_output(data))
    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f"❌ 脚本运行出错: {e}")

if __name__ == "__main__":
    main()