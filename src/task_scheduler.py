from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

from telethon.errors import RPCError

from .client_pool import ClientPool

logger = logging.getLogger("ca_filter_bot.task_scheduler")

# 中国时区（UTC+8）
TZ_SHANGHAI = timezone(timedelta(hours=8))


class TaskScheduler:
    """
    轻量级任务调度器：
    - 基于 interval_minutes 轮询触发
    - 支持 enable/disable
    - 支持多个 client 并行执行
    """

    def __init__(self, client_pool: ClientPool, process_ca, state_store=None):
        self.client_pool = client_pool
        self.process_ca = process_ca
        self.state_store = state_store  # 用于同步状态到 state.json
        self.tasks: List[Dict[str, Any]] = []
        self._loop_task: Optional[asyncio.Task] = None

    def load_tasks(self, tasks_cfg: List[dict]) -> None:
        now = time.time()
        self.tasks = []
        for t in tasks_cfg:
            task = {
                "id": t.get("id") or t.get("name"),
                "name": t.get("name", "task"),
                "client": t.get("client"),
                "chain": t.get("chain", "solana"),
                "ca": t.get("ca"),
                "targets": t.get("targets", []),
                "interval_minutes": int(t.get("interval_minutes", 5)),
                "enabled": bool(t.get("enabled", True)),
                "next_run": now,
                "start_time": t.get("start_time"),
                "end_time": t.get("end_time"),
            }
            if not task["id"] or not task["client"] or not task["ca"]:
                logger.warning(f"⚠️ Skip invalid task config: {t}")
                continue
            
            # 加载时检查时间窗，如果不在时间窗内则自动禁用
            has_window = task.get("start_time") or task.get("end_time")
            if has_window and task["enabled"]:
                if not self._is_in_time_window(task):
                    task["enabled"] = False
                    logger.info(f"⏸️ Task {task['id']} auto-disabled on load (out of window {task.get('start_time')}~{task.get('end_time')})")
            
            self.tasks.append(task)
        if self.tasks:
            logger.info(f"✅ Loaded {len(self.tasks)} task(s)")
        else:
            logger.info("ℹ️ No tasks loaded")

    async def start(self):
        if self._loop_task:
            return
        self._loop_task = asyncio.create_task(self._run_loop(), name="task_scheduler_loop")
        logger.info("✅ Task scheduler started")

    async def stop(self):
        if self._loop_task:
            self._loop_task.cancel()
            try:
                await self._loop_task
            except asyncio.CancelledError:
                pass
            logger.info("✅ Task scheduler stopped")

    def list_tasks(self) -> List[Dict[str, Any]]:
        return self.tasks

    def add_task(self, task: Dict[str, Any]) -> bool:
        if any(t["id"] == task["id"] for t in self.tasks):
            return False
        now = time.time()
        task["next_run"] = now
        self.tasks.append(task)
        # 同步写回配置
        cfg_tasks = []
        for t in self.tasks:
            cfg_tasks.append({
                "id": t["id"],
                "name": t.get("name"),
                "client": t.get("client"),
                "chain": t.get("chain"),
                "ca": t.get("ca"),
                "targets": t.get("targets", []),
                "interval_minutes": t.get("interval_minutes", 5),
                "enabled": t.get("enabled", True),
                "start_time": t.get("start_time"),
                "end_time": t.get("end_time"),
            })
        self.client_pool.update_tasks_config(cfg_tasks)
        return True

    def pause(self, task_id: str) -> bool:
        for t in self.tasks:
            if t["id"] == task_id:
                t["enabled"] = False
                self.client_pool.update_tasks_config(self.tasks)
                return True
        return False

    def resume(self, task_id: str) -> bool:
        now = time.time()
        for t in self.tasks:
            if t["id"] == task_id:
                t["enabled"] = True
                t["next_run"] = now
                self.client_pool.update_tasks_config(self.tasks)
                return True
        return False

    async def _run_loop(self):
        while True:
            now = time.time()
            for task in self.tasks:
                # 检查时间窗，自动启用/禁用任务
                has_window = task.get("start_time") or task.get("end_time")
                if has_window:
                    in_window = self._is_in_time_window(task)
                    if task["enabled"] and not in_window:
                        # 任务已启用但不在时间窗内，自动禁用
                        task["enabled"] = False
                        logger.info(f"⏸️ Task {task['id']} auto-disabled (out of window {task.get('start_time')}~{task.get('end_time')})")
                        # 同步到配置和 state
                        self.client_pool.update_tasks_config(self.tasks)
                        # 同步到 state.json（如果可用）
                        if self.state_store:
                            asyncio.create_task(self._sync_state_enabled(task["id"], False))
                    elif not task["enabled"] and in_window:
                        # 任务已禁用但在时间窗内，自动启用
                        task["enabled"] = True
                        task["next_run"] = now  # 立即可以运行
                        logger.info(f"▶️ Task {task['id']} auto-enabled (in window {task.get('start_time')}~{task.get('end_time')})")
                        # 同步到配置和 state
                        self.client_pool.update_tasks_config(self.tasks)
                        # 同步到 state.json（如果可用）
                        if self.state_store:
                            asyncio.create_task(self._sync_state_enabled(task["id"], True))
                
                if not task["enabled"]:
                    continue
                
                # 检查时间窗（在调度时也检查，避免在时间窗外设置 next_run）
                if not self._is_in_time_window(task):
                    # 如果不在时间窗内，重置 next_run 为时间窗开始时间
                    if task.get("start_time"):
                        try:
                            h, m = task["start_time"].split(":")
                            start_minutes = int(h) * 60 + int(m)
                            now_dt = datetime.now(TZ_SHANGHAI)
                            now_minutes = now_dt.hour * 60 + now_dt.minute
                            
                            # 计算下次运行时间（时间窗开始时间）
                            if now_minutes < start_minutes:
                                # 今天的时间窗还没开始
                                next_run_dt = now_dt.replace(hour=int(h), minute=int(m), second=0, microsecond=0)
                            else:
                                # 今天的时间窗已过，设置为明天的时间窗开始时间
                                next_run_dt = (now_dt + timedelta(days=1)).replace(hour=int(h), minute=int(m), second=0, microsecond=0)
                            
                            task["next_run"] = next_run_dt.timestamp()
                            logger.debug(f"⏸️ Task {task['id']} out of window, next run at window start: {next_run_dt.strftime('%Y-%m-%d %H:%M:%S %Z')}")
                        except Exception as e:
                            logger.warning(f"⚠️ Failed to calculate next run for task {task['id']}: {e}")
                    continue
                
                if now >= task["next_run"]:
                    # 在执行前再次检查时间窗（双重检查）
                    if not self._is_in_time_window(task):
                        # 如果不在时间窗内，计算下次运行时间
                        if task.get("start_time"):
                            try:
                                h, m = task["start_time"].split(":")
                                start_minutes = int(h) * 60 + int(m)
                                now_dt = datetime.now(TZ_SHANGHAI)
                                now_minutes = now_dt.hour * 60 + now_dt.minute
                                
                                # 计算下次运行时间（时间窗开始时间）
                                if now_minutes < start_minutes:
                                    # 今天的时间窗还没开始
                                    next_run_dt = now_dt.replace(hour=int(h), minute=int(m), second=0, microsecond=0)
                                else:
                                    # 今天的时间窗已过，设置为明天的时间窗开始时间
                                    next_run_dt = (now_dt + timedelta(days=1)).replace(hour=int(h), minute=int(m), second=0, microsecond=0)
                                
                                task["next_run"] = next_run_dt.timestamp()
                                logger.info(f"⏸️ Task {task['id']} skipped (out of window), next run at: {next_run_dt.strftime('%Y-%m-%d %H:%M:%S %Z')}")
                            except Exception as e:
                                logger.warning(f"⚠️ Failed to calculate next run for task {task['id']}: {e}")
                        continue
                    
                    # 在时间窗内，正常执行
                    task["next_run"] = now + task["interval_minutes"] * 60
                    # 记录任务执行时间（使用中国时区）
                    next_run_dt = datetime.fromtimestamp(task["next_run"], tz=TZ_SHANGHAI)
                    logger.info(f"⏰ Task {task['id']} next run: {next_run_dt.strftime('%Y-%m-%d %H:%M:%S %Z')}")
                    asyncio.create_task(self._run_task(task))
            await asyncio.sleep(3)
    
    def _is_in_time_window(self, task: Dict[str, Any]) -> bool:
        """检查任务是否在时间窗内"""
        if not task.get("start_time") and not task.get("end_time"):
            return True  # 没有设置时间窗，始终允许
        
        now_dt = datetime.now(TZ_SHANGHAI)
        now_minutes = now_dt.hour * 60 + now_dt.minute
        start_minutes = None
        end_minutes = None
        
        try:
            if task.get("start_time"):
                h, m = task["start_time"].split(":")
                start_minutes = int(h) * 60 + int(m)
            if task.get("end_time"):
                h, m = task["end_time"].split(":")
                end_minutes = int(h) * 60 + int(m)
        except Exception:
            logger.warning(f"⚠️ Invalid start/end time format for task {task['id']}: {task.get('start_time')} - {task.get('end_time')}")
            return True  # 格式错误时允许运行，避免阻塞
        
        # 判断是否在时间窗内（支持跨天）
        if start_minutes is not None and end_minutes is not None:
            if start_minutes <= end_minutes:
                return start_minutes <= now_minutes <= end_minutes
            else:
                return now_minutes >= start_minutes or now_minutes <= end_minutes
        elif start_minutes is not None:
            return now_minutes >= start_minutes
        elif end_minutes is not None:
            return now_minutes <= end_minutes
        else:
            return True

    async def _run_task(self, task: Dict[str, Any]):
        # 再次检查时间窗（双重检查，确保在时间窗内）
        if not self._is_in_time_window(task):
            logger.info(f"⏸️ Task {task['id']} skipped (out of window {task.get('start_time')}~{task.get('end_time')})")
            return

        client_name = task["client"]
        client = self.client_pool.get_client(client_name)
        if not client:
            logger.warning(f"⚠️ Client not found for task {task['id']}: {client_name}")
            return

        chain = task["chain"]
        ca = task["ca"]
        targets = task["targets"]

        # 记录任务执行时间（使用中国时区）
        run_time = datetime.now(TZ_SHANGHAI)
        logger.info(f"▶️ Task {task['id']} running at {run_time.strftime('%Y-%m-%d %H:%M:%S %Z')}: {chain} {ca[:8]}..., targets={len(targets)}")
        try:
            photo, caption, error_msg = await self.process_ca(chain, ca, True, task_id=task.get("id"))
            if error_msg:
                msg = f"❌ 任务 {task['name']} 失败：{error_msg}"
                await self._send_to_targets(client, targets, text=msg, ca=ca)
                return
            if not caption:
                await self._send_to_targets(client, targets, text=f"❌ 任务 {task['name']} 无返回数据", ca=ca)
                return
            await self._send_to_targets(client, targets, text=caption, photo=photo, ca=ca)
            logger.info(f"✅ Task {task['id']} sent to {len(targets)} targets")
        except Exception as e:
            logger.warning(f"⚠️ Task {task['id']} error: {e}")

    async def _send_to_targets(self, client, targets: List[Any], text: Optional[str] = None, photo=None, ca: Optional[str] = None):
        for target in targets:
            try:
                is_bot = isinstance(target, str) and target.startswith("@")
                if is_bot:
                    # 对机器人仅发送 CA（若提供），否则发送文本
                    payload = ca or text or ""
                    if photo:
                        if hasattr(photo, "seek"):
                            photo.seek(0)
                        await client.send_file(target, photo, caption=payload, parse_mode="html")
                    else:
                        await client.send_message(target, payload, parse_mode="html")
                else:
                    if photo:
                        if hasattr(photo, "seek"):
                            photo.seek(0)
                        await client.send_file(target, photo, caption=text or "", parse_mode="html")
                    else:
                        if text:
                            await client.send_message(target, text, parse_mode="html")
            except RPCError as e:
                logger.warning(f"⚠️ Send failed to {target}: {e}")
            except Exception as e:
                logger.warning(f"⚠️ Send failed to {target}: {e}")
    
    async def _sync_state_enabled(self, task_id: str, enabled: bool):
        """异步同步任务启用状态到 state.json"""
        try:
            if self.state_store:
                await self.state_store.set_task_enabled(task_id, enabled)
        except Exception as e:
            logger.warning(f"⚠️ Failed to sync task {task_id} enabled status to state: {e}")

