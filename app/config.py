"""從環境變數讀取設定。缺必要變數時直接報錯,不帶預設值跑。"""
from __future__ import annotations

import os
from dataclasses import dataclass

# 預設模型:依 Google AI 官方 release notes,gemini-3.8-flash 於 2026-09-02 GA。
# 可用 GEMINI_MODEL 覆寫。
DEFAULT_GEMINI_MODEL = "gemini-3.8-flash"

REQUIRED = ("LINE_CHANNEL_SECRET", "LINE_CHANNEL_ACCESS_TOKEN", "GEMINI_API_KEY")


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class Settings:
    line_channel_secret: str
    line_channel_access_token: str
    gemini_api_key: str
    gemini_model: str = DEFAULT_GEMINI_MODEL
    memory_backend: str = "memory"
    redis_url: str | None = None
    memory_max_turns: int = 10
    memory_ttl_minutes: int = 30
    ai_timeout_seconds: float = 20.0
    log_level: str = "INFO"


def _int(name: str, default: int, env: dict) -> int:
    raw = env.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError as e:
        raise ConfigError(f"{name} 必須是整數") from e
    if value <= 0:
        raise ConfigError(f"{name} 必須大於 0")
    return value


def load_settings(env: dict | None = None) -> Settings:
    env = dict(os.environ) if env is None else env
    missing = [k for k in REQUIRED if not env.get(k)]
    backend = env.get("MEMORY_BACKEND", "memory").strip().lower()
    if backend not in ("memory", "redis"):
        raise ConfigError("MEMORY_BACKEND 只能是 memory 或 redis")
    if backend == "redis" and not env.get("REDIS_URL"):
        missing.append("REDIS_URL")
    if missing:
        raise ConfigError("缺少必要環境變數: " + ", ".join(missing))

    return Settings(
        line_channel_secret=env["LINE_CHANNEL_SECRET"],
        line_channel_access_token=env["LINE_CHANNEL_ACCESS_TOKEN"],
        gemini_api_key=env["GEMINI_API_KEY"],
        gemini_model=env.get("GEMINI_MODEL") or DEFAULT_GEMINI_MODEL,
        memory_backend=backend,
        redis_url=env.get("REDIS_URL") or None,
        memory_max_turns=_int("MEMORY_MAX_TURNS", 10, env),
        memory_ttl_minutes=_int("MEMORY_TTL_MINUTES", 30, env),
        ai_timeout_seconds=float(_int("AI_TIMEOUT_SECONDS", 20, env)),
        log_level=env.get("LOG_LEVEL", "INFO").upper(),
    )
