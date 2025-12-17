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

    def __init__(self, client_pool: ClientPool, process_ca):
        self.client_pool = client_pool
        self.process_ca = process_ca
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
                if not task["enabled"]:
                    continue
                if now >= task["next_run"]:
                    task["next_run"] = now + task["interval_minutes"] * 60
                    # 记录任务执行时间（使用中国时区）
                    next_run_dt = datetime.fromtimestamp(task["next_run"], tz=TZ_SHANGHAI)
                    logger.info(f"⏰ Task {task['id']} next run: {next_run_dt.strftime('%Y-%m-%d %H:%M:%S %Z')}")
                    asyncio.create_task(self._run_task(task))
            await asyncio.sleep(3)

    async def _run_task(self, task: Dict[str, Any]):
        # 检查时间窗（start_time/end_time），格式 HH:MM，本地上海时区
        if task.get("start_time") or task.get("end_time"):
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
            # 判断是否在时间窗内（支持跨天）
            if start_minutes is not None and end_minutes is not None:
                if start_minutes <= end_minutes:
                    in_window = start_minutes <= now_minutes <= end_minutes
                else:
                    in_window = now_minutes >= start_minutes or now_minutes <= end_minutes
            elif start_minutes is not None:
                in_window = now_minutes >= start_minutes
            elif end_minutes is not None:
                in_window = now_minutes <= end_minutes
            else:
                in_window = True
            if not in_window:
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

