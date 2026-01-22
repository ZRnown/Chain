from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

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
        "sol_sniffer_score": f.sol_sniffer_score.dict(),
        "token_sniffer_score": f.token_sniffer_score.dict(),
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
        sol_sniffer_score=fr("sol_sniffer_score"),
        token_sniffer_score=fr("token_sniffer_score"),
    )


class StateStore:
    def __init__(self, path: str | Path, admin_ids: List[int]):
        self.path = Path(path)
        self.lock = asyncio.Lock()
        # 多任务配置：
        # - current_task: 当前选中的任务ID
        # - tasks: {task_id: {"enabled": bool, "listen_chats": [], "push_chats": [], "filters": {...}}}
        # - api_keys: {"sol_sniffer": "...", "token_sniffer": "..."}
        self._state = {
            "current_task": None,
            "tasks": {},
            "api_keys": {
                "sol_sniffer": None,
                "token_sniffer": None,
            },
        }
        self._load_existing()

    def _load_existing(self):
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text())
                default_filters = _filters_to_dict(FilterConfig())
                # 迁移旧版结构（无 tasks）
                if "tasks" not in data:
                    legacy_listen = data.get("listen_chats", [])
                    legacy_push = data.get("push_chats", [])
                    legacy_filters = data.get("filters", default_filters)
                    if not isinstance(legacy_filters, dict):
                        legacy_filters = {}
                    for key, value in default_filters.items():
                        legacy_filters.setdefault(key, dict(value))
                    self._state["tasks"] = {
                        "default": {
                            "enabled": True,
                            "listen_chats": legacy_listen,
                            "push_chats": legacy_push,
                            "filters": legacy_filters,
                        }
                    }
                    self._state["current_task"] = "default" if legacy_listen or legacy_push else None
                else:
                    # 确保 enabled 字段存在
                    tasks = data.get("tasks", {})
                    for tid, cfg in tasks.items():
                        cfg.setdefault("enabled", False)
                        cfg.setdefault("listen_chats", [])
                        cfg.setdefault("push_chats", [])
                        filters_cfg = cfg.get("filters") or {}
                        if not isinstance(filters_cfg, dict):
                            filters_cfg = {}
                        for key, value in default_filters.items():
                            filters_cfg.setdefault(key, dict(value))
                        cfg["filters"] = filters_cfg
                    self._state.update(data)
                    # 如果没有 current_task，则选第一个
                    if not self._state.get("current_task") and tasks:
                        self._state["current_task"] = list(tasks.keys())[0]
                # 确保 api_keys 字段存在
                if "api_keys" not in self._state:
                    self._state["api_keys"] = {"sol_sniffer": None, "token_sniffer": None}
                else:
                    self._state["api_keys"].setdefault("sol_sniffer", None)
                    self._state["api_keys"].setdefault("token_sniffer", None)
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

    # --- 任务级别存取 ---
    def _ensure_task(self, task_id: str):
        if task_id not in self._state["tasks"]:
            self._state["tasks"][task_id] = {
                "enabled": False,
                "listen_chats": [],
                "push_chats": [],
                "filters": _filters_to_dict(FilterConfig()),
                    "start_time": None,
                    "end_time": None,
            }

    async def create_task(self, task_id: str) -> bool:
        async with self.lock:
            if task_id in self._state["tasks"]:
                return False
            self._ensure_task(task_id)
            # 新任务默认暂停
            self._state["tasks"][task_id]["enabled"] = False
            if not self._state.get("current_task"):
                self._state["current_task"] = task_id
            await self._write()
            return True

    async def delete_task(self, task_id: str) -> bool:
        async with self.lock:
            if task_id in self._state["tasks"]:
                self._state["tasks"].pop(task_id, None)
                if self._state.get("current_task") == task_id:
                    self._state["current_task"] = None
                await self._write()
                return True
            return False

    async def set_task_enabled(self, task_id: str, enabled: bool) -> bool:
        async with self.lock:
            if task_id not in self._state["tasks"]:
                return False
            self._state["tasks"][task_id]["enabled"] = enabled
            await self._write()
            return True

    async def set_current_task(self, task_id: Optional[str]):
        async with self.lock:
            self._state["current_task"] = task_id
            if task_id:
                self._ensure_task(task_id)
            await self._write()

    async def current_task(self) -> Optional[str]:
        async with self.lock:
            return self._state.get("current_task")

    async def task_settings(self, task_id: str) -> Dict[str, Any]:
        async with self.lock:
            self._ensure_task(task_id)
            return json.loads(json.dumps(self._state["tasks"][task_id]))

    async def all_tasks(self) -> Dict[str, Any]:
        async with self.lock:
            return json.loads(json.dumps(self._state["tasks"]))

    # --- 监听群组 ---
    async def add_listen(self, chat_id: Union[int, str], task_id: Optional[str] = None):
        async with self.lock:
            task_id = task_id or self._state.get("current_task")
            if not task_id:
                return
            self._ensure_task(task_id)
            task = self._state["tasks"][task_id]
            if chat_id not in task["listen_chats"]:
                task["listen_chats"].append(chat_id)
            await self._write()

    async def del_listen(self, chat_id: Union[int, str], task_id: Optional[str] = None):
        async with self.lock:
            task_id = task_id or self._state.get("current_task")
            if not task_id:
                return
            self._ensure_task(task_id)
            task = self._state["tasks"][task_id]
            if chat_id in task["listen_chats"]:
                task["listen_chats"].remove(chat_id)
            await self._write()

    # --- 推送目标 ---
    async def add_push(self, chat_id: Union[int, str], task_id: Optional[str] = None):
        async with self.lock:
            task_id = task_id or self._state.get("current_task")
            if not task_id:
                return
            self._ensure_task(task_id)
            task = self._state["tasks"][task_id]
            if chat_id not in task["push_chats"]:
                task["push_chats"].append(chat_id)
            await self._write()

    async def del_push(self, chat_id: Union[int, str], task_id: Optional[str] = None):
        async with self.lock:
            task_id = task_id or self._state.get("current_task")
            if not task_id:
                return
            self._ensure_task(task_id)
            task = self._state["tasks"][task_id]
            if chat_id in task["push_chats"]:
                task["push_chats"].remove(chat_id)
            await self._write()

    # --- 筛选条件 ---
    async def set_filter(self, name: str, min_val: Optional[float], max_val: Optional[float], task_id: Optional[str] = None):
        async with self.lock:
            task_id = task_id or self._state.get("current_task")
            if not task_id:
                return
            self._ensure_task(task_id)
            filters = self._state["tasks"][task_id]["filters"]
            if name not in filters:
                raise ValueError("unknown filter")
            filters[name] = {"min": min_val, "max": max_val}
            await self._write()

    async def filters_cfg(self, task_id: Optional[str] = None) -> FilterConfig:
        async with self.lock:
            task_id = task_id or self._state.get("current_task")
            if not task_id or task_id not in self._state["tasks"]:
                return _filters_from_dict(_filters_to_dict(FilterConfig()))
            return _filters_from_dict(self._state["tasks"][task_id]["filters"])

    # --- 任务时间窗 ---
    async def set_task_window(self, task_id: str, start_time: Optional[str], end_time: Optional[str]) -> bool:
        """
        start_time/end_time: 字符串 "HH:MM" 或 None
        """
        async with self.lock:
            if task_id not in self._state["tasks"]:
                return False
            self._state["tasks"][task_id]["start_time"] = start_time
            self._state["tasks"][task_id]["end_time"] = end_time
            await self._write()
            return True

    # --- API Keys ---
    async def set_api_key(self, key_name: str, value: Optional[str]) -> bool:
        """设置 API Key (sol_sniffer 或 token_sniffer)"""
        async with self.lock:
            if key_name not in ("sol_sniffer", "token_sniffer"):
                return False
            self._state["api_keys"][key_name] = value
            await self._write()
            return True

    async def get_api_key(self, key_name: str) -> Optional[str]:
        """获取 API Key"""
        async with self.lock:
            return self._state.get("api_keys", {}).get(key_name)

    async def get_all_api_keys(self) -> Dict[str, Optional[str]]:
        """获取所有 API Keys"""
        async with self.lock:
            return dict(self._state.get("api_keys", {}))
