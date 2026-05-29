"""Извлечение долгосрочных фактов из сообщений пользователя.

Запускается после каждого ответа агента в фоне (asyncio.create_task).
Использует Haiku — дешёвый и быстрый.

После сохранения фактов проверяет, не пора ли пересчитать `life_state`
(если подкопилось значимого) — без ожидания ежедневного крона.
"""

import asyncio
import json
from datetime import datetime, timedelta, timezone

import structlog

from src.core.embeddings import embed
from src.core.llm import chat_json
from src.core.prompts import EXTRACTION_PROMPT
from src.db.client import get_pool
from src.db.repo import FactsRepo, LifeStateRepo
from src.domain.models import ExtractedFact, Fact

log = structlog.get_logger()


async def extract_and_store(
    user_id: int, user_message: str, source_message_id: int | None = None
) -> list[Fact]:
    """Извлекает факты из сообщения пользователя и сохраняет их с эмбеддингами."""
    if not user_message or len(user_message.strip()) < 10:
        return []

    try:
        raw = await chat_json(
            user_id=user_id,
            system=EXTRACTION_PROMPT,
            user_message=user_message,
            purpose="extraction",
        )
    except Exception as e:
        log.warning("extraction.llm_failed", error=str(e))
        return []

    facts = _parse_facts(raw)
    if not facts:
        return []

    contents = [f.content for f in facts]
    try:
        embeddings = await embed(contents)
    except Exception as e:
        log.warning("extraction.embed_failed", error=str(e))
        return []

    saved: list[Fact] = []
    for ext, vec in zip(facts, embeddings, strict=False):
        fact = Fact(
            user_id=user_id,
            kind=ext.kind,
            content=ext.content,
            confidence=ext.confidence,
            source_message_id=source_message_id,
        )
        try:
            fact_id = await FactsRepo.insert(fact, vec)
            fact.id = fact_id
            saved.append(fact)
        except Exception as e:
            log.warning("extraction.save_failed", error=str(e), content=ext.content[:80])

    if saved:
        log.info("extraction.saved", count=len(saved), user_id=user_id)
        # Если подкопилось значимое — обновляем life_state не дожидаясь дневного крона
        asyncio.create_task(_maybe_refresh_life_state(user_id, saved))

    return saved


async def _maybe_refresh_life_state(user_id: int, just_saved: list[Fact]) -> None:
    """Триггерит пересчёт life_state если:
    - последний апдейт > 4ч назад И
    - либо есть свежий факт с confidence ≥ 0.85,
    - либо за последние 2ч в БД добавилось ≥3 новых фактов.
    """
    try:
        last_update = await LifeStateRepo.updated_at(user_id)
        now = datetime.now(timezone.utc)
        if last_update and (now - last_update) < timedelta(hours=4):
            return

        high_conf = any(f.confidence >= 0.85 for f in just_saved)
        if not high_conf:
            pool = await get_pool()
            recent_count = await pool.fetchval(
                "SELECT COUNT(*) FROM facts "
                "WHERE user_id=$1 AND created_at > now() - interval '2 hours'",
                user_id,
            )
            if int(recent_count or 0) < 3:
                return

        from src.core.life_state import update_life_state

        log.info("life_state.refresh_triggered", user_id=user_id,
                 just_saved=len(just_saved), high_conf=high_conf)
        await update_life_state(user_id, since_hours=24)
    except Exception as e:
        log.warning("life_state.refresh_failed", error=str(e))


def _parse_facts(raw: str) -> list[ExtractedFact]:
    """Тщательный парсинг ответа Haiku в список фактов."""
    text = raw.strip()
    # Удалим возможный markdown code fence
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text
        if text.endswith("```"):
            text = text.rsplit("```", 1)[0]
        text = text.strip()

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        log.warning("extraction.invalid_json", raw=raw[:300])
        return []

    if not isinstance(data, list):
        return []

    out: list[ExtractedFact] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        try:
            out.append(ExtractedFact(**item))
        except Exception:
            continue
    return out
