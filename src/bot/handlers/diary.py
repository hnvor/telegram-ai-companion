"""Дневник: явная команда + callback'и для mood/energy."""

import json
from datetime import date, datetime
from zoneinfo import ZoneInfo

import structlog
from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from src.bot.keyboards import mood_keyboard
from src.core.embeddings import embed_one
from src.core.llm import chat_json
from src.core.prompts import DIARY_STRUCTURE_PROMPT
from src.db.repo import DiaryRepo, ProfileRepo
from src.domain.models import DiaryEntry

router = Router()
log = structlog.get_logger()


@router.message(Command("diary"))
async def on_diary(message: Message) -> None:
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await message.answer(
            "Напиши как прошёл день — одной фразой или развёрнуто. Пример:\n"
            "/diary день норм, поработал над StyleAura, шея устала, mood 7"
        )
        return
    raw = parts[1].strip()
    user_id = message.from_user.id  # type: ignore[union-attr]
    await _save_diary(user_id, raw, message)


async def _save_diary(user_id: int, raw: str, message: Message) -> None:
    profile = await ProfileRepo.get(user_id)
    tz = ZoneInfo(profile.timezone) if profile else ZoneInfo("UTC")
    today_local = datetime.now(tz).date()

    # Парсим в структуру
    structured: dict | None = None
    mood: int | None = None
    energy: int | None = None
    try:
        raw_json = await chat_json(
            user_id=user_id,
            system=DIARY_STRUCTURE_PROMPT,
            user_message=raw,
            purpose="diary_parse",
        )
        text = raw_json.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1] if "\n" in text else text
            if text.endswith("```"):
                text = text.rsplit("```", 1)[0]
        structured = json.loads(text.strip())
        mood = structured.get("mood") if isinstance(structured.get("mood"), int) else None
        energy = structured.get("energy") if isinstance(structured.get("energy"), int) else None
    except Exception as e:
        log.warning("diary.parse_failed", error=str(e))
        structured = None

    embedding = await embed_one(raw)
    entry = DiaryEntry(
        user_id=user_id,
        entry_date=today_local,
        mood=mood,
        energy=energy,
        raw_text=raw,
        structured=structured,
    )
    saved = await DiaryRepo.upsert(entry, embedding)

    if mood is None:
        await message.answer(
            f"Записал. Как настроение по шкале 1-10?",
            reply_markup=mood_keyboard("mood"),
        )
    else:
        await message.answer(f"Записал. mood={mood} energy={energy or '?'}")


@router.callback_query(F.data.startswith("mood:") | F.data.startswith("energy:"))
async def on_mood(cb: CallbackQuery) -> None:
    if cb.data is None:
        await cb.answer()
        return
    kind, value_str = cb.data.split(":", 1)
    try:
        value = int(value_str)
    except ValueError:
        await cb.answer()
        return

    user_id = cb.from_user.id
    profile = await ProfileRepo.get(user_id)
    tz = ZoneInfo(profile.timezone) if profile else ZoneInfo("UTC")
    today = datetime.now(tz).date()

    entry = await DiaryRepo.get_by_date(user_id, today)
    if entry is None:
        # Создаём заглушку
        entry = DiaryEntry(user_id=user_id, entry_date=today, raw_text="(оценка только)")
    if kind == "mood":
        entry.mood = value
        next_kind = "energy"
        prompt = "А энергия?"
    else:
        entry.energy = value
        next_kind = None
        prompt = None

    await DiaryRepo.upsert(entry, None)

    if next_kind:
        await cb.message.edit_text(prompt, reply_markup=mood_keyboard(next_kind))  # type: ignore[union-attr]
    else:
        await cb.message.edit_text(  # type: ignore[union-attr]
            f"Записал. mood={entry.mood or '?'} energy={entry.energy or '?'}"
        )
    await cb.answer()
