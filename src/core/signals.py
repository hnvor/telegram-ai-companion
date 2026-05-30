"""Адаптивные сигналы — `pressure` (можно ли грузить) и `engagement` (как реагирует).

Перед каждым проактивным сообщением бот смотрит сюда вместо жёстких лимитов.
Низкое pressure → меньше пушей, мягче формулировки.
Низкое engagement → не наращиваем частоту даже если pressure ок.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import structlog

from src.db.client import get_pool

log = structlog.get_logger()


# Simple tone markers. Not perfect, but cheap and LLM-free.
NEGATIVE_MARKERS = [
    "drained", "tired", "exhausted", "hard day", "rough day", "tough day",
    "no energy", "can't", "cannot", "autopilot", "tunnel", "burnt out", "burned out",
    "awful", "feel bad", "feeling bad", "anxiety", "anxious", "panic", "tense", "stressed",
    "didn't get to", "didn't do", "skipped", "blew it", "overwhelmed",
    "shitty", "crappy", "fucked up",
]
POSITIVE_MARKERS = [
    "got it", "worked out", "got done", "did it", "closed", "finished", "wrapped up",
    "figured out", "better", "easier", "loved it", "great",
    "energy", "stoked", "on fire", "feeling good", "made progress",
    "woke up", "slept well", "landed", "felt good",
]


@dataclass
class Signals:
    pressure: float  # 0..1 — можно ли грузить (низкое=не грузить)
    engagement: float  # 0..1 — как откликается на пуши
    note: str  # короткое объяснение для лога/промптов

    @property
    def quiet(self) -> bool:
        """True если сейчас лучше не пушить вообще."""
        return self.pressure < 0.30


async def compute_signals(user_id: int) -> Signals:
    pool = await get_pool()
    now = datetime.now(timezone.utc)

    # 1. mood/energy последние 3 дня → 0..1
    mood_score = await _mood_score(pool, user_id, now - timedelta(days=3))

    # 2. keyword-тон последних 30 user-сообщений → 0..1
    tone_score = await _tone_score(pool, user_id)

    # 3. response rate на проактивы за 7 дней → engagement
    eng = await _engagement_score(pool, user_id, now - timedelta(days=7))

    # 4. активность user-сообщений за 24ч → 0..1
    activity = await _activity_score(pool, user_id, now - timedelta(hours=24))

    # Pressure — взвешенная сумма. Если плохое настроение или негативный тон —
    # резко снижаем. Активность бустит (юзер включён, можно общаться).
    pressure = (
        0.35 * mood_score
        + 0.25 * tone_score
        + 0.20 * eng
        + 0.20 * activity
    )
    pressure = max(0.0, min(1.0, pressure))

    note = (
        f"mood={mood_score:.2f} tone={tone_score:.2f} "
        f"engagement={eng:.2f} activity={activity:.2f}"
    )
    log.info("signals", user_id=user_id, pressure=round(pressure, 2),
             engagement=round(eng, 2), note=note)
    return Signals(pressure=pressure, engagement=eng, note=note)


async def _mood_score(pool, user_id: int, since: datetime) -> float:
    """avg(mood,energy)/10 за последние 3 дня. Если данных нет — 0.5 (нейтрально)."""
    row = await pool.fetchrow(
        """
        SELECT AVG((COALESCE(mood, 0) + COALESCE(energy, 0))::float
                   / NULLIF((CASE WHEN mood IS NOT NULL THEN 1 ELSE 0 END
                            + CASE WHEN energy IS NOT NULL THEN 1 ELSE 0 END), 0)) AS avg_score
        FROM diary_entries
        WHERE user_id = $1 AND created_at > $2
        """,
        user_id, since,
    )
    avg = row["avg_score"] if row and row["avg_score"] is not None else None
    if avg is None:
        return 0.5
    return float(max(0.0, min(1.0, avg / 10)))


async def _tone_score(pool, user_id: int) -> float:
    """Маркеры в последних 30 user-сообщениях. Negative→0, Positive→1, нейтрально→0.5."""
    rows = await pool.fetch(
        "SELECT content FROM conversations "
        "WHERE user_id=$1 AND role='user' "
        "ORDER BY created_at DESC LIMIT 30",
        user_id,
    )
    if not rows:
        return 0.5
    text = " ".join((r["content"] or "").lower() for r in rows)
    neg = sum(text.count(m) for m in NEGATIVE_MARKERS)
    pos = sum(text.count(m) for m in POSITIVE_MARKERS)
    if neg + pos == 0:
        return 0.5
    return float(max(0.0, min(1.0, pos / (pos + neg))))


async def _engagement_score(pool, user_id: int, since: datetime) -> float:
    """% реакций на проактивные сообщения за период.

    Проактивный = role=assistant с metadata->>'proactive'.
    Реакция = был ли user-message в течение 2 часов после.
    """
    rows = await pool.fetch(
        """
        SELECT a.id, a.created_at,
               EXISTS (
                 SELECT 1 FROM conversations c
                 WHERE c.user_id = a.user_id AND c.role='user'
                   AND c.created_at > a.created_at
                   AND c.created_at < a.created_at + interval '2 hours'
               ) AS responded
        FROM conversations a
        WHERE a.user_id = $1
          AND a.role = 'assistant'
          AND a.metadata IS NOT NULL
          AND a.metadata ? 'proactive'
          AND a.created_at > $2
        """,
        user_id, since,
    )
    if not rows:
        return 0.5
    responded = sum(1 for r in rows if r["responded"])
    return float(responded / len(rows))


async def _activity_score(pool, user_id: int, since: datetime) -> float:
    """Сколько user-сообщений за период. 0=ноль, 0.5=несколько, 1=много (≥10)."""
    n = await pool.fetchval(
        "SELECT COUNT(*) FROM conversations "
        "WHERE user_id=$1 AND role='user' AND created_at > $2",
        user_id, since,
    )
    n = int(n or 0)
    if n == 0:
        return 0.0
    return float(min(1.0, n / 10))
