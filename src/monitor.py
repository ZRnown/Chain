from __future__ import annotations

import re
from typing import Awaitable, Callable

from telethon import TelegramClient, events
from telethon.sessions import StringSession

CA_PATTERN = re.compile(r"[1-9A-HJ-NP-Za-km-z]{32,44}")


def guess_chain(address: str) -> str:
    # Hex 0x... likely EVM/BSC, base58 lengths lean Solana
    if address.startswith("0x") and len(address) == 42:
        return "bsc"
    if len(address) >= 32 and len(address) <= 44:
        return "solana"
    return "bsc"


class Monitor:
    def __init__(
        self,
        api_id: int,
        api_hash: str,
        session: str,
        listen_chats: list[int],
        process_ca: Callable[[str, str], Awaitable[str | None]],
    ):
        self.client = TelegramClient(StringSession(session), api_id, api_hash)
        self.listen_chats = listen_chats
        self.process_ca = process_ca

    def setup(self):
        @self.client.on(events.NewMessage(chats=self.listen_chats))
        async def handler(event):
            text = event.raw_text or ""
            for ca in CA_PATTERN.findall(text):
                chain = guess_chain(ca)
                await self.process_ca(chain, ca)

    async def run(self):
        self.setup()
        await self.client.start()
        await self.client.run_until_disconnected()
