"""端對端模擬:產生合法 X-Line-Signature,對服務送假的 LINE 文字訊息。

兩種模式:
1) 預設(自帶服務):在本機啟動真正的 HTTP 服務(uvicorn),Gemini 與 LINE reply 換成 fake,
   連續送多則訊息,印出 HTTP 回應與機器人回覆,證明後續回覆有用到前文記憶。
     python scripts/simulate_webhook.py
2) --url:對已在跑的服務(例如 docker run 的容器)送請求,只能看到 HTTP 狀態碼
   (回覆會送往真實 LINE API,假 token 會失敗並寫 log)。
     python scripts/simulate_webhook.py --url http://localhost:8000 --secret <測試用 secret>
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import socket
import sys
import threading
import time
import uuid
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

TEST_SECRET = "simulate-test-secret"
MESSAGES = ["我叫小明,我喜歡貓", "我剛剛說我叫什麼?", "我喜歡什麼動物?", "/reset", "我叫什麼?"]


def sign(body: bytes, secret: str) -> str:
    return base64.b64encode(hmac.new(secret.encode(), body, hashlib.sha256).digest()).decode()


def make_body(text: str, user_id: str = "Usimulated0001") -> bytes:
    event = {
        "type": "message", "mode": "active", "timestamp": int(time.time() * 1000),
        "webhookEventId": uuid.uuid4().hex, "deliveryContext": {"isRedelivery": False},
        "replyToken": uuid.uuid4().hex, "source": {"type": "user", "userId": user_id},
        "message": {"type": "text", "id": uuid.uuid4().hex[:12], "text": text, "quoteToken": "q"},
    }
    return json.dumps({"destination": "Udest", "events": [event]}, ensure_ascii=False).encode()


def post(client: httpx.Client, url: str, body: bytes, signature: str | None):
    headers = {"Content-Type": "application/json"}
    if signature is not None:
        headers["X-Line-Signature"] = signature
    return client.post(f"{url}/callback", content=body, headers=headers)


def run_against_url(url: str, secret: str) -> None:
    with httpx.Client(timeout=10) as c:
        print("GET /healthz ->", c.get(f"{url}/healthz").status_code, c.get(f"{url}/healthz").text)
        body = make_body("哈囉")
        r = post(c, url, body, sign(body, secret))
        print("POST /callback (合法簽章) ->", r.status_code, r.text)
        r = post(c, url, body, sign(body, "wrong-secret"))
        print("POST /callback (錯誤簽章) ->", r.status_code, r.text)
        r = post(c, url, body, None)
        print("POST /callback (缺 header) ->", r.status_code, r.text)


class FakeGemini:
    """模擬模型:從歷史中找名字與喜好來回答,證明歷史有被送進 AI。"""

    async def __call__(self, history, user_text):
        past = " / ".join(m["text"] for m in history if m["role"] == "user")
        return f"[fake-gemini] 收到「{user_text}」;我記得你之前說過:{past or '(沒有前文)'}"


class CapturingReplier:
    def __init__(self):
        self.replies = []

    async def reply(self, token, text):
        self.replies.append(text)


def run_self_hosted() -> None:
    import uvicorn
    from app.config import Settings
    from app.main import create_app
    from app.memory import InMemoryStore

    settings = Settings(line_channel_secret=TEST_SECRET, line_channel_access_token="fake",
                        gemini_api_key="fake")
    replier = CapturingReplier()
    app = create_app(settings, store=InMemoryStore(), replier=replier, ai_fn=FakeGemini())

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    url = f"http://127.0.0.1:{port}"
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    print(f"本機服務已啟動於 {url}(Gemini 與 LINE reply 為 fake)\n")

    with httpx.Client(timeout=10) as c:
        print("GET /healthz ->", c.get(f"{url}/healthz").status_code)
        for text in MESSAGES:
            body = make_body(text)
            n = len(replier.replies)
            r = post(c, url, body, sign(body, TEST_SECRET))
            for _ in range(50):
                if len(replier.replies) > n:
                    break
                time.sleep(0.02)
            print(f"\n使用者: {text}\n  HTTP {r.status_code} {r.text}\n  機器人: {replier.replies[-1]}")
        body = make_body("偽造訊息")
        r = post(c, url, body, sign(body, "attacker-secret"))
        print(f"\n錯誤簽章 -> HTTP {r.status_code} {r.text}")
        r = post(c, url, body, None)
        print(f"缺少簽章 -> HTTP {r.status_code} {r.text}")
    server.should_exit = True


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", help="對已在執行的服務送請求,例如 http://localhost:8000")
    ap.add_argument("--secret", default=TEST_SECRET, help="該服務設定的 LINE_CHANNEL_SECRET(測試用)")
    args = ap.parse_args()
    if args.url:
        run_against_url(args.url.rstrip("/"), args.secret)
    else:
        run_self_hosted()
