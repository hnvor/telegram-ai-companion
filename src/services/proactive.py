"""Содержательная часть проактивных нотификаций. Все джобы:
1. Загружают профиль пользователя.
2. Проверяют paused_until и pushes preferences.
3. Решают, надо ли что-то слать СЕЙЧАС (с учётом локального времени).
4. Если да — генерируют сообщение через LLM и отправляют.
"""

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import structlog
from aiogram import Bot

from src.bot.keyboards import experiment_kb, mood_keyboard, task_actions_kb
from src.core import memory
from src.core.challenge import format_challenge_message, generate_challenge
from src.core.life_state import update_life_state
from src.core.llm import chat as llm_chat, chat_json
from src.core.proactive_gate import decide_proactive
from src.core.signals import compute_signals
from src.core.weekly_plan import (
    _week_start,
    format_plan_message,
    generate_weekly_plan,
)
from src.core.prompts import (
    AWARENESS_ANCHOR_PROMPT,
    EVENING_CHECKIN_SYSTEM,
    MORNING_BRIEF_SYSTEM,
    SYSTEM_BASE,
)
from src.db.client import get_pool
from src.db.repo import (
    AnchorsRepo,
    ConversationsRepo,
    ExperimentsRepo,
    HabitsRepo,
    ProfileRepo,
    TasksRepo,
    WeeklyPlanRepo,
)

log = structlog.get_logger()


# ============================================================================
# Helpers
# ============================================================================


async def _is_paused(user_id: int) -> bool:
    profile = await ProfileRepo.get(user_id)
    if profile is None or profile.onboarding_completed_at is None:
        return True
    if profile.paused_until and profile.paused_until > datetime.now(timezone.utc):
        return True
    return False


async def _local_now(user_id: int) -> datetime:
    profile = await ProfileRepo.get(user_id)
    tz = ZoneInfo(profile.timezone) if profile else ZoneInfo("UTC")
    return datetime.now(tz)


def _was_today(stored_iso: str | None, today: date) -> bool:
    if not stored_iso:
        return False
    try:
        return date.fromisoformat(stored_iso) == today
    except Exception:
        return False


async def _set_pref(user_id: int, key: str, value) -> None:
    profile = await ProfileRepo.get(user_id)
    if profile is None:
        return
    prefs = dict(profile.preferences)
    prefs[key] = value
    await ProfileRepo.patch(user_id, preferences=prefs)


async def _user_wrote_recently(user_id: int, hours: int) -> bool:
    """True если за последние N часов было хотя бы одно сообщение от пользователя.

    Используется чтобы не спамить habit-нуждами того, кто и так с ботом разговаривает.
    """
    pool = await get_pool()
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    return bool(
        await pool.fetchval(
            "SELECT 1 FROM conversations "
            "WHERE user_id=$1 AND role='user' AND created_at > $2 LIMIT 1",
            user_id, cutoff,
        )
    )


def _wake_hours(profile) -> tuple[int, int]:
    """Окно бодрствования (часы локального ТЗ): из profile.wake_window/sleep_window
    либо дефолт 11-22 для гибкого ритма."""
    default_lo, default_hi = 11, 22
    try:
        wake = profile.wake_window or ""
        if "-" in wake:
            wake_lo = int(wake.split("-")[0].split(":")[0])
        else:
            wake_lo = default_lo
        sleep = profile.sleep_window or ""
        if "-" in sleep:
            sleep_lo = int(sleep.split("-")[0].split(":")[0])
            if sleep_lo < 6:  # ложится после полуночи (1-4 ночи) — для нашего юзера
                hi = 23
            else:
                hi = sleep_lo
        else:
            hi = default_hi
        return max(6, wake_lo), min(24, hi)
    except Exception:
        return default_lo, default_hi


async def pool_last_challenge_at(user_id: int):
    """Время последнего предложенного челленджа из experiments_log."""
    pool = await get_pool()
    return await pool.fetchval(
        "SELECT MAX(proposed_at) FROM experiments_log "
        "WHERE user_id=$1 AND source='challenge'",
        user_id,
    )


async def _recent_proactive_texts(user_id: int, kind: str, limit: int = 3) -> list[str]:
    """Последние N текстов одного типа проактивных пушей — чтобы не повторяться."""
    pool = await get_pool()
    rows = await pool.fetch(
        "SELECT content FROM conversations "
        "WHERE user_id=$1 AND role='assistant' AND metadata->>'proactive'=$2 "
        "ORDER BY created_at DESC LIMIT $3",
        user_id, kind, limit,
    )
    return [r["content"] for r in rows]


# ============================================================================
# Task reminders — каждые 5 мин
# ============================================================================


async def check_task_reminders(bot: Bot, user_id: int) -> None:
    if await _is_paused(user_id):
        return
    pool = await get_pool()
    rows = await pool.fetch(
        """
        SELECT id, title, project FROM tasks
        WHERE user_id = $1 AND remind_at IS NOT NULL AND remind_at <= now()
              AND status IN ('open', 'doing')
        ORDER BY remind_at ASC
        LIMIT 5
        """,
        user_id,
    )
    for row in rows:
        try:
            proj = f" [{row['project']}]" if row["project"] else ""
            await bot.send_message(
                chat_id=user_id,
                text=f"⏰ Напоминание: #{row['id']} {row['title']}{proj}",
                reply_markup=task_actions_kb(row["id"]),
            )
            await pool.execute(
                "UPDATE tasks SET remind_at = NULL WHERE id = $1", row["id"]
            )
        except Exception as e:
            log.warning("reminder.send_failed", task_id=row["id"], error=str(e))


# ============================================================================
# Morning brief
# ============================================================================


async def check_morning_brief(bot: Bot, user_id: int) -> None:
    if await _is_paused(user_id):
        return

    profile = await ProfileRepo.get(user_id)
    if profile is None:
        return
    if "morning_brief" not in profile.preferences.get("pushes", []):
        return

    local_now = await _local_now(user_id)
    today = local_now.date()
    last_sent = profile.preferences.get("morning_brief_sent_date")
    if _was_today(last_sent, today):
        return

    # Адаптивная логика: если до 13:00 локального не было сообщений сегодня — шлём сами.
    # Если пользователь уже писал — брифинг отдаст chat handler через специальный системный промпт (TODO).
    # Простая реализация: шлём в окне [11:00, 14:00] если ещё не было сообщения сегодня.
    if local_now.hour < 11 or local_now.hour >= 14:
        return

    pool = await get_pool()
    row = await pool.fetchrow(
        """
        SELECT MAX(created_at) AS last
        FROM conversations
        WHERE user_id = $1 AND role = 'user'
        """,
        user_id,
    )
    last_user_msg = row["last"] if row else None
    if last_user_msg:
        last_local = last_user_msg.astimezone(local_now.tzinfo)
        if last_local.date() == today:
            # Пользователь уже писал сегодня — пометим как отправленный, не спамим
            await _set_pref(user_id, "morning_brief_sent_date", today.isoformat())
            return

    # Адаптивный гейт: спрашиваем сигналы и Haiku — стоит ли грузить?
    signals = await compute_signals(user_id)
    decision = await decide_proactive(user_id, "morning_brief", signals)
    if not decision.send:
        log.info("morning_brief.skipped", reason=decision.reason)
        await _set_pref(user_id, "morning_brief_sent_date", today.isoformat())
        return

    # Генерим брифинг.
    # Подменяем active_tasks на отфильтрованный список (без застарелого мусора),
    # чтобы бриф не повторял задачи которые висят неделями.
    bundle = await memory.build_context(user_id, "Доброе утро. Дай короткий брифинг на день.")
    fresh_tasks = await TasksRepo.list_active(
        user_id, limit=10, max_age_days=7, max_postponed=3
    )
    bundle.active_tasks = fresh_tasks
    dynamic = memory.format_dynamic_system(bundle)
    recent = await _recent_proactive_texts(user_id, "morning_brief", limit=3)
    avoid_block = ""
    if recent:
        avoid_block = (
            "\n\n## ИЗБЕГАЙ ПОВТОРОВ\n"
            "Вот последние утренние брифинги — НЕ начинай ровно так же и не повторяй "
            "буквально те же задачи без причины:\n"
            + "\n".join(f"- {t[:240]}" for t in recent)
        )
    if decision.soften:
        avoid_block += (
            f"\n\n## СМЯГЧИ ТОН\nИнструкция от гейта: {decision.soften}\n"
            "Сделай короче и без давления."
        )
    try:
        text = await llm_chat(
            user_id=user_id,
            system_static=SYSTEM_BASE + "\n\n" + MORNING_BRIEF_SYSTEM,
            system_dynamic=dynamic + avoid_block,
            messages=[{"role": "user", "content": "Доброе утро. Дай короткий брифинг на день."}],
            purpose="morning_brief",
            temperature=0.75,
        )
        await bot.send_message(chat_id=user_id, text=text)
        await ConversationsRepo.append(user_id, "assistant", text, {"proactive": "morning_brief"})
        await _set_pref(user_id, "morning_brief_sent_date", today.isoformat())
    except Exception as e:
        log.warning("morning_brief.failed", error=str(e))


# ============================================================================
# Evening checkin
# ============================================================================


async def check_evening_checkin(bot: Bot, user_id: int) -> None:
    if await _is_paused(user_id):
        return

    profile = await ProfileRepo.get(user_id)
    if profile is None:
        return
    if "evening_checkin" not in profile.preferences.get("pushes", []):
        return

    local_now = await _local_now(user_id)
    today = local_now.date()
    last_sent = profile.preferences.get("evening_checkin_sent_date")
    if _was_today(last_sent, today):
        return

    # Окно: 22:00..23:30 локального времени
    if not (local_now.hour == 22 or (local_now.hour == 23 and local_now.minute < 30)):
        return

    signals = await compute_signals(user_id)
    decision = await decide_proactive(user_id, "evening_checkin", signals)
    if not decision.send:
        log.info("evening_checkin.skipped", reason=decision.reason)
        await _set_pref(user_id, "evening_checkin_sent_date", today.isoformat())
        return

    bundle = await memory.build_context(user_id, "Время вечернего чекина.")
    dynamic = memory.format_dynamic_system(bundle)
    recent = await _recent_proactive_texts(user_id, "evening_checkin", limit=3)
    avoid_block = ""
    if recent:
        avoid_block = (
            "\n\n## ИЗБЕГАЙ ПОВТОРОВ\n"
            "Вот последние твои вечерние вопросы — НЕ начинай ровно так же, "
            "формулируй иначе и цепляйся за конкретный контекст сегодняшнего дня:\n"
            + "\n".join(f"- {t[:200]}" for t in recent)
        )
    if decision.soften:
        avoid_block += (
            f"\n\n## СМЯГЧИ ТОН\nИнструкция от гейта: {decision.soften}\n"
            "Один вопрос максимум, без давления, можно просто сказать «я тут, если надо»."
        )
    try:
        text = await llm_chat(
            user_id=user_id,
            system_static=SYSTEM_BASE + "\n\n" + EVENING_CHECKIN_SYSTEM,
            system_dynamic=dynamic + avoid_block,
            messages=[{"role": "user", "content": "Спроси меня про сегодняшний день, как обычно вечером."}],
            purpose="evening_checkin",
            temperature=0.85,
        )
        await bot.send_message(chat_id=user_id, text=text, reply_markup=mood_keyboard("mood"))
        await ConversationsRepo.append(
            user_id, "assistant", text, {"proactive": "evening_checkin"}
        )
        await _set_pref(user_id, "evening_checkin_sent_date", today.isoformat())
    except Exception as e:
        log.warning("evening_checkin.failed", error=str(e))


# ============================================================================
# Habit nudges
# ============================================================================


async def check_habit_nudges(bot: Bot, user_id: int) -> None:
    if await _is_paused(user_id):
        return

    profile = await ProfileRepo.get(user_id)
    if profile is None:
        return
    pushes = profile.preferences.get("pushes", [])

    local_now = await _local_now(user_id)
    if local_now.hour < 10 or local_now.hour >= 22:
        return

    # Если пользователь сам с ботом разговаривал последние 6 часов — не спамим.
    if await _user_wrote_recently(user_id, hours=6):
        return

    # Адаптивный гейт
    signals = await compute_signals(user_id)
    decision = await decide_proactive(user_id, "habit_nudge", signals)
    if not decision.send:
        log.info("habit_nudge.skipped", reason=decision.reason)
        return

    habits = await HabitsRepo.list_active(user_id)
    if not habits:
        # Создаём дефолтные, если есть relevantные pushes
        from src.domain.models import Habit

        defaults: list[Habit] = []
        if "water" in pushes:
            defaults.append(
                Habit(
                    user_id=user_id,
                    name="water",
                    cadence="daily",
                    target={"amount": 8, "unit": "glasses"},
                )
            )
        if "workout" in pushes:
            defaults.append(
                Habit(user_id=user_id, name="workout", cadence="weekly:3")
            )
        for h in defaults:
            await HabitsRepo.create(h)
        habits = await HabitsRepo.list_active(user_id)

    day_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = day_start + timedelta(days=1)

    nudges: list[str] = []
    for h in habits:
        if h.id is None:
            continue
        n = await HabitsRepo.count_today(h.id, day_start, day_end)
        if h.name == "water" and n < 4:
            nudges.append(f"💧 Воды сегодня: {n}/{h.target.get('amount', 8) if h.target else 8}. Давай ещё стакан.")
        elif h.name == "workout" and n == 0 and local_now.hour > 17:
            nudges.append("💪 Сегодня ещё не двигался. 10 минут разминки сейчас?")
        elif h.name == "sleep" and local_now.hour >= 23:
            nudges.append("😴 Скоро 23:00. Закругляйся, сон важнее.")

    for text in nudges[:1]:  # не больше 1 за раз
        try:
            await bot.send_message(user_id, text)
            await ConversationsRepo.append(user_id, "assistant", text, {"proactive": "habit_nudge"})
        except Exception as e:
            log.warning("habit_nudge.failed", error=str(e))


# ============================================================================
# Weekly review (вс 20:00 UTC)
# ============================================================================


# ============================================================================
# Awareness anchors — короткие якоря осознанности
# ============================================================================


async def check_awareness_anchor(bot: Bot, user_id: int) -> None:
    """Шлёт одну короткую строку-якорь, чтобы переключить внимание на тело/момент.

    Адаптивная частота:
    - не более 3 в день локального
    - минимум 2.5 часа между якорями
    - если 3+ предыдущих остались без ответа — пауза 24 часа
    - окно 11:00–21:00 локально
    - не шлём, если юзер недавно (< 30 мин) что-то писал — он и так включён
    """
    if await _is_paused(user_id):
        return
    profile = await ProfileRepo.get(user_id)
    if profile is None:
        return
    if "awareness_anchor" not in profile.preferences.get("pushes", []):
        return

    local_now = await _local_now(user_id)
    if local_now.hour < 11 or local_now.hour >= 21:
        return

    # Адаптивные пороги вместо фиксированных:
    # - mingap зависит от engagement (низкое → реже)
    # - max_per_day зависит от pressure (низкое → меньше)
    signals = await compute_signals(user_id)
    if signals.quiet:
        log.info("anchor.skipped", reason="pressure quiet")
        return

    min_gap_hours = 2.5 + (1 - signals.engagement) * 3.5  # 2.5..6.0
    max_per_day = 1 if signals.pressure < 0.5 else (2 if signals.pressure < 0.75 else 3)

    last_sent = await AnchorsRepo.last_sent_at(user_id)
    if last_sent and (datetime.now(timezone.utc) - last_sent) < timedelta(hours=min_gap_hours):
        return

    # Streak: если 3+ подряд без ответа — пауза 24ч
    streak = await AnchorsRepo.ignored_streak(user_id)
    if streak >= 3:
        if last_sent and (datetime.now(timezone.utc) - last_sent) < timedelta(hours=24):
            return

    # Если юзер только что писал — он и так в моменте, не дёргаем
    pool = await get_pool()
    last_user = await pool.fetchval(
        "SELECT MAX(created_at) FROM conversations WHERE user_id=$1 AND role='user'",
        user_id,
    )
    if last_user and (datetime.now(timezone.utc) - last_user) < timedelta(minutes=30):
        return

    day_start_utc = local_now.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
    sent_today = await AnchorsRepo.count_today(user_id, day_start_utc)
    if sent_today >= max_per_day:
        return

    # LLM-гейт перед отправкой якоря
    decision = await decide_proactive(user_id, "anchor", signals)
    if not decision.send:
        log.info("anchor.skipped", reason=decision.reason)
        return

    recent = await AnchorsRepo.recent_texts(user_id, limit=8)
    avoid = ""
    if recent:
        avoid = (
            "\n\nИзбегай повторов. Последние якоря:\n"
            + "\n".join(f"- {t}" for t in recent)
        )

    bundle = await memory.build_context(user_id, "Дай якорь осознанности.")
    dynamic = memory.format_dynamic_system(bundle)

    try:
        text = await chat_json(
            user_id=user_id,
            system=AWARENESS_ANCHOR_PROMPT + avoid + "\n\n## КОНТЕКСТ\n" + dynamic[:2000],
            user_message="Сгенерируй один якорь.",
            purpose="awareness_anchor",
            max_tokens=80,
        )
    except Exception as e:
        log.warning("anchor.llm_failed", error=str(e))
        return

    text = (text or "").strip().strip('"').strip("'")
    if not text or len(text) > 140:
        log.warning("anchor.invalid_output", out=text[:200])
        return

    try:
        await bot.send_message(chat_id=user_id, text=text)
        await ConversationsRepo.append(user_id, "assistant", text, {"proactive": "anchor"})
        await AnchorsRepo.log(user_id, text)
    except Exception as e:
        log.warning("anchor.send_failed", error=str(e))


# ============================================================================
# Life state — обновление структурного портрета жизни
# ============================================================================


async def daily_life_state_update(bot: Bot, user_id: int) -> None:
    """Раз в сутки фоновый Haiku-проход обновляет life_state из новых разговоров.
    bot не используется (нет уведомлений), но сохраняем сигнатуру совместимой со scheduler.
    """
    if await _is_paused(user_id):
        return
    try:
        await update_life_state(user_id, since_hours=30)
    except Exception as e:
        log.warning("life_state.update_failed", error=str(e))


async def weekly_review(bot: Bot, user_id: int) -> None:
    """Воскресенье вечером: восстановительный обзор + план следующей недели."""
    if await _is_paused(user_id):
        return

    # 1. Хронические задачи — короткое уведомление если есть
    chronic = await TasksRepo.chronic_procrastinated(user_id, threshold=3)
    if chronic:
        lines = ["Эти задачи переносятся 3+ раза. Что мешает?"]
        for t in chronic[:5]:
            lines.append(f"• #{t.id} {t.title} (×{t.postponed_count})")
        try:
            await bot.send_message(user_id, "\n".join(lines))
        except Exception as e:
            log.warning("weekly_review.send_failed", error=str(e))

    # 2. Свежий life_state перед планированием
    try:
        await update_life_state(user_id, since_hours=24 * 7)
    except Exception as e:
        log.warning("weekly_review.life_state_failed", error=str(e))

    # 3. План на следующую неделю — для понедельника локального
    local_now = await _local_now(user_id)
    next_week_start = _week_start(local_now.date()) + timedelta(days=7)
    try:
        plan = await generate_weekly_plan(user_id, next_week_start)
    except Exception as e:
        log.warning("weekly_plan.generation_failed", error=str(e))
        plan = None

    if plan:
        try:
            text = format_plan_message(plan)
            await bot.send_message(chat_id=user_id, text=text)
            await ConversationsRepo.append(
                user_id, "assistant", text, {"proactive": "weekly_plan"}
            )
        except Exception as e:
            log.warning("weekly_plan.send_failed", error=str(e))

    # 4. Tone calibration — отдельный вызов
    try:
        from src.core.personalization import recalibrate_tone

        await recalibrate_tone(user_id)
    except Exception as e:
        log.warning("tone.recalibrate_failed", error=str(e))


# ============================================================================
# Challenge — ad-hoc предложение конкретной вещи попробовать
# ============================================================================


async def check_challenge(bot: Bot, user_id: int) -> None:
    """Раз в среду и субботу локально: предложить один челлендж.
    Если за последние 3 дня уже было предложено что-то незакрытое — пропускаем.
    """
    if await _is_paused(user_id):
        return
    profile = await ProfileRepo.get(user_id)
    if profile is None:
        return
    if "challenge" not in profile.preferences.get("pushes", []):
        return

    local_now = await _local_now(user_id)
    # Окно бодрствования (по wake_window если задан, иначе 11–21)
    wake_h_lo, wake_h_hi = _wake_hours(profile)
    # Челлендж шлём в вечернее окно последних 3 часов бодрствования
    challenge_lo = max(wake_h_lo + 6, 16)
    challenge_hi = min(wake_h_hi - 1, 21)
    if local_now.hour < challenge_lo or local_now.hour >= challenge_hi:
        return

    today = local_now.date().isoformat()
    last_sent = profile.preferences.get("challenge_sent_date")
    if last_sent == today:
        return

    # Адаптивно: челлендж только когда давно не было предлагали и pressure ок
    signals = await compute_signals(user_id)
    if signals.pressure < 0.45:
        log.info("challenge.skipped", reason=f"pressure low {signals.pressure:.2f}")
        return

    # Дни между челленджами зависит от engagement: высокий → 3, низкий → 5+
    min_days_between = 3 if signals.engagement >= 0.5 else (5 if signals.engagement >= 0.25 else 7)
    last_challenge_at = await pool_last_challenge_at(user_id)
    if last_challenge_at:
        gap = (datetime.now(timezone.utc) - last_challenge_at).total_seconds() / 86400
        if gap < min_days_between:
            return

    # Если есть незакрытый челлендж за последние 3 дня — не дублируем
    recent = await ExperimentsRepo.recent(user_id, days=3, limit=10)
    pending = [e for e in recent if e["source"] == "challenge" and e.get("accepted") is None]
    if pending:
        await _set_pref(user_id, "challenge_sent_date", today)
        return

    challenge = await generate_challenge(user_id)
    if not challenge:
        return

    exp_id = await ExperimentsRepo.create(
        user_id,
        title=str(challenge.get("what", ""))[:200],
        description=str(challenge.get("description") or challenge.get("why") or "")[:1000],
        source="challenge",
    )

    text = format_challenge_message(challenge)
    try:
        await bot.send_message(
            chat_id=user_id, text=text,
            reply_markup=experiment_kb(exp_id),
        )
        await ConversationsRepo.append(
            user_id, "assistant", text, {"proactive": "challenge", "experiment_id": exp_id}
        )
        await _set_pref(user_id, "challenge_sent_date", today)
    except Exception as e:
        log.warning("challenge.send_failed", error=str(e))
