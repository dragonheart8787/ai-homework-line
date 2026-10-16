"""短期記憶:MemoryStore 介面 + 記憶體版 / Redis 版。

內部一律存中性格式 {"role": "user" | "assistant", "text": "..."},
轉成各家 AI 的格式由 app/ai.py 負責。
"""
from __future__ import annotations

import json
import time
from typing import Callable, Protocol

Message = dict  # {"role": "user"|"assistant", "text": str}


class MemoryStore(Protocol):
    async def get_history(self, key: str) -> list[Message]: ...
    async def append_turn(self, key: str, user_text: str, bot_text: str) -> None: ...
    async def reset(self, key: str) -> None: ...
    async def mark_event_seen(self, event_id: str) -> bool:
        """第一次看到回傳 True;重複事件回傳 False。"""
        ...
    async def close(self) -> None: ...


DEDUP_TTL_SECONDS = 600


class InMemoryStore:
    """單一行程用。方法內沒有 await,在同一個 event loop 中本身就是原子操作。"""

    def __init__(self, max_turns: int = 10, ttl_seconds: int = 1800,
                 clock: Callable[[], float] = time.monotonic):
        self.max_messages = max_turns * 2
        self.ttl = ttl_seconds
        self.clock = clock
        self._data: dict[str, tuple[float, list[Message]]] = {}
        self._seen: dict[str, float] = {}
        self._last_sweep = clock()

    def _sweep(self) -> None:
        now = self.clock()
        if now - self._last_sweep < 60:
            return
        self._last_sweep = now
        for k in [k for k, (ts, _) in self._data.items() if now - ts > self.ttl]:
            del self._data[k]
        for k in [k for k, exp in self._seen.items() if exp <= now]:
            del self._seen[k]

    async def get_history(self, key: str) -> list[Message]:
        entry = self._data.get(key)
        if entry is None:
            return []
        ts, msgs = entry
        if self.clock() - ts > self.ttl:
            del self._data[key]
            return []
        return list(msgs)

    async def append_turn(self, key: str, user_text: str, bot_text: str) -> None:
        self._sweep()
        msgs = await self.get_history(key)
        msgs += [{"role": "user", "text": user_text}, {"role": "assistant", "text": bot_text}]
        self._data[key] = (self.clock(), msgs[-self.max_messages:])

    async def reset(self, key: str) -> None:
        self._data.pop(key, None)

    async def mark_event_seen(self, event_id: str) -> bool:
        now = self.clock()
        exp = self._seen.get(event_id)
        if exp is not None and exp > now:
            return False
        self._seen[event_id] = now + DEDUP_TTL_SECONDS
        return True

    async def close(self) -> None:
        pass


class RedisStore:
    """多實例共用。閒置過期用 key TTL,寫入用 MULTI/EXEC pipeline 原子化。"""

    def __init__(self, client, max_turns: int = 10, ttl_seconds: int = 1800,
                 prefix: str = "linebot"):
        self.r = client  # redis.asyncio.Redis(decode_responses=True)
        self.max_messages = max_turns * 2
        self.ttl = ttl_seconds
        self.prefix = prefix

    @classmethod
    def from_url(cls, url: str, **kw) -> "RedisStore":
        import redis.asyncio as redis
        return cls(redis.from_url(url, decode_responses=True), **kw)

    def _hkey(self, key: str) -> str:
        return f"{self.prefix}:hist:{key}"

    async def get_history(self, key: str) -> list[Message]:
        raw = await self.r.lrange(self._hkey(key), 0, -1)
        return [json.loads(x) for x in raw]

    async def append_turn(self, key: str, user_text: str, bot_text: str) -> None:
        k = self._hkey(key)
        async with self.r.pipeline(transaction=True) as pipe:
            pipe.rpush(k,
                       json.dumps({"role": "user", "text": user_text}, ensure_ascii=False),
                       json.dumps({"role": "assistant", "text": bot_text}, ensure_ascii=False))
            pipe.ltrim(k, -self.max_messages, -1)
            pipe.expire(k, self.ttl)
            await pipe.execute()

    async def reset(self, key: str) -> None:
        await self.r.delete(self._hkey(key))

    async def mark_event_seen(self, event_id: str) -> bool:
        ok = await self.r.set(f"{self.prefix}:seen:{event_id}", "1",
                              nx=True, ex=DEDUP_TTL_SECONDS)
        return bool(ok)

    async def close(self) -> None:
        await self.r.aclose()


def create_store(settings) -> MemoryStore:
    kw = dict(max_turns=settings.memory_max_turns,
              ttl_seconds=settings.memory_ttl_minutes * 60)
    if settings.memory_backend == "redis":
        return RedisStore.from_url(settings.redis_url, **kw)
    return InMemoryStore(**kw)
