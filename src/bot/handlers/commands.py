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
    "Commands:\n"
    "/start — onboarding (first time only)\n"
    "/help — this message\n"
    "\nMemory & state:\n"
    "/profile — your profile (name, goals, projects, tone, nudges, location)\n"
    "/facts [kind] — what I remember about you (kind: health, goal, project, preference, etc.)\n"
    "/signals — what the pattern detector noticed recently\n"
    "/jobs — when I'll message you on my own\n"
    "\nTasks:\n"
    "/task <text> — add a task\n"
    "/tasks — open tasks\n"
    "/done <id> — close a task\n"
    "\nDiary:\n"
    "/diary <text> — manual entry (or just message me plain text)\n"
    "/export — export the whole diary as JSON\n"
    "\nPlanning (agent uses what it knows + weather):\n"
    "/plan_date [context] — come up with a concrete date plan\n"
    "/plan_day [context] — lay out your day\n"
    "/activity [context] — one activity for your current state\n"
    "\nLocation:\n"
    "📎 → Location — send coordinates\n"
    "/where [city] — view / set manually (e.g. /where Lisbon)\n"
    "\nSettings:\n"
    "/timezone <TZ> — change timezone\n"
    "/pause [2h | 1d | until tomorrow | off] — mute notifications\n"
    "/tone harder | softer | reset — adjust tone\n"
    "/usage — token usage for 24h / 7d\n"
    "\nJust message me about anything — I'll remember."
)


@router.message(Command("help"))
async def on_help(message: Message) -> None:
    await message.answer(HELP_TEXT)


@router.message(Command("timezone"))
async def on_timezone(message: Message) -> None:
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Use: /timezone Europe/Kyiv")
        return
    tz = parts[1].strip()
    try:
        ZoneInfo(tz)
    except Exception:
        await message.answer(f"I don't recognize that timezone: {tz}")
        return
    await ProfileRepo.patch(message.from_user.id, timezone=tz)  # type: ignore[union-attr]
    await message.answer(f"Okay, switched to {tz}")


PAUSE_RE = re.compile(r"^(\d+)([hdm])$", re.IGNORECASE)


@router.message(Command("pause"))
async def on_pause(message: Message) -> None:
    parts = (message.text or "").split(maxsplit=1)
    user_id = message.from_user.id  # type: ignore[union-attr]
    arg = parts[1].strip().lower() if len(parts) >= 2 else "1d"

    if arg in ("off", "no", "stop"):
        await ProfileRepo.set_paused(user_id, None)
        await message.answer("Pause lifted, notifications are back on.")
        return

    until = _parse_pause(arg)
    if until is None:
        await message.answer(
            "Use: /pause 2h, /pause 1d, /pause until tomorrow, /pause off"
        )
        return

    await ProfileRepo.set_paused(user_id, until)
    await message.answer(f"Paused until {until.strftime('%Y-%m-%d %H:%M UTC')}.")


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
        await message.answer("Use: /tone harder | softer | reset")
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
        await message.answer("Don't recognize that. Use harder / softer / reset.")
        return
    prefs = dict(profile.preferences)
    prefs["tone"] = tone
    await ProfileRepo.patch(user_id, preferences=prefs)
    await message.answer(
        f"Tone updated:\n"
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
        f"Usage (24h):\n{fmt(day)}\n\nUsage (7d):\n{fmt(week)}"
    )


@router.message(Command("profile"))
async def on_profile(message: Message) -> None:
    user_id = message.from_user.id  # type: ignore[union-attr]
    profile = await ProfileRepo.get(user_id)
    if profile is None:
        await message.answer("Profile not created yet. Hit /start.")
        return

    loc = await LocationsRepo.latest(user_id)
    tone = profile.preferences.get("tone", {}) if isinstance(profile.preferences, dict) else {}
    pushes = profile.preferences.get("pushes", []) if isinstance(profile.preferences, dict) else []
    push_notes = profile.preferences.get("push_notes", "")

    lines = [
        f"Name: {profile.display_name or '?'}",
        f"Timezone: {profile.timezone}",
        f"Location: {loc.get('label') if loc else 'not set'}",
        f"Onboarding: {'completed ' + profile.onboarding_completed_at.strftime('%Y-%m-%d') if profile.onboarding_completed_at else 'not completed'}",
        "",
        "Goals:",
    ]
    for g in (profile.goals or [])[:10]:
        lines.append(f"  • {g}")
    if not profile.goals:
        lines.append("  —")

    lines.append("")
    lines.append("Projects:")
    for p in (profile.projects or [])[:10]:
        lines.append(f"  • {p}")
    if not profile.projects:
        lines.append("  —")

    lines.append("")
    lines.append(f"Nudges: {', '.join(pushes) if pushes else 'none'}")
    if push_notes:
        lines.append(f"  preferences: {push_notes}")

    if tone:
        lines.append("")
        lines.append(
            f"Tone: warmth={tone.get('warmth', 0.7):.2f} "
            f"directness={tone.get('directness', 0.5):.2f} "
            f"humor={tone.get('humor', 0.6):.2f} "
            f"push={tone.get('push_intensity', 0.5):.2f}"
        )
    if profile.paused_until and profile.paused_until > datetime.now(timezone.utc):
        lines.append(f"\n⏸ Paused until {profile.paused_until:%Y-%m-%d %H:%M UTC}")

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
        await message.answer("No facts extracted yet. Talk to me — I'll remember.")
        return

    by_kind: dict[str, list[str]] = {}
    for r in rows:
        by_kind.setdefault(r["kind"], []).append(
            f"  • {r['content']}"
        )

    lines: list[str] = []
    if kind_filter:
        lines.append(f"Facts [{kind_filter}] — {len(rows)}:")
        lines.extend(by_kind.get(kind_filter, []))
    else:
        lines.append(f"Extracted {len(rows)} facts (latest). By kind:\n")
        for k in sorted(by_kind.keys()):
            lines.append(f"[{k}] ({len(by_kind[k])})")
            lines.extend(by_kind[k])
            lines.append("")

    text = "\n".join(lines)
    # Telegram message limit
    if len(text) > 3800:
        text = text[:3800] + "\n…(truncated)"
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
            "The pattern detector hasn't flagged anything yet. That's fine — it looks at diaries "
            "and messages cumulatively (low mood 3+ days, fatigue markers, etc.) and only fires "
            "on clear triggers."
        )
        return
    lines = ["Detector signals:"]
    for r in rows:
        cooldown = ""
        if r["cooldown_until"] and r["cooldown_until"] > datetime.now(timezone.utc):
            cooldown = " (cooldown active)"
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
        await message.answer("Go through /start first.")
        return

    pushes = profile.preferences.get("pushes", []) if isinstance(profile.preferences, dict) else []
    tz = ZoneInfo(profile.timezone or "UTC")
    now_local = datetime.now(tz)

    lines = [f"Your local time: {now_local:%Y-%m-%d %H:%M %Z}\n"]

    if profile.paused_until and profile.paused_until > datetime.now(timezone.utc):
        lines.append(f"⏸ All notifications paused until {profile.paused_until:%Y-%m-%d %H:%M UTC}\n")

    if "morning_brief" in pushes:
        lines.append("☀ Morning brief — between 11:00 and 14:00 local, if you haven't messaged today")
    else:
        lines.append("☀ Morning brief — off")

    if "evening_checkin" in pushes:
        lines.append("🌙 Evening check-in — between 22:00 and 23:30 local")
    else:
        lines.append("🌙 Evening check-in — off")

    if "workout" in pushes or "water" in pushes or "sleep" in pushes:
        lines.append("💪 Habit nudges — every 3 hours during the active window (10:00-22:00)")

    lines.append("🧠 Pattern detector — every 30 min in the 13:00-19:00 window (if there are fresh triggers)")
    lines.append("📅 Sunday 20:00 UTC — weekly review + tone calibration")
    lines.append("\n/pause turns everything off temporarily.")

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
        f"Dedup: removed {len(rows)} exact duplicates. Active facts left: {total_left}.\n"
        f"Going forward, duplicates are blocked automatically at insert time (93% similarity threshold)."
    )


@router.message(Command("export"))
async def on_export(message: Message) -> None:
    user_id = message.from_user.id  # type: ignore[union-attr]
    rows = await DiaryRepo.export_all(user_id)
    payload = json.dumps(rows, ensure_ascii=False, indent=2, default=str).encode("utf-8")
    file = BufferedInputFile(payload, filename=f"diary_{datetime.utcnow():%Y%m%d}.json")
    await message.answer_document(file, caption=f"Diary: {len(rows)} entries")
