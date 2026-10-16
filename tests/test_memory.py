import asyncio
import time

import fakeredis
import pytest

from app.bot import Bot
from app.memory import InMemoryStore, RedisStore
from tests.conftest import FakeReplier


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def make(backend, max_turns=10, ttl=1800, clock=None):
    if backend == "memory":
        return InMemoryStore(max_turns=max_turns, ttl_seconds=ttl,
                             **({"clock": clock} if clock else {}))
    return RedisStore(fakeredis.FakeAsyncRedis(decode_responses=True),
                      max_turns=max_turns, ttl_seconds=ttl)


BACKENDS = ["memory", "redis"]


def run(coro):
    return asyncio.run(coro)


@pytest.mark.parametrize("backend", BACKENDS)
def test_sliding_window(backend):
    async def go():
        s = make(backend, max_turns=3)
        for i in range(5):
            await s.append_turn("k", f"u{i}", f"b{i}")
        h = await s.get_history("k")
        assert [m["text"] for m in h] == ["u2", "b2", "u3", "b3", "u4", "b4"]
        assert [m["role"] for m in h[:2]] == ["user", "assistant"]
    run(go())


def test_ttl_memory():
    async def go():
        clock = Clock()
        s = make("memory", ttl=60, clock=clock)
        await s.append_turn("k", "u", "b")
        clock.t += 59
        assert len(await s.get_history("k")) == 2
        clock.t += 2
        assert await s.get_history("k") == []
    run(go())


def test_ttl_redis():
    async def go():
        s = make("redis", ttl=1)
        await s.append_turn("k", "u", "b")
        assert 0 < await s.r.ttl("linebot:hist:k") <= 1
        assert len(await s.get_history("k")) == 2
        await asyncio.sleep(1.2)
        assert await s.get_history("k") == []
    run(go())


def test_redis_ttl_refreshed_on_write():
    async def go():
        s = make("redis", ttl=1800)
        await s.append_turn("k", "u", "b")
        await s.r.expire("linebot:hist:k", 5)
        await s.append_turn("k", "u2", "b2")
        assert await s.r.ttl("linebot:hist:k") > 1700
    run(go())


@pytest.mark.parametrize("backend", BACKENDS)
def test_reset_and_isolation(backend):
    async def go():
        s = make(backend)
        await s.append_turn("a", "ua", "ba")
        await s.append_turn("b", "ub", "bb")
        assert [m["text"] for m in await s.get_history("a")] == ["ua", "ba"]
        await s.reset("a")
        assert await s.get_history("a") == []
        assert [m["text"] for m in await s.get_history("b")] == ["ub", "bb"]
    run(go())


@pytest.mark.parametrize("backend", BACKENDS)
def test_concurrent_appends_not_lost(backend):
    async def go():
        s = make(backend, max_turns=100)
        await asyncio.gather(*(s.append_turn("k", f"u{i}", f"b{i}") for i in range(50)))
        h = await s.get_history("k")
        assert len(h) == 100
        assert {m["text"] for m in h if m["role"] == "user"} == {f"u{i}" for i in range(50)}
    run(go())


@pytest.mark.parametrize("backend", BACKENDS)
def test_dedup(backend):
    async def go():
        s = make(backend)
        assert await s.mark_event_seen("e1") is True
        assert await s.mark_event_seen("e1") is False
        assert await s.mark_event_seen("e2") is True
    run(go())


@pytest.mark.parametrize("backend", BACKENDS)
def test_bot_serializes_same_user(backend):
    """同一使用者同時送兩則,第二則要看得到第一則,且歷史不互相覆蓋。"""
    from linebot.v3.webhooks import Event
    from tests.conftest import text_event

    async def slow_ai(history, text):
        await asyncio.sleep(0.05)
        return f"r-{text}|seen={len(history)}"

    async def go():
        s = make(backend)
        replier = FakeReplier()
        bot = Bot(s, replier, slow_ai)
        evs = [Event.from_dict(text_event(t)) for t in ("m1", "m2", "m3")]
        await asyncio.gather(*(bot.handle_event(e) for e in evs))
        h = await s.get_history("user:U1")
        assert [m["text"] for m in h if m["role"] == "user"] == ["m1", "m2", "m3"]
        assert sorted(replier.texts) == ["r-m1|seen=0", "r-m2|seen=2", "r-m3|seen=4"]
    run(go())
