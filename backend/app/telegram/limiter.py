"""Outbound send pacing: Telegram allows ~1 msg/sec per chat, 20/min per
group, ~30/sec globally per bot. Every feature (agent replies, workflow
actions) shares those buckets, so all sends should pass through here."""

import asyncio
import time
from collections import deque

PER_CHAT_INTERVAL_S = 1.1
PER_CHAT_PER_MINUTE = 19  # stay under the 20/min group cap


class SendLimiter:
    def __init__(self) -> None:
        self._last_send: dict[tuple[int, int], float] = {}
        self._minute_window: dict[tuple[int, int], deque[float]] = {}
        self._lock = asyncio.Lock()

    async def acquire(self, bot_id: int, chat_id: int) -> None:
        key = (bot_id, chat_id)
        while True:
            async with self._lock:
                now = time.monotonic()
                window = self._minute_window.setdefault(key, deque())
                while window and now - window[0] > 60:
                    window.popleft()
                wait = 0.0
                last = self._last_send.get(key)
                if last is not None:
                    wait = max(wait, PER_CHAT_INTERVAL_S - (now - last))
                if len(window) >= PER_CHAT_PER_MINUTE:
                    wait = max(wait, 60 - (now - window[0]))
                if wait <= 0:
                    self._last_send[key] = now
                    window.append(now)
                    return
            await asyncio.sleep(wait)
