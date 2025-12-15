from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Dict, List, Optional

from telethon import TelegramClient

logger = logging.getLogger("ca_filter_bot.client_pool")


class ClientConfigError(RuntimeError):
    pass


class ClientPool:
    """
    简单的 MTProto 客户端池，基于 Telethon。
    从 config/tasks.json 读取 client 配置并启动。
    """

    def __init__(self, config_path: str):
        self.config_path = Path(config_path)
        self.clients: Dict[str, TelegramClient] = {}
        self._tasks_cfg: List[dict] = []

    async def load(self) -> None:
        if not self.config_path.exists():
            logger.warning(f"⚠️ tasks config not found: {self.config_path}")
            return
        try:
            data = json.loads(self.config_path.read_text(encoding="utf-8"))
        except Exception as e:
            raise ClientConfigError(f"Failed to load tasks config: {e}") from e

        clients_cfg = data.get("clients", [])
        self._tasks_cfg = data.get("tasks", [])

        for cfg in clients_cfg:
            name = cfg.get("name")
            api_id = cfg.get("api_id")
            api_hash = cfg.get("api_hash")
            session = cfg.get("session") or f"session_{name}"
            if not name or not api_id or not api_hash:
                logger.warning(f"⚠️ Invalid client config: {cfg}")
                continue
            if name in self.clients:
                logger.debug(f"Skipping duplicate client name: {name}")
                continue
            client = TelegramClient(session=session, api_id=int(api_id), api_hash=api_hash)
            await client.start()
            self.clients[name] = client
            logger.info(f"✅ Client started: {name}")

        if not self.clients:
            logger.warning("⚠️ No MTProto clients started; tasks needing client will be skipped")

    def get_client(self, name: str) -> Optional[TelegramClient]:
        return self.clients.get(name)

    def tasks_config(self) -> List[dict]:
        return self._tasks_cfg

    async def stop(self) -> None:
        for name, client in self.clients.items():
            try:
                await client.disconnect()
                logger.info(f"✅ Client stopped: {name}")
            except Exception as e:
                logger.warning(f"⚠️ Failed to stop client {name}: {e}")

