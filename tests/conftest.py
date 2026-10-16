import base64
import hashlib
import hmac
import json
import uuid

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.memory import InMemoryStore

SECRET = "test-channel-secret"


def sign(body: bytes, secret: str = SECRET) -> str:
    return base64.b64encode(hmac.new(secret.encode(), body, hashlib.sha256).digest()).decode()


def text_event(text, user_id="U1", event_id=None, msg_id=None, source=None):
    return {
        "type": "message",
        "mode": "active",
        "timestamp": 1700000000000,
        "webhookEventId": event_id or uuid.uuid4().hex,
        "deliveryContext": {"isRedelivery": False},
        "replyToken": "rt-" + uuid.uuid4().hex[:8],
        "source": source or {"type": "user", "userId": user_id},
        "message": {"type": "text", "id": msg_id or uuid.uuid4().hex[:12],
                    "text": text, "quoteToken": "q"},
    }


def image_event(user_id="U1"):
    e = text_event("x", user_id)
    e["message"] = {"type": "image", "id": "img1", "quoteToken": "q",
                    "contentProvider": {"type": "line"}}
    return e


def payload(*events) -> bytes:
    return json.dumps({"destination": "Uxxx", "events": list(events)}).encode()


class FakeReplier:
    def __init__(self):
        self.sent = []  # (reply_token, text)

    async def reply(self, token, text):
        self.sent.append((token, text))

    @property
    def texts(self):
        return [t for _, t in self.sent]


class FakeAI:
    """回覆內容會帶出它看到的歷史,用來證明記憶有被使用。"""

    def __init__(self):
        self.calls = []

    async def __call__(self, history, user_text):
        self.calls.append((list(history), user_text))
        prev = [m["text"] for m in history if m["role"] == "user"]
        return f"echo:{user_text}|prev:{','.join(prev)}"


@pytest.fixture
def settings():
    return Settings(line_channel_secret=SECRET, line_channel_access_token="tok",
                    gemini_api_key="fake")


@pytest.fixture
def harness(settings):
    store, replier, fake_ai = InMemoryStore(), FakeReplier(), FakeAI()
    app = create_app(settings, store=store, replier=replier, ai_fn=fake_ai)
    with TestClient(app) as client:
        client.store, client.replier, client.ai = store, replier, fake_ai

        def post(body: bytes, signature: str | None = "auto"):
            headers = {"Content-Type": "application/json"}
            if signature == "auto":
                headers["X-Line-Signature"] = sign(body)
            elif signature is not None:
                headers["X-Line-Signature"] = signature
            return client.post("/callback", content=body, headers=headers)

        client.post_line = post
        yield client
