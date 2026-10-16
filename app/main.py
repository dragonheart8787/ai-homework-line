"""FastAPI 入口。啟動方式:uvicorn --factory app.main:create_app"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import BackgroundTasks, FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from linebot.v3 import WebhookParser
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    AsyncApiClient, AsyncMessagingApi, Configuration, ReplyMessageRequest, TextMessage,
)
from linebot.v3.webhook import SignatureValidator

from app import ai
from app.bot import Bot, Replier
from app.config import Settings, load_settings
from app.memory import MemoryStore, create_store

log = logging.getLogger("app")


class LineReplier:
    def __init__(self, access_token: str):
        self._client = AsyncApiClient(Configuration(access_token=access_token))
        self._api = AsyncMessagingApi(self._client)

    async def reply(self, reply_token: str, text: str) -> None:
        await self._api.reply_message(ReplyMessageRequest(
            reply_token=reply_token, messages=[TextMessage(text=text)]))

    async def close(self) -> None:
        await self._client.close()


def create_app(settings: Settings | None = None, store: MemoryStore | None = None,
               replier: Replier | None = None, ai_fn=None) -> FastAPI:
    settings = settings or load_settings()  # 缺必要變數直接拋 ConfigError,行程退出
    logging.basicConfig(level=settings.log_level,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # 不記錄外部 API 請求 URL

    if ai_fn is None:
        ai.configure(settings.gemini_api_key, settings.gemini_model, settings.ai_timeout_seconds)
        ai_fn = ai.generate_reply
    store = store or create_store(settings)
    replier = replier or LineReplier(settings.line_channel_access_token)
    bot = Bot(store, replier, ai_fn)
    validator = SignatureValidator(settings.line_channel_secret)
    parser = WebhookParser(settings.line_channel_secret)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        log.info("startup backend=%s model=%s", settings.memory_backend, settings.gemini_model)
        yield
        if hasattr(replier, "close"):
            await replier.close()
        await store.close()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    # UptimeRobot 等監控預設用 HEAD;伺服器會自動不送 body
    @app.api_route("/healthz", methods=["GET", "HEAD"])
    async def healthz():
        return {"status": "ok"}

    @app.post("/callback")
    async def callback(request: Request, background: BackgroundTasks):
        signature = request.headers.get("X-Line-Signature")
        raw = await request.body()
        # fail-closed:先用原始 body 驗簽,通過前不 parse 任何內容
        try:
            body = raw.decode("utf-8")
        except UnicodeDecodeError:
            body = None
        if not signature or body is None or not validator.validate(body, signature):
            log.warning("invalid signature rejected")
            return PlainTextResponse("invalid signature", status_code=400)
        try:
            events = parser.parse(body, signature)
        except InvalidSignatureError:
            return PlainTextResponse("invalid signature", status_code=400)
        except Exception as e:
            log.warning("bad webhook body error=%s", type(e).__name__)
            return PlainTextResponse("bad request", status_code=400)

        log.info("webhook received events=%d", len(events))
        # 先回 200 給 LINE,再於背景處理(避免 Gemini 慢造成 LINE 逾時重送)
        background.add_task(bot.handle_events, events)
        return JSONResponse({"status": "ok"})

    app.state.bot = bot
    return app
