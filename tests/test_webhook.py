import asyncio

import pytest

from app.ai import AIError
from app.bot import APOLOGY_REPLY, RESET_REPLY, TEXT_ONLY_REPLY
from app.config import ConfigError, load_settings
from app.main import create_app
from app.memory import InMemoryStore
from tests.conftest import FakeReplier, image_event, payload, sign, text_event


# ---- 簽章 ----
def test_valid_signature_ok(harness):
    r = harness.post_line(payload(text_event("hi")))
    assert r.status_code == 200
    assert harness.replier.texts == ["echo:hi|prev:"]


def test_wrong_signature_400(harness):
    r = harness.post_line(payload(text_event("hi")), signature=sign(b"other"))
    assert r.status_code == 400
    assert harness.replier.sent == [] and harness.ai.calls == []


def test_missing_signature_400(harness):
    r = harness.post_line(payload(text_event("hi")), signature=None)
    assert r.status_code == 400
    assert harness.ai.calls == []


def test_tampered_body_400(harness):
    body = payload(text_event("hi"))
    sig = sign(body)
    tampered = body.replace(b"hi", b"ho")
    r = harness.post_line(tampered, signature=sig)
    assert r.status_code == 400
    assert harness.ai.calls == []


def test_invalid_json_with_bad_signature_is_400_not_500(harness):
    r = harness.post_line(b"{not json", signature="garbage")
    assert r.status_code == 400


def test_empty_events_verify_button(harness):
    # LINE Developers Console 的 Verify 會送 events: []
    r = harness.post_line(payload())
    assert r.status_code == 200


# ---- 訊息類型 / 指令 ----
def test_non_text_message(harness):
    harness.post_line(payload(image_event()))
    assert harness.replier.texts == [TEXT_ONLY_REPLY]
    assert harness.ai.calls == []


def test_memory_used_and_reset(harness):
    for t in ["a", "b", "c"]:
        harness.post_line(payload(text_event(t)))
    assert harness.replier.texts[-1] == "echo:c|prev:a,b"
    harness.post_line(payload(text_event("/reset")))
    assert harness.replier.texts[-1] == RESET_REPLY
    harness.post_line(payload(text_event("d")))
    assert harness.replier.texts[-1] == "echo:d|prev:"


def test_group_users_isolated(harness):
    g = lambda uid: {"type": "group", "groupId": "G1", "userId": uid}
    harness.post_line(payload(text_event("from-u1", source=g("U1"))))
    harness.post_line(payload(text_event("from-u2", source=g("U2"))))
    assert harness.replier.texts[-1] == "echo:from-u2|prev:"


# ---- 去重 ----
def test_duplicate_event_replied_once(harness):
    ev = text_event("hi", event_id="EVT-1")
    body = payload(ev)
    assert harness.post_line(body).status_code == 200
    assert harness.post_line(body).status_code == 200
    assert len(harness.replier.sent) == 1
    assert len(harness.ai.calls) == 1


# ---- AI 失敗 ----
@pytest.mark.parametrize("exc", [AIError("timeout"), AIError("api error 503")])
def test_ai_failure_sends_apology(settings, exc):
    async def broken_ai(history, text):
        raise exc

    replier = FakeReplier()
    store = InMemoryStore()
    app = create_app(settings, store=store, replier=replier, ai_fn=broken_ai)
    from fastapi.testclient import TestClient
    with TestClient(app) as c:
        body = payload(text_event("hi"))
        r = c.post("/callback", content=body, headers={"X-Line-Signature": sign(body)})
        assert r.status_code == 200
        assert replier.texts == [APOLOGY_REPLY]
        # 失敗的回合不寫入記憶
        assert asyncio.run(store.get_history("user:U1")) == []
        assert c.get("/healthz").status_code == 200  # 服務沒崩


def test_reply_failure_does_not_crash(settings):
    class BadReplier:
        async def reply(self, *_):
            raise RuntimeError("LINE down")

    async def ai_fn(h, t):
        return "ok"

    app = create_app(settings, store=InMemoryStore(), replier=BadReplier(), ai_fn=ai_fn)
    from fastapi.testclient import TestClient
    with TestClient(app) as c:
        body = payload(text_event("hi"))
        assert c.post("/callback", content=body,
                      headers={"X-Line-Signature": sign(body)}).status_code == 200


# ---- 健康檢查 / 設定 ----
def test_healthz_no_leak(harness, settings):
    r = harness.get("/healthz")
    assert r.status_code == 200 and r.json() == {"status": "ok"}
    assert settings.line_channel_secret not in r.text


def test_missing_env_fails_fast():
    with pytest.raises(ConfigError) as e:
        load_settings({"LINE_CHANNEL_SECRET": "x"})
    assert "LINE_CHANNEL_ACCESS_TOKEN" in str(e.value) and "GEMINI_API_KEY" in str(e.value)


def test_redis_backend_requires_url():
    env = {"LINE_CHANNEL_SECRET": "a", "LINE_CHANNEL_ACCESS_TOKEN": "b",
           "GEMINI_API_KEY": "c", "MEMORY_BACKEND": "redis"}
    with pytest.raises(ConfigError, match="REDIS_URL"):
        load_settings(env)
