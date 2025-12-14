from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from .models import FilterConfig, FilterRange


def _filters_to_dict(f: FilterConfig) -> Dict[str, Dict[str, Optional[float]]]:
    return {
        "market_cap_usd": f.market_cap_usd.dict(),
        "liquidity_usd": f.liquidity_usd.dict(),
        "open_minutes": f.open_minutes.dict(),
        "top10_ratio": f.top10_ratio.dict(),
        "holder_count": f.holder_count.dict(),
        "max_holder_ratio": f.max_holder_ratio.dict(),
        "trades_5m": f.trades_5m.dict(),
    }


def _filters_from_dict(data: Dict[str, Dict[str, Optional[float]]]) -> FilterConfig:
    def fr(name: str) -> FilterRange:
        return FilterRange(**(data.get(name) or {}))

    return FilterConfig(
        market_cap_usd=fr("market_cap_usd"),
        liquidity_usd=fr("liquidity_usd"),
        open_minutes=fr("open_minutes"),
        top10_ratio=fr("top10_ratio"),
        holder_count=fr("holder_count"),
        max_holder_ratio=fr("max_holder_ratio"),
        trades_5m=fr("trades_5m"),
    )


class StateStore:
    def __init__(self, path: str | Path, admin_ids: List[int]):
        self.path = Path(path)
        self.lock = asyncio.Lock()
        self._state = {
            "listen_chats": [],
            "push_chats": [],
            "filters": _filters_to_dict(FilterConfig()),
        }
        self._load_existing()

    def _load_existing(self):
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text())
                self._state.update(data)
            except Exception:
                # ignore corrupt state; keep defaults
                pass

    async def _write(self):
        self.path.write_text(json.dumps(self._state, indent=2))

    async def save(self):
        async with self.lock:
            await self._write()

    async def snapshot(self) -> Dict[str, Any]:
        async with self.lock:
            return json.loads(json.dumps(self._state))

    async def add_listen(self, chat_id: int):
        async with self.lock:
            if chat_id not in self._state["listen_chats"]:
                self._state["listen_chats"].append(chat_id)
            await self._write()

    async def del_listen(self, chat_id: int):
        async with self.lock:
            if chat_id in self._state["listen_chats"]:
                self._state["listen_chats"].remove(chat_id)
            await self._write()

    async def add_push(self, chat_id: int):
        async with self.lock:
            if chat_id not in self._state["push_chats"]:
                self._state["push_chats"].append(chat_id)
            await self._write()

    async def del_push(self, chat_id: int):
        async with self.lock:
            if chat_id in self._state["push_chats"]:
                self._state["push_chats"].remove(chat_id)
            await self._write()

    async def set_filter(self, name: str, min_val: Optional[float], max_val: Optional[float]):
        async with self.lock:
            filters = self._state["filters"]
            if name not in filters:
                raise ValueError("unknown filter")
            filters[name] = {"min": min_val, "max": max_val}
            await self._write()

    async def filters_cfg(self) -> FilterConfig:
        snap = await self.snapshot()
        return _filters_from_dict(snap["filters"])


