import asyncio
from types import SimpleNamespace

import httpx
import pytest
from google.genai import errors, types

from app import ai


def api_error(code):
    return errors.APIError(code, {"error": {"code": code, "message": "x", "status": "X"}})


def ok_response(text):
    return types.GenerateContentResponse(candidates=[types.Candidate(
        content=types.Content(role="model", parts=[types.Part(text=text)]),
        finish_reason=types.FinishReason.STOP)])


class FakeModels:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    async def generate_content(self, model, contents, config):
        self.calls.append(dict(model=model, contents=contents, config=config))
        o = self.outcomes.pop(0)
        if isinstance(o, Exception):
            raise o
        if o == "hang":
            await asyncio.sleep(10)
        return o


@pytest.fixture
def fake(monkeypatch):
    def install(*outcomes, timeout=20.0):
        models = FakeModels(outcomes)
        monkeypatch.setattr(ai, "_client", SimpleNamespace(aio=SimpleNamespace(models=models)))
        monkeypatch.setattr(ai, "_timeout", timeout)
        monkeypatch.setattr(ai, "BACKOFF_BASE_SECONDS", 0.0)
        return models
    return install


def run(coro):
    return asyncio.run(coro)


def test_role_mapping_assistant_to_model():
    contents = ai.to_gemini_contents(
        [{"role": "user", "text": "q1"}, {"role": "assistant", "text": "a1"}], "q2")
    assert [c.role for c in contents] == ["user", "model", "user"]
    assert [c.parts[0].text for c in contents] == ["q1", "a1", "q2"]
    assert all(c.role != "assistant" for c in contents)


def test_success_sends_history_and_system_instruction(fake):
    models = fake(ok_response("你好"))
    out = run(ai.generate_reply([{"role": "assistant", "text": "prev"}], "hi"))
    assert out == "你好"
    call = models.calls[0]
    assert call["contents"][0].role == "model"
    assert "繁體中文" in call["config"].system_instruction


def test_truncate_over_5000(fake):
    fake(ok_response("字" * 6000))
    out = run(ai.generate_reply([], "hi"))
    assert len(out) == 5000 and out.endswith("…")


def test_truncate_helper_boundary():
    assert ai.truncate_for_line("a" * 5000) == "a" * 5000
    assert len(ai.truncate_for_line("a" * 5001)) == 5000


def test_safety_block_prompt_feedback(fake):
    resp = types.GenerateContentResponse(
        prompt_feedback=types.GenerateContentResponsePromptFeedback(
            block_reason=types.BlockedReason.SAFETY))
    fake(resp)
    assert run(ai.generate_reply([], "bad")) == ai.BLOCKED_REPLY


def test_safety_block_candidate_without_text(fake):
    resp = types.GenerateContentResponse(candidates=[types.Candidate(
        finish_reason=types.FinishReason.SAFETY)])
    fake(resp)
    assert run(ai.generate_reply([], "bad")) == ai.BLOCKED_REPLY


def test_empty_candidates(fake):
    fake(types.GenerateContentResponse(candidates=[]))
    assert run(ai.generate_reply([], "x")) == ai.BLOCKED_REPLY


def test_429_retried_then_success(fake):
    models = fake(api_error(429), api_error(429), ok_response("ok"))
    assert run(ai.generate_reply([], "x")) == "ok"
    assert len(models.calls) == 3


def test_5xx_retried_max_2_times_then_error(fake):
    models = fake(api_error(503), api_error(500), api_error(502), ok_response("never"))
    with pytest.raises(ai.AIError):
        run(ai.generate_reply([], "x"))
    assert len(models.calls) == 3  # 1 次 + 2 次重試


def test_400_not_retried(fake):
    models = fake(api_error(400), ok_response("never"))
    with pytest.raises(ai.AIError):
        run(ai.generate_reply([], "x"))
    assert len(models.calls) == 1


def test_timeout_raises_ai_error(fake):
    models = fake("hang", timeout=0.05)
    with pytest.raises(ai.AIError, match="timeout"):
        run(ai.generate_reply([], "x"))
    assert len(models.calls) == 1


def test_httpx_timeout_raises_ai_error(fake):
    fake(httpx.ReadTimeout("slow"))
    with pytest.raises(ai.AIError, match="timeout"):
        run(ai.generate_reply([], "x"))


def test_backoff_is_exponential(fake, monkeypatch):
    fake(api_error(429), api_error(503), ok_response("ok"))
    monkeypatch.setattr(ai, "BACKOFF_BASE_SECONDS", 1.0)
    sleeps = []

    async def fake_sleep(s):
        sleeps.append(s)
    monkeypatch.setattr(ai.asyncio, "sleep", fake_sleep)
    assert run(ai.generate_reply([], "x")) == "ok"
    assert sleeps == [1.0, 2.0]
