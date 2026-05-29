"""Адаптивная калибровка тона: раз в неделю Haiku смотрит последние 50 сообщений
и подкручивает параметры warmth/directness/humor/push_intensity."""

import json

import structlog

from src.core.llm import chat_json
from src.core.prompts import TONE_CALIBRATION_SYSTEM
from src.db.client import get_pool
from src.db.repo import ProfileRepo

log = structlog.get_logger()


DEFAULT_TONE = {"warmth": 0.7, "directness": 0.5, "humor": 0.6, "push_intensity": 0.5}


async def recalibrate_tone(user_id: int) -> dict | None:
    profile = await ProfileRepo.get(user_id)
    if profile is None:
        return None

    pool = await get_pool()
    rows = await pool.fetch(
        """
        SELECT role, content, created_at FROM conversations
        WHERE user_id = $1
        ORDER BY created_at DESC
        LIMIT 50
        """,
        user_id,
    )
    if len(rows) < 10:
        return None

    transcript_lines: list[str] = []
    for r in reversed(rows):
        snippet = r["content"][:300].replace("\n", " ")
        transcript_lines.append(f"{r['role']}: {snippet}")
    transcript = "\n".join(transcript_lines)

    current_tone = dict(profile.preferences.get("tone", DEFAULT_TONE))

    user_message = (
        f"Текущий тон: {json.dumps(current_tone, ensure_ascii=False)}\n\n"
        f"Последние 50 сообщений:\n{transcript}"
    )

    try:
        raw = await chat_json(
            user_id=user_id,
            system=TONE_CALIBRATION_SYSTEM,
            user_message=user_message,
            purpose="tone_calib",
            max_tokens=400,
        )
    except Exception as e:
        log.warning("tone.llm_failed", error=str(e))
        return None

    text = raw.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text
        if text.endswith("```"):
            text = text.rsplit("```", 1)[0]
    try:
        new_tone = json.loads(text.strip())
    except Exception:
        log.warning("tone.invalid_json", raw=raw[:200])
        return None

    sanitized: dict[str, float] = {}
    for key in ("warmth", "directness", "humor", "push_intensity"):
        old = current_tone.get(key, 0.5)
        new = new_tone.get(key, old)
        try:
            new_val = float(new)
        except Exception:
            new_val = old
        # Капаем шаг ±0.15
        new_val = max(old - 0.15, min(old + 0.15, new_val))
        sanitized[key] = round(max(0.0, min(1.0, new_val)), 2)

    prefs = dict(profile.preferences)
    prefs["tone"] = sanitized
    prefs["tone_last_calibrated_rationale"] = new_tone.get("rationale", "")
    await ProfileRepo.patch(user_id, preferences=prefs)
    log.info("tone.recalibrated", user_id=user_id, new_tone=sanitized)
    return sanitized
