"""Транскрипция голосовых сообщений через OpenRouter (Gemini 2.5 Flash audio input).

Документация: https://openrouter.ai/docs/guides/overview/multimodal/audio
Аудио надо передавать как base64 + format. Telegram присылает .ogg (Opus).
"""

import base64

import httpx
import structlog

from src.config import settings

log = structlog.get_logger()

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
TRANSCRIBE_MODEL = "google/gemini-2.5-flash"


async def transcribe_voice(ogg_bytes: bytes) -> str | None:
    """Возвращает транскрипцию или None при ошибке."""
    if not settings.openrouter_api_key:
        log.warning("voice.no_openrouter_key")
        return None

    audio_b64 = base64.b64encode(ogg_bytes).decode("ascii")

    payload = {
        "model": TRANSCRIBE_MODEL,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "input_audio",
                        "input_audio": {"data": audio_b64, "format": "ogg"},
                    },
                    {
                        "type": "text",
                        "text": (
                            "Transcribe this voice message word for word in the language spoken. "
                            "Don't edit, don't shorten, don't add comments. "
                            "Only the transcription text itself."
                        ),
                    },
                ],
            }
        ],
        "temperature": 0.0,
    }
    headers = {
        "Authorization": f"Bearer {settings.openrouter_api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com/hnvor/telegram-ai-companion",
        "X-Title": "Telegram AI Companion",
    }

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            r = await client.post(OPENROUTER_URL, json=payload, headers=headers)
            r.raise_for_status()
            data = r.json()
    except Exception as e:
        log.warning("voice.openrouter_failed", error=str(e))
        return None

    try:
        text = data["choices"][0]["message"]["content"]
        if isinstance(text, list):
            # Иногда контент приходит списком блоков
            text = "".join(b.get("text", "") for b in text if isinstance(b, dict))
        return (text or "").strip()
    except Exception as e:
        log.warning("voice.parse_failed", error=str(e), data=str(data)[:300])
        return None
