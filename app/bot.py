"""LINE 事件處理:去重、記憶、呼叫 AI、回覆。"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import time
import weakref
from typing import Awaitable, Callable, Protocol

from linebot.v3.webhooks import (
    GroupSource, MessageEvent, RoomSource, TextMessageContent,
)

from app.ai import AIError, truncate_for_line
from app.memory import MemoryStore

log = logging.getLogger(__name__)

APOLOGY_REPLY = "抱歉,我現在有點忙不過來,請稍後再試一次 🙏"
TEXT_ONLY_REPLY = "目前只支援文字訊息喔,請用打字跟我聊天!"
RESET_REPLY = "好的,我已經把我們剛剛的對話忘掉了,重新開始吧!"
RESET_COMMAND = "/reset"

ReplyFn = Callable[[str, str], Awaitable[None]]           # (reply_token, text)
AIFn = Callable[[list[dict], str], Awaitable[str]]      # (history, user_text)


class Replier(Protocol):
    async def reply(self, reply_token: str, text: str) -> None: ...


def short_hash(value: str | None) -> str:
    if not value:
        return "-"
    return hashlib.sha256(value.encode()).hexdigest()[:10]


def conversation_key(event: MessageEvent) -> str:
    src = event.source
    user_id = getattr(src, "user_id", None) or "unknown"
    if isinstance(src, GroupSource):
        return f"group:{src.group_id}:{user_id}"
    if isinstance(src, RoomSource):
        return f"room:{src.room_id}:{user_id}"
    return f"user:{user_id}"


def dedup_id(event) -> str | None:
    if getattr(event, "webhook_event_id", None):
        return f"evt:{event.webhook_event_id}"
    msg = getattr(event, "message", None)
    if msg is not None and getattr(msg, "id", None):
        return f"msg:{msg.id}"
    return None


class Bot:
    def __init__(self, store: MemoryStore, replier: Replier, ai_fn: AIFn):
        self.store = store
        self.replier = replier
        self.ai_fn = ai_fn
        # 同一對話的訊息依序處理,避免歷史互相覆蓋(單一實例內);
        # 跨實例則依靠 Redis 原子寫入保證不遺失。
        self._locks: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()

    def _lock_for(self, key: str) -> asyncio.Lock:
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        return lock

    async def _reply(self, token: str, text: str, uid: str) -> None:
        try:
            await self.replier.reply(token, truncate_for_line(text))
        except Exception as e:
            log.error("line reply failed user=%s error=%s", uid, type(e).__name__)

    async def handle_events(self, events: list) -> None:
        for event in events:
            try:
                await self.handle_event(event)
            except Exception:
                log.exception("unhandled error while processing event type=%s",
                              getattr(event, "type", "?"))

    async def handle_event(self, event) -> None:
        etype = getattr(event, "type", "?")
        if not isinstance(event, MessageEvent):
            log.info("ignore event type=%s", etype)
            return

        uid = short_hash(getattr(event.source, "user_id", None))
        eid = dedup_id(event)
        if eid and not await self.store.mark_event_seen(eid):
            log.info("duplicate event skipped user=%s", uid)
            return

        msg = event.message
        if not isinstance(msg, TextMessageContent):
            log.info("non-text message type=%s user=%s", getattr(msg, "type", "?"), uid)
            await self._reply(event.reply_token, TEXT_ONLY_REPLY, uid)
            return

        key = conversation_key(event)
        text = msg.text or ""
        if text.strip().lower() == RESET_COMMAND:
            await self.store.reset(key)
            log.info("memory reset user=%s", uid)
            await self._reply(event.reply_token, RESET_REPLY, uid)
            return

        started = time.perf_counter()
        async with self._lock_for(key):
            history = await self.store.get_history(key)
            try:
                answer = await self.ai_fn(history, text)
            except AIError as e:
                log.error("ai failed user=%s reason=%s latency_ms=%d", uid, e,
                          (time.perf_counter() - started) * 1000)
                await self._reply(event.reply_token, APOLOGY_REPLY, uid)
                return
            await self.store.append_turn(key, text, answer)

        log.info("text message handled user=%s history_len=%d latency_ms=%d",
                 uid, len(history), (time.perf_counter() - started) * 1000)
        await self._reply(event.reply_token, answer, uid)
