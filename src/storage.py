from __future__ import annotations

import asyncio
import time
from typing import Optional


class DedupeStore:
    """内存去重存储，不使用Redis"""
    def __init__(self):
        self.memory = {}
        self.lock = asyncio.Lock()

    async def seen(self, key: str, ttl: int = 900) -> bool:
        """检查key是否已存在，如果不存在则添加并返回False，如果存在则返回True"""
        async with self.lock:
            now = time.time()
            # cleanup过期项
            for k, v in list(self.memory.items()):
                if v < now:
                    self.memory.pop(k, None)
            if key in self.memory and self.memory[key] > now:
                return True
            self.memory[key] = now + ttl
            return False

