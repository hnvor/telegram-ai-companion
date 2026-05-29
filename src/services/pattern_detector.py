"""Pattern detector v2 — мягкие сигналы + Sonnet-генерация + adaptive gate.

В отличие от v1 (жёсткие пороги «3 дня подряд mood ≤ 4») этот детектор
смотрит на распределения: «mood за неделю ниже среднего по 30 дням»,
«частота слова автопилот выше базовой», «нет упоминаний движения N дней».

После детекции — прогон через `proactive_gate` чтобы не насесть в плохой момент.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import structlog
from aiogram import Bot

from src.core import memory
from src.core.life_state import format_life_state_block
from src.core.llm import chat as llm_chat
from src.core.proactive_gate import decide_proactive
from src.core.prompts import SYSTEM_BASE
from src.core.signals import compute_signals
from src.db.client import get_pool
from src.db.repo import ConversationsRepo, LifeStateRepo, ProfileRepo
from src.db.repo_extra import PatternSignalsRepo

log = structlog.get_logger()


AUTOPILOT_KEYWORDS = (
    "автопилот", "туннел", "не вижу", "не осозна", "пусто",
    "стен", "будто меня нет", "не помню как",
)
MOVEMENT_KEYWORDS = (
    "гулял", "прогулк", "сходил", "вышел", "пешком", "побегал",
    "тренировк", "разминк", "баня", "сауна", "пробежк", "поплавал",
    "велосипед", "размяк", "отжим", "приседан",
)
NEGATIVE_BODY_MARKERS = (
    "напряг", "зажатост", "болит", "шея зажат", "челюсть",
)


SOFT_SIGNAL_PROMPT = """Ты заметил паттерн в состоянии пользователя за последние дни.
Тебе передаются:
- Тип сигнала с описанием
- Доказательства из его сообщений и метрик
- Портрет жизни (life_state) для контекста

Напиши проактивное сообщение пользователю.

ПРАВИЛА
1. Не приветствуй. Вы уже в постоянной переписке.
2. Прямо назови что заметил. Привяжи к конкретике из доказательств, не общие слова.
3. ОДИН вопрос или ОДНО конкретное предложение действия — не больше.
4. Тон без паники, без морали, без «давай работать над собой».
5. 2-4 предложения максимум.
6. Учитывай его устойчивые паттерны из портрета жизни (например запрет на отдых, гиперфокус на работе) — не предлагай делать больше, предлагай делать иначе.
7. Если уместно — опирайся на experiments, что у него уже зашло (баня, jaw release).

Пиши финальный текст сообщения. Без префикса, без объяснений.
"""


async def run_pattern_detection(bot: Bot, user_id: int) -> None:
    profile = await ProfileRepo.get(user_id)
    if profile is None or profile.onboarding_completed_at is None:
        return
    if profile.paused_until and profile.paused_until > datetime.now(timezone.utc):
        return

    tz = ZoneInfo(profile.timezone) if profile else ZoneInfo("UTC")
    local_now = datetime.now(tz)
    if not (13 <= local_now.hour < 19):
        return

    # Не дублируемся: не было pattern-сообщений за последние 18ч
    pool = await get_pool()
    last_pattern = await pool.fetchval(
        "SELECT MAX(created_at) FROM pattern_signals "
        "WHERE user_id=$1 AND action_taken='sent_message'",
        user_id,
    )
    if last_pattern and (datetime.now(timezone.utc) - last_pattern) < timedelta(hours=18):
        return

    soft_signals = await _detect_soft_signals(user_id)
    if not soft_signals:
        return

    chosen = None
    for sig in soft_signals:
        if not await PatternSignalsRepo.is_in_cooldown(user_id, sig["kind"]):
            chosen = sig
            break
    if chosen is None:
        return

    # Проверяем готовность пользователя через адаптивный gate
    signals = await compute_signals(user_id)
    decision = await decide_proactive(user_id, "anchor", signals)  # pattern идёт под "anchor" фильтр
    if not decision.send:
        log.info("pattern.skipped", reason=decision.reason)
        return

    life_state = await LifeStateRepo.get(user_id) or {}

    user_brief = (
        f"Сигнал: {chosen['kind']} (severity={chosen['severity']})\n"
        f"Описание: {chosen['description']}\n"
        f"Доказательства: {chosen['evidence']}\n\n"
        f"## Портрет жизни\n{format_life_state_block(life_state)[:2000]}\n\n"
        "Сформулируй проактивное сообщение."
    )
    if decision.soften:
        user_brief += f"\n\n[смягчи: {decision.soften}]"

    try:
        text = await llm_chat(
            user_id=user_id,
            system_static=SYSTEM_BASE + "\n\n" + SOFT_SIGNAL_PROMPT,
            system_dynamic="",
            messages=[{"role": "user", "content": user_brief}],
            purpose="pattern_detector",
            temperature=0.7,
            max_tokens=400,
        )
    except Exception as e:
        log.warning("pattern.llm_failed", error=str(e))
        return

    try:
        await bot.send_message(user_id, text)
        await ConversationsRepo.append(
            user_id, "assistant", text,
            {"proactive": "pattern", "signal": chosen["kind"]},
        )
        cooldown = chosen.get("cooldown_hours", 72)
        await PatternSignalsRepo.record(
            user_id,
            signal_kind=chosen["kind"],
            severity=chosen["severity"],
            evidence=chosen["evidence"],
            action_taken="sent_message",
            cooldown_hours=cooldown,
        )
        log.info("pattern.sent", user_id=user_id, signal=chosen["kind"])
    except Exception as e:
        log.warning("pattern.send_failed", error=str(e))


async def _detect_soft_signals(user_id: int) -> list[dict]:
    pool = await get_pool()
    found: list[dict] = []

    # 1. mood_below_baseline — avg(mood, последние 7д) < avg(mood, последние 30д) - 0.5
    recent_avg = await pool.fetchval(
        """
        SELECT AVG((COALESCE(mood,0)+COALESCE(energy,0))::float
                   / NULLIF((CASE WHEN mood IS NOT NULL THEN 1 ELSE 0 END
                           + CASE WHEN energy IS NOT NULL THEN 1 ELSE 0 END), 0))
        FROM diary_entries
        WHERE user_id=$1 AND entry_date >= CURRENT_DATE - INTERVAL '7 days'
        """,
        user_id,
    )
    base_avg = await pool.fetchval(
        """
        SELECT AVG((COALESCE(mood,0)+COALESCE(energy,0))::float
                   / NULLIF((CASE WHEN mood IS NOT NULL THEN 1 ELSE 0 END
                           + CASE WHEN energy IS NOT NULL THEN 1 ELSE 0 END), 0))
        FROM diary_entries
        WHERE user_id=$1 AND entry_date >= CURRENT_DATE - INTERVAL '30 days'
              AND entry_date < CURRENT_DATE - INTERVAL '7 days'
        """,
        user_id,
    )
    if recent_avg is not None and base_avg is not None and (base_avg - recent_avg) >= 0.6:
        found.append({
            "kind": "mood_below_baseline",
            "severity": "high" if (base_avg - recent_avg) >= 1.5 else "medium",
            "description": "Настроение/энергия в последнюю неделю заметно ниже личной нормы",
            "evidence": {"recent_7d": round(float(recent_avg), 2),
                          "baseline_30d": round(float(base_avg), 2),
                          "delta": round(float(base_avg - recent_avg), 2)},
            "cooldown_hours": 96,
        })

    # 2. autopilot_frequency — слово автопилот/туннел/не помню упомянуто N+ раз в последние 5 дней
    rows = await pool.fetch(
        "SELECT content, created_at FROM conversations "
        "WHERE user_id=$1 AND role='user' AND created_at > now() - interval '5 days'",
        user_id,
    )
    autopilot_hits = []
    for r in rows:
        c = (r["content"] or "").lower()
        if any(k in c for k in AUTOPILOT_KEYWORDS):
            autopilot_hits.append({
                "date": r["created_at"].strftime("%Y-%m-%d"),
                "snippet": r["content"][:140],
            })
    if len(autopilot_hits) >= 2:
        found.append({
            "kind": "autopilot_frequency",
            "severity": "high" if len(autopilot_hits) >= 4 else "medium",
            "description": "Сам упоминаешь автопилот/туннель чаще обычного — паттерн обостряется",
            "evidence": {"hits": autopilot_hits[:5], "count": len(autopilot_hits)},
            "cooldown_hours": 72,
        })

    # 3. no_movement — нет упоминаний движения за последние 4 дня
    rows = await pool.fetch(
        "SELECT content FROM conversations "
        "WHERE user_id=$1 AND role='user' AND created_at > now() - interval '4 days'",
        user_id,
    )
    movement_count = 0
    for r in rows:
        c = (r["content"] or "").lower()
        if any(k in c for k in MOVEMENT_KEYWORDS):
            movement_count += 1
    if rows and movement_count == 0:
        found.append({
            "kind": "no_movement",
            "severity": "medium",
            "description": "За 4 дня ни одного упоминания движения/прогулки/тела",
            "evidence": {"days": 4, "movement_mentions": 0,
                          "messages_total": len(rows)},
            "cooldown_hours": 72,
        })

    # 4. body_tension — частые упоминания напряжения/боли тела
    rows = await pool.fetch(
        "SELECT content, created_at FROM conversations "
        "WHERE user_id=$1 AND role='user' AND created_at > now() - interval '5 days'",
        user_id,
    )
    body_hits = []
    for r in rows:
        c = (r["content"] or "").lower()
        if any(k in c for k in NEGATIVE_BODY_MARKERS):
            body_hits.append({
                "date": r["created_at"].strftime("%Y-%m-%d"),
                "snippet": r["content"][:140],
            })
    if len(body_hits) >= 3:
        found.append({
            "kind": "body_tension",
            "severity": "medium",
            "description": "Часто упоминаешь напряжение/зажатость тела — стоит вернуться к якорям",
            "evidence": {"hits": body_hits[:5]},
            "cooldown_hours": 96,
        })

    # 5. high_postpone — много задач переносятся
    chronic = await pool.fetch(
        "SELECT id, title, postponed_count FROM tasks "
        "WHERE user_id=$1 AND status IN ('open','doing') AND postponed_count>=3 "
        "ORDER BY postponed_count DESC LIMIT 5",
        user_id,
    )
    if len(chronic) >= 3:
        found.append({
            "kind": "high_postpone",
            "severity": "medium",
            "description": "3+ задач переносятся 3+ раза — стоит спросить почему",
            "evidence": {"tasks": [dict(r) for r in chronic]},
            "cooldown_hours": 168,
        })

    # 6. experiment_regression — последний accepted эксперимент закрыт как failed
    last_failed = await pool.fetchrow(
        "SELECT id, title FROM experiments_log "
        "WHERE user_id=$1 AND completed=FALSE "
        "ORDER BY completed_at DESC NULLS LAST LIMIT 1",
        user_id,
    )
    if last_failed:
        # есть ли accepted эксперименты позже него?
        any_after = await pool.fetchval(
            "SELECT COUNT(*) FROM experiments_log "
            "WHERE user_id=$1 AND id > $2 AND accepted=TRUE",
            user_id, last_failed["id"],
        )
        if not any_after:
            found.append({
                "kind": "experiment_regression",
                "severity": "low",
                "description": "Последний эксперимент не вышел и новых не пробовал",
                "evidence": {"failed_title": last_failed["title"]},
                "cooldown_hours": 168,
            })

    # severity sort
    order = {"high": 0, "medium": 1, "low": 2}
    found.sort(key=lambda s: order.get(s["severity"], 3))
    return found
