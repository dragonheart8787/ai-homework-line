"""冒煙測試:用真實 GEMINI_API_KEY 做一次 Gemini 呼叫(經由 app/ai.py)。
沒有 GEMINI_API_KEY 時跳過並標示「未驗證」。
    GEMINI_API_KEY=xxx python scripts/smoke_gemini.py
"""
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import ai  # noqa: E402
from app.config import DEFAULT_GEMINI_MODEL  # noqa: E402


def main() -> int:
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        print("SKIPPED: 未設定 GEMINI_API_KEY,真實 Gemini 呼叫「未驗證」。")
        return 0
    model = os.environ.get("GEMINI_MODEL") or DEFAULT_GEMINI_MODEL
    ai.configure(key, model, 20.0)
    history = [{"role": "user", "text": "我叫小明"}, {"role": "assistant", "text": "你好,小明!"}]
    try:
        reply = asyncio.run(ai.generate_reply(history, "我叫什麼名字?請用一句話回答。"))
    except ai.AIError as e:
        print(f"FAILED: model={model} error={e}")
        return 1
    print(f"OK: model={model}\n回覆: {reply}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
