from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

logger = logging.getLogger("ca_filter_bot.storage")


class DedupeStore:
    """内存去重存储，不使用Redis"""
    def __init__(self):
        self.memory = {}
        self.lock = asyncio.Lock()
        self._last_cleanup = 0
        self._cleanup_interval = 300  # 每5分钟清理一次

    async def seen(self, key: str, ttl: int = 900) -> bool:
        """检查key是否已存在，如果不存在则添加并返回False，如果存在则返回True"""
        try:
            async with self.lock:
                now = time.time()
                
                # 定期清理过期项（避免每次都清理）
                if now - self._last_cleanup > self._cleanup_interval:
                    expired_count = 0
                    for k, v in list(self.memory.items()):
                        if v < now:
                            self.memory.pop(k, None)
                            expired_count += 1
                    if expired_count > 0:
                        logger.debug(f"🧹 Cleaned up {expired_count} expired dedupe entries")
                    self._last_cleanup = now
                
                # 检查key是否存在
                if key in self.memory and self.memory[key] > now:
                    logger.debug(f"⏭️  Key already seen: {key[:16]}...")
                    return True
                
                # 添加新key
                self.memory[key] = now + ttl
                logger.debug(f"✅ Key added to dedupe: {key[:16]}...")
                return False
        except Exception as e:
            logger.error(f"❌ Error in dedupe.seen: {e}", exc_info=True)
            # 出错时返回False，允许处理继续
            return False

