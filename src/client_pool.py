from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from telethon import TelegramClient
from telethon.sessions import StringSession

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
        # 缓存客户端账号信息（username / id），用于展示友好的名称
        self.client_meta: Dict[str, Dict[str, Any]] = {}
        self._tasks_cfg: List[dict] = []
        self._clients_cfg: List[dict] = []
        self.default_api_id: Optional[int] = None
        self.default_api_hash: Optional[str] = None

    async def load(self) -> None:
        if not self.config_path.exists():
            logger.warning(f"⚠️ tasks config not found: {self.config_path}")
            return
        try:
            data = json.loads(self.config_path.read_text(encoding="utf-8"))
        except Exception as e:
            raise ClientConfigError(f"Failed to load tasks config: {e}") from e

        # 读取默认 app id/hash（仅从环境变量）
        self.default_api_id = self._env_int("TELEGRAM_API_ID") or self._env_int("APP_ID")
        self.default_api_hash = os.getenv("TELEGRAM_API_HASH") or os.getenv("APP_HASH")
        if not self.default_api_id or not self.default_api_hash:
            raise ClientConfigError("环境变量 TELEGRAM_API_ID / TELEGRAM_API_HASH 未配置（或 APP_ID / APP_HASH）")

        self._clients_cfg = data.get("clients", [])
        self._tasks_cfg = data.get("tasks", [])

        # 清理配置中的api_id和api_hash（统一使用.env中的默认值）
        cleaned_clients = []
        for cfg in self._clients_cfg:
            name = cfg.get("name")
            session = cfg.get("session") or ""
            
            if not name:
                logger.warning(f"⚠️ Invalid client config (缺少name): {cfg}")
                continue
            
            # 验证session是否有效
            session_type = self._detect_session_type(session)
            if session_type == "unknown":
                logger.warning(f"⚠️ 跳过无效的客户端配置（无法识别session类型）: {name}")
                continue
            
            # 统一使用默认的api_id和api_hash
            api_id = self.default_api_id
            api_hash = self.default_api_hash
            
            if name in self.clients:
                logger.debug(f"Skipping duplicate client name: {name}")
                continue
            
            # 清理配置，只保留name和session
            cleaned_cfg = {"name": name, "session": session}
            cleaned_clients.append(cleaned_cfg)
            
            try:
                client = self._create_client(session, int(api_id), api_hash)
                await client.start()
                self.clients[name] = client
                # 获取账号信息并缓存
                try:
                    me = await client.get_me()
                    username = getattr(me, "username", None)
                    user_id = getattr(me, "id", None)
                    display_name = username or f"user_{user_id}" if user_id is not None else name
                    self.client_meta[name] = {
                        "username": username,
                        "id": user_id,
                        "display_name": display_name,
                    }
                    logger.info(f"✅ Client started: {name} (username={username}, id={user_id})")
                except Exception as e:
                    logger.warning(f"⚠️ Failed to fetch client info for {name}: {e}")
                    self.client_meta[name] = {"username": None, "id": None, "display_name": name}
            except Exception as e:
                logger.warning(f"⚠️ Failed to start client {name}: {e}")
        
        # 保存清理后的配置
        if cleaned_clients != self._clients_cfg:
            self._clients_cfg = cleaned_clients
            self._save()

        if not self.clients:
            logger.warning("⚠️ No MTProto clients started; tasks needing client will be skipped")

    def get_client(self, name: str) -> Optional[TelegramClient]:
        return self.clients.get(name)

    def tasks_config(self) -> List[dict]:
        return self._tasks_cfg

    def clients_config(self) -> List[dict]:
        return self._clients_cfg

    def _save(self):
        data = {
            "clients": self._clients_cfg,
            "tasks": self._tasks_cfg,
        }
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.config_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def _env_int(self, key: str) -> Optional[int]:
        try:
            val = os.getenv(key)
            return int(val) if val else None
        except Exception:
            return None

    async def add_client(self, name: Optional[str], session: str) -> str:
        """
        添加客户端并返回最终使用的名称。
        如果未传入 name，则自动使用账号的 username（或 user_id）作为名称。
        """
        if not self.default_api_id or not self.default_api_hash:
            raise ClientConfigError("默认 APP_ID/APP_HASH 未配置，请在 .env 设置 TELEGRAM_API_ID / TELEGRAM_API_HASH")
        
        # 验证session是否有效
        session_type = self._detect_session_type(session)
        if not session or session_type == "unknown":
            raise ValueError(f"无效的session：无法识别session类型（文件路径或string session）")
        
        # 先尝试创建和启动客户端
        try:
            client = self._create_client(session, int(self.default_api_id), self.default_api_hash)
            await client.start()
            # 获取账号信息用于自动命名
            try:
                me = await client.get_me()
                auto_base = getattr(me, "username", None) or f"user_{getattr(me, 'id', '')}".strip("_")
            except Exception:
                auto_base = None
        except Exception as e:
            logger.error(f"❌ Failed to start client {name or '[auto]'}: {e}")
            raise  # 重新抛出异常，让调用者知道失败原因
        
        # 确定最终名称：优先用户输入，其次 username，再次 user_id，最后回退 client
        base_name = name or auto_base or "client"
        final_name = base_name
        idx = 1
        # 避免与已有客户端/配置重名
        existing_names = {c.get("name") for c in self._clients_cfg} | set(self.clients.keys())
        while final_name in existing_names:
            idx += 1
            final_name = f"{base_name}_{idx}"
        
        # 保存客户端到内存
        self.clients[final_name] = client
        # 缓存账号信息用于展示
        try:
            if 'me' not in locals():
                me = await client.get_me()
            username = getattr(me, "username", None)
            user_id = getattr(me, "id", None)
            display_name = username or f"user_{user_id}" if user_id is not None else final_name
            self.client_meta[final_name] = {
                "username": username,
                "id": user_id,
                "display_name": display_name,
            }
            logger.info(f"✅ Client started: {final_name} (username={username}, id={user_id})")
        except Exception as e:
            logger.warning(f"⚠️ Failed to fetch client info for {final_name}: {e}")
            self.client_meta[final_name] = {"username": None, "id": None, "display_name": final_name}
        
        # 客户端启动成功后，才保存到配置
        cfg = {"name": final_name, "session": session}
        self._clients_cfg.append(cfg)
        self._save()
        logger.info(f"✅ Client {final_name} added to config")
        return final_name

    async def remove_client(self, name: str) -> bool:
        """删除客户端"""
        removed = False
        
        # 停止并断开客户端连接
        if name in self.clients:
            try:
                await self.clients[name].disconnect()
                del self.clients[name]
                logger.info(f"✅ Client disconnected: {name}")
                removed = True
            except Exception as e:
                logger.warning(f"⚠️ Failed to disconnect client {name}: {e}")
                # 即使断开失败，也从内存中删除
                if name in self.clients:
                    del self.clients[name]
                    removed = True
        
        # 从配置中删除
        original_count = len(self._clients_cfg)
        self._clients_cfg = [c for c in self._clients_cfg if c.get("name") != name]
        
        if len(self._clients_cfg) < original_count:
            self._save()
            logger.info(f"✅ Client removed from config: {name}")
            removed = True
        
        if not removed:
            logger.warning(f"⚠️ Client {name} not found in memory or config")
        
        return removed

    def describe_clients(self) -> List[Dict[str, Any]]:
        """返回客户端详细信息，用于前端展示"""
        desc = []
        for cfg in self._clients_cfg:
            name = cfg.get("name")
            session = cfg.get("session") or ""
            
            # 检测session类型，如果无法识别则跳过
            session_type = self._detect_session_type(session)
            if session_type == "unknown":
                logger.warning(f"⚠️ 跳过无效的客户端配置（无法识别session类型）: {name}")
                continue
            
            # 统一使用.env中的默认api_id
            api_id = self.default_api_id
            session_preview = self._mask_session(session, session_type)
            
            # 中文状态
            status = "运行中" if name in self.clients else "已停止"
            
            # 中文session类型
            session_type_cn = "文件" if session_type == "file" else "字符串"
            
            meta = self.client_meta.get(name, {})
            display_name = meta.get("display_name") or name
            username = meta.get("username")
            user_id = meta.get("id")
            
            desc.append({
                "name": display_name,
                "internal_name": name,
                "username": username,
                "user_id": user_id,
                "api_id": api_id,
                "session_type": session_type_cn,
                "session_preview": session_preview,
                "status": status,
            })
        return desc
    
    def _detect_session_type(self, session: str) -> str:
        """检测session类型：file 或 string 或 unknown"""
        if not session or not session.strip():
            return "unknown"
        session = session.strip()
        
        # 如果session很长（>255字符），很可能是string session
        if len(session) > 255:
            return "string"
        
        # 如果包含路径分隔符，可能是文件路径
        if "/" in session or "\\" in session:
            try:
                if Path(session).exists():
                    return "file"
            except (OSError, ValueError):
                # 文件路径太长或其他错误，无法识别
                return "unknown"
        
        # 如果以.session结尾，可能是文件路径
        if session.endswith(".session"):
            try:
                if Path(session).exists():
                    return "file"
            except (OSError, ValueError):
                return "unknown"
        
        # 如果session长度合理（>10字符），可能是string session
        if len(session) > 10:
            return "string"
        
        # 无法识别
        return "unknown"

    def _create_client(self, session: str, api_id: int, api_hash: str) -> TelegramClient:
        """支持 session 文件路径或 session 字符串"""
        # 先检测 session 类型，避免对长字符串调用 Path().exists()
        session_type = self._detect_session_type(session)
        
        if session_type == "file":
            # 文件路径
            return TelegramClient(session=str(session), api_id=int(api_id), api_hash=api_hash)
        elif session_type == "string":
            # 字符串 session
            return TelegramClient(session=StringSession(session), api_id=int(api_id), api_hash=api_hash)
        else:
            # 未知类型，尝试作为字符串处理
            logger.warning(f"⚠️ Unknown session type, treating as string session")
            return TelegramClient(session=StringSession(session), api_id=int(api_id), api_hash=api_hash)

    def _mask_session(self, session: str, session_type: str) -> str:
        if session_type == "file":
            return str(Path(session))
        if len(session) <= 12:
            return session
        return f"{session[:6]}...{session[-6:]}"

    def update_tasks_config(self, tasks: List[dict]):
        self._tasks_cfg = tasks
        self._save()

    async def stop(self) -> None:
        for name, client in self.clients.items():
            try:
                await client.disconnect()
                logger.info(f"✅ Client stopped: {name}")
            except Exception as e:
                logger.warning(f"⚠️ Failed to stop client {name}: {e}")

