"""Сервисные команды: /help, /timezone, /pause, /tone, /usage, /export"""

import json
import re
from datetime import datetime, timedelta, timezone

import structlog
from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import BufferedInputFile, Message
from zoneinfo import ZoneInfo

from src.db.client import get_pool
from src.db.repo import DiaryRepo, ProfileRepo, UsageRepo
from src.db.repo_extra import LocationsRepo

router = Router()
log = structlog.get_logger()

HELP_TEXT = (
    "Команды:\n"
    "/start — онбординг (только в первый раз)\n"
    "/help — это сообщение\n"
    "\nПамять и состояние:\n"
    "/profile — твой профиль (имя, цели, проекты, тон, пуши, локация)\n"
    "/facts [kind] — что я запомнил о тебе (kind: health, goal, project, preference, и т.д.)\n"
    "/signals — что детектор паттернов заметил недавно\n"
    "/jobs — когда я тебе планирую написать сам\n"
    "\nЗадачи:\n"
    "/task <текст> — добавить задачу\n"
    "/tasks — открытые задачи\n"
    "/done <id> — закрыть задачу\n"
    "\nДневник:\n"
    "/diary <текст> — запись вручную (можно и просто текстом боту)\n"
    "/export — выгрузить весь дневник в JSON\n"
    "\nПланирование (агент использует свои знания + погоду):\n"
    "/plan_date [контекст] — придумать конкретный план свидания\n"
    "/plan_day [контекст] — расписать день\n"
    "/activity [контекст] — одна активность под текущее состояние\n"
    "\nЛокация:\n"
    "📎 → Геопозиция — отправить координаты\n"
    "/where [город] — посмотреть / задать вручную (например: /where Хошимин)\n"
    "\nНастройки:\n"
    "/timezone <TZ> — сменить часовой пояс\n"
    "/pause [2h | 1d | until tomorrow | off] — отключить нотификации\n"
    "/tone harder | softer | reset — подкрутить тон\n"
    "/usage — расход токенов за 24ч / 7д\n"
    "\nПросто пиши мне о чём угодно — я запомню."
)


@router.message(Command("help"))
async def on_help(message: Message) -> None:
    await message.answer(HELP_TEXT)


@router.message(Command("timezone"))
async def on_timezone(message: Message) -> None:
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Используй: /timezone Europe/Kyiv")
        return
    tz = parts[1].strip()
    try:
        ZoneInfo(tz)
    except Exception:
        await message.answer(f"Не узнаю такой пояс: {tz}")
        return
    await ProfileRepo.patch(message.from_user.id, timezone=tz)  # type: ignore[union-attr]
    await message.answer(f"Окей, переключил на {tz}")


PAUSE_RE = re.compile(r"^(\d+)([hdm])$", re.IGNORECASE)


@router.message(Command("pause"))
async def on_pause(message: Message) -> None:
    parts = (message.text or "").split(maxsplit=1)
    user_id = message.from_user.id  # type: ignore[union-attr]
    arg = parts[1].strip().lower() if len(parts) >= 2 else "1d"

    if arg in ("off", "нет", "stop"):
        await ProfileRepo.set_paused(user_id, None)
        await message.answer("Пауза снята, нотификации снова работают.")
        return

    until = _parse_pause(arg)
    if until is None:
        await message.answer(
            "Используй: /pause 2h, /pause 1d, /pause until tomorrow, /pause off"
        )
        return

    await ProfileRepo.set_paused(user_id, until)
    await message.answer(f"Пауза до {until.strftime('%Y-%m-%d %H:%M UTC')}.")


def _parse_pause(arg: str) -> datetime | None:
    arg = arg.strip().lower()
    now = datetime.now(timezone.utc)
    if arg == "until tomorrow" or arg == "tomorrow":
        return now.replace(hour=8, minute=0, second=0, microsecond=0) + timedelta(days=1)
    m = PAUSE_RE.match(arg)
    if m:
        n, unit = int(m.group(1)), m.group(2).lower()
        if unit == "h":
            return now + timedelta(hours=n)
        if unit == "d":
            return now + timedelta(days=n)
        if unit == "m":
            return now + timedelta(minutes=n)
    return None


@router.message(Command("tone"))
async def on_tone(message: Message) -> None:
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Используй: /tone harder | softer | reset")
        return
    direction = parts[1].strip().lower()
    user_id = message.from_user.id  # type: ignore[union-attr]
    profile = await ProfileRepo.get(user_id)
    if profile is None:
        return
    tone = dict(profile.preferences.get("tone", {}))
    if direction == "harder":
        tone["directness"] = min(1.0, tone.get("directness", 0.5) + 0.15)
        tone["push_intensity"] = min(1.0, tone.get("push_intensity", 0.5) + 0.15)
        tone["warmth"] = max(0.0, tone.get("warmth", 0.7) - 0.1)
    elif direction == "softer":
        tone["directness"] = max(0.0, tone.get("directness", 0.5) - 0.15)
        tone["push_intensity"] = max(0.0, tone.get("push_intensity", 0.5) - 0.15)
        tone["warmth"] = min(1.0, tone.get("warmth", 0.7) + 0.1)
    elif direction == "reset":
        tone = {"warmth": 0.7, "directness": 0.5, "humor": 0.6, "push_intensity": 0.5}
    else:
        await message.answer("Не узнаю. Используй harder / softer / reset.")
        return
    prefs = dict(profile.preferences)
    prefs["tone"] = tone
    await ProfileRepo.patch(user_id, preferences=prefs)
    await message.answer(
        f"Тон обновлён:\n"
        f"warmth={tone['warmth']:.2f} directness={tone['directness']:.2f} "
        f"humor={tone.get('humor', 0.6):.2f} push={tone['push_intensity']:.2f}"
    )


@router.message(Command("usage"))
async def on_usage(message: Message) -> None:
    user_id = message.from_user.id  # type: ignore[union-attr]
    now = datetime.now(timezone.utc)
    day = await UsageRepo.summary(user_id, now - timedelta(hours=24))
    week = await UsageRepo.summary(user_id, now - timedelta(days=7))

    def fmt(rows: list[dict]) -> str:
        if not rows:
            return "  —"
        out = []
        total_in = 0
        total_out = 0
        for r in rows:
            out.append(
                f"  {r['model']}: in={r['input_tokens']} out={r['output_tokens']} "
                f"cache_read={r['cache_read']} ({r['calls']} calls)"
            )
            total_in += r["input_tokens"]
            total_out += r["output_tokens"]
        cost_usd = total_in * 3 / 1_000_000 + total_out * 15 / 1_000_000
        out.append(f"  ≈ ${cost_usd:.4f}")
        return "\n".join(out)

    await message.answer(
        f"Расход за 24ч:\n{fmt(day)}\n\nРасход за 7д:\n{fmt(week)}"
    )


@router.message(Command("profile"))
async def on_profile(message: Message) -> None:
    user_id = message.from_user.id  # type: ignore[union-attr]
    profile = await ProfileRepo.get(user_id)
    if profile is None:
        await message.answer("Профиль ещё не создан. Жми /start.")
        return

    loc = await LocationsRepo.latest(user_id)
    tone = profile.preferences.get("tone", {}) if isinstance(profile.preferences, dict) else {}
    pushes = profile.preferences.get("pushes", []) if isinstance(profile.preferences, dict) else []
    push_notes = profile.preferences.get("push_notes", "")

    lines = [
        f"Имя: {profile.display_name or '?'}",
        f"Часовой пояс: {profile.timezone}",
        f"Локация: {loc.get('label') if loc else 'не задана'}",
        f"Онбординг: {'пройден ' + profile.onboarding_completed_at.strftime('%Y-%m-%d') if profile.onboarding_completed_at else 'не пройден'}",
        "",
        "Цели:",
    ]
    for g in (profile.goals or [])[:10]:
        lines.append(f"  • {g}")
    if not profile.goals:
        lines.append("  —")

    lines.append("")
    lines.append("Проекты:")
    for p in (profile.projects or [])[:10]:
        lines.append(f"  • {p}")
    if not profile.projects:
        lines.append("  —")

    lines.append("")
    lines.append(f"Пуши: {', '.join(pushes) if pushes else 'нет'}")
    if push_notes:
        lines.append(f"  пожелания: {push_notes}")

    if tone:
        lines.append("")
        lines.append(
            f"Тон: warmth={tone.get('warmth', 0.7):.2f} "
            f"directness={tone.get('directness', 0.5):.2f} "
            f"humor={tone.get('humor', 0.6):.2f} "
            f"push={tone.get('push_intensity', 0.5):.2f}"
        )
    if profile.paused_until and profile.paused_until > datetime.now(timezone.utc):
        lines.append(f"\n⏸ Пауза до {profile.paused_until:%Y-%m-%d %H:%M UTC}")

    await message.answer("\n".join(lines))


@router.message(Command("facts"))
async def on_facts(message: Message) -> None:
    user_id = message.from_user.id  # type: ignore[union-attr]
    parts = (message.text or "").split(maxsplit=1)
    kind_filter = parts[1].strip().lower() if len(parts) >= 2 else None

    pool = await get_pool()
    if kind_filter:
        rows = await pool.fetch(
            """
            SELECT kind, content, confidence, created_at FROM facts
            WHERE user_id = $1 AND superseded_by IS NULL AND kind = $2
            ORDER BY created_at DESC LIMIT 50
            """,
            user_id,
            kind_filter,
        )
    else:
        rows = await pool.fetch(
            """
            SELECT kind, content, confidence, created_at FROM facts
            WHERE user_id = $1 AND superseded_by IS NULL
            ORDER BY created_at DESC LIMIT 30
            """,
            user_id,
        )

    if not rows:
        await message.answer("Фактов пока не извлёк. Поговори со мной — я буду запоминать.")
        return

    by_kind: dict[str, list[str]] = {}
    for r in rows:
        by_kind.setdefault(r["kind"], []).append(
            f"  • {r['content']}"
        )

    lines: list[str] = []
    if kind_filter:
        lines.append(f"Факты [{kind_filter}] — {len(rows)}:")
        lines.extend(by_kind.get(kind_filter, []))
    else:
        lines.append(f"Извлечено {len(rows)} фактов (последние). По типам:\n")
        for k in sorted(by_kind.keys()):
            lines.append(f"[{k}] ({len(by_kind[k])})")
            lines.extend(by_kind[k])
            lines.append("")

    text = "\n".join(lines)
    # Telegram message limit
    if len(text) > 3800:
        text = text[:3800] + "\n…(обрезано)"
    await message.answer(text)


@router.message(Command("signals"))
async def on_signals(message: Message) -> None:
    user_id = message.from_user.id  # type: ignore[union-attr]
    pool = await get_pool()
    rows = await pool.fetch(
        """
        SELECT signal_kind, severity, action_taken, created_at, cooldown_until
        FROM pattern_signals
        WHERE user_id = $1
        ORDER BY created_at DESC LIMIT 15
        """,
        user_id,
    )
    if not rows:
        await message.answer(
            "Детектор паттернов пока ничего не отметил. Это норм — он смотрит на дневники "
            "и сообщения накопительно (низкий mood 3+ дня, маркеры усталости и т.д.) и срабатывает только "
            "при явных триггерах."
        )
        return
    lines = ["Сигналы детектора:"]
    for r in rows:
        cooldown = ""
        if r["cooldown_until"] and r["cooldown_until"] > datetime.now(timezone.utc):
            cooldown = " (cooldown активен)"
        lines.append(
            f"• {r['created_at']:%Y-%m-%d %H:%M} [{r['severity']}] {r['signal_kind']} → {r['action_taken']}{cooldown}"
        )
    await message.answer("\n".join(lines))


@router.message(Command("jobs"))
async def on_jobs(message: Message) -> None:
    """Показывает когда бот следующий раз сам тебе напишет."""
    user_id = message.from_user.id  # type: ignore[union-attr]
    profile = await ProfileRepo.get(user_id)
    if profile is None:
        await message.answer("Сначала пройди /start.")
        return

    pushes = profile.preferences.get("pushes", []) if isinstance(profile.preferences, dict) else []
    tz = ZoneInfo(profile.timezone or "UTC")
    now_local = datetime.now(tz)

    lines = [f"Сейчас у тебя: {now_local:%Y-%m-%d %H:%M %Z}\n"]

    if profile.paused_until and profile.paused_until > datetime.now(timezone.utc):
        lines.append(f"⏸ Все нотификации на паузе до {profile.paused_until:%Y-%m-%d %H:%M UTC}\n")

    if "morning_brief" in pushes:
        lines.append("☀ Утренний брифинг — между 11:00 и 14:00 локально, если ты ещё не написал сегодня")
    else:
        lines.append("☀ Утренний брифинг — выключен")

    if "evening_checkin" in pushes:
        lines.append("🌙 Вечерний чекин — между 22:00 и 23:30 локально")
    else:
        lines.append("🌙 Вечерний чекин — выключен")

    if "workout" in pushes or "water" in pushes or "sleep" in pushes:
        lines.append("💪 Habit nudges — раз в 3 часа в активное окно (10:00-22:00)")

    lines.append("🧠 Pattern detector — каждые 30 мин в окне 13:00-19:00 (если есть свежие триггеры)")
    lines.append("📅 Воскресенье 20:00 UTC — еженедельный обзор + калибровка тона")
    lines.append("\nКоманда /pause выключит всё временно.")

    await message.answer("\n".join(lines))


@router.message(Command("dedup_facts"))
async def on_dedup_facts(message: Message) -> None:
    """Удаляет точные дубликаты по (user_id, kind, content). Семантические дубли
    оставляет — они в редких случаях полезны как разные формулировки. Если хочешь жёсткий
    semantic dedup — скажешь."""
    user_id = message.from_user.id  # type: ignore[union-attr]
    pool = await get_pool()
    deleted = await pool.fetchval(
        """
        WITH ranked AS (
            SELECT id, ROW_NUMBER() OVER (
                PARTITION BY user_id, kind, content ORDER BY id ASC
            ) AS rn
            FROM facts
            WHERE user_id = $1 AND superseded_by IS NULL
        )
        DELETE FROM facts WHERE id IN (SELECT id FROM ranked WHERE rn > 1)
        RETURNING id
        """,
        user_id,
    )
    deleted_count = 0 if deleted is None else 1
    # Pool.fetchval возвращает только 1 запись, нужно использовать fetch
    rows = await pool.fetch(
        """
        WITH ranked AS (
            SELECT id, ROW_NUMBER() OVER (
                PARTITION BY user_id, kind, content ORDER BY id ASC
            ) AS rn
            FROM facts
            WHERE user_id = $1 AND superseded_by IS NULL
        )
        SELECT id FROM ranked WHERE rn > 1
        """,
        user_id,
    )
    if rows:
        ids = [r["id"] for r in rows]
        await pool.execute("DELETE FROM facts WHERE id = ANY($1::bigint[])", ids)

    total_left = await pool.fetchval(
        "SELECT COUNT(*) FROM facts WHERE user_id = $1 AND superseded_by IS NULL",
        user_id,
    )
    await message.answer(
        f"Дедуп: удалил {len(rows)} точных дублей. Осталось активных фактов: {total_left}.\n"
        f"Дальше дубли блокируются автоматически на уровне insert (порог 93% сходства)."
    )


@router.message(Command("export"))
async def on_export(message: Message) -> None:
    user_id = message.from_user.id  # type: ignore[union-attr]
    rows = await DiaryRepo.export_all(user_id)
    payload = json.dumps(rows, ensure_ascii=False, indent=2, default=str).encode("utf-8")
    file = BufferedInputFile(payload, filename=f"diary_{datetime.utcnow():%Y%m%d}.json")
    await message.answer_document(file, caption=f"Дневник: {len(rows)} записей")
