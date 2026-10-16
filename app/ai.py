"""AI 供應商封裝(Google Gemini, google-genai SDK)。換供應商只需改這個檔案。

對外介面:await generate_reply(history, user_text) -> str
- history 為中性格式 [{"role": "user"|"assistant", "text": ...}]
- 成功回傳文字(已截斷至 LINE 上限);安全過濾或無文字回傳固定友善訊息
- 呼叫失敗(timeout / 重試用盡 / 4xx)拋出 AIError,由呼叫端回覆道歉訊息
"""
from __future__ import annotations

import asyncio
import logging
import os

import httpx
from google import genai
from google.genai import errors, types

log = logging.getLogger(__name__)

LINE_TEXT_LIMIT = 5000
MAX_RETRIES = 2
BACKOFF_BASE_SECONDS = 1.0

SYSTEM_INSTRUCTION = (
    "你是一個友善的 LINE 聊天助理。一律使用繁體中文回答。"
    "回答要簡短、口語、適合在手機上閱讀,能用幾句話說完就不要長篇大論,"
    "避免使用 Markdown 表格與大量標題。"
)
BLOCKED_REPLY = "抱歉,這個問題我沒辦法回答,換個話題聊聊吧!"


class AIError(Exception):
    pass


_client: genai.Client | None = None
_model: str = os.environ.get("GEMINI_MODEL") or "gemini-3.8-flash"
_timeout: float = 20.0


def configure(api_key: str, model: str, timeout_seconds: float = 20.0) -> None:
    global _client, _model, _timeout
    _model, _timeout = model, timeout_seconds
    _client = genai.Client(
        api_key=api_key,
        # SDK 自帶的 HTTP 重試關閉(attempts=1),重試策略統一由下方 generate_reply 控制
        http_options=types.HttpOptions(
            timeout=int(timeout_seconds * 1000),
            retry_options=types.HttpRetryOptions(attempts=1),
        ),
    )


def to_gemini_contents(history: list[dict], user_text: str) -> list[types.Content]:
    """中性格式 -> Gemini 格式。Gemini 的角色只有 "user" 與 "model"。"""
    contents = []
    for m in history:
        role = "model" if m["role"] == "assistant" else "user"
        contents.append(types.Content(role=role, parts=[types.Part(text=m["text"])]))
    contents.append(types.Content(role="user", parts=[types.Part(text=user_text)]))
    return contents


def truncate_for_line(text: str, limit: int = LINE_TEXT_LIMIT) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _extract_text(resp) -> str | None:
    """取出文字;被安全過濾或沒有文字時回傳 None(不拋例外)。"""
    feedback = getattr(resp, "prompt_feedback", None)
    if feedback is not None and getattr(feedback, "block_reason", None):
        log.warning("gemini prompt blocked reason=%s", feedback.block_reason)
        return None
    try:
        text = resp.text
    except Exception as e:  # SDK 在某些情況下取 text 可能出錯
        log.warning("gemini response has no readable text: %s", type(e).__name__)
        text = None
    if not text or not text.strip():
        reason = None
        cands = getattr(resp, "candidates", None) or []
        if cands:
            reason = getattr(cands[0], "finish_reason", None)
        log.warning("gemini empty/blocked response finish_reason=%s", reason)
        return None
    return text


def _retryable(e: Exception) -> bool:
    return isinstance(e, errors.APIError) and (e.code == 429 or 500 <= (e.code or 0) < 600)


async def generate_reply(history: list[dict], user_text: str) -> str:
    if _client is None:
        raise AIError("AI client not configured")
    config = types.GenerateContentConfig(
        system_instruction=SYSTEM_INSTRUCTION,
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
    )
    contents = to_gemini_contents(history, user_text)

    for attempt in range(MAX_RETRIES + 1):
        try:
            resp = await asyncio.wait_for(
                _client.aio.models.generate_content(
                    model=_model, contents=contents, config=config),
                timeout=_timeout,
            )
        except (asyncio.TimeoutError, httpx.TimeoutException) as e:
            log.error("gemini timeout attempt=%d", attempt + 1)
            raise AIError("timeout") from e
        except errors.APIError as e:
            if _retryable(e) and attempt < MAX_RETRIES:
                delay = BACKOFF_BASE_SECONDS * (2 ** attempt)
                log.warning("gemini error code=%s attempt=%d retry_in=%.1fs",
                            e.code, attempt + 1, delay)
                await asyncio.sleep(delay)
                continue
            log.error("gemini error code=%s attempt=%d giving up", e.code, attempt + 1)
            raise AIError(f"api error {e.code}") from e
        except Exception as e:
            log.error("gemini unexpected error type=%s", type(e).__name__)
            raise AIError("unexpected") from e

        text = _extract_text(resp)
        if text is None:
            return BLOCKED_REPLY
        return truncate_for_line(text)

    raise AIError("unreachable")  # pragma: no cover
