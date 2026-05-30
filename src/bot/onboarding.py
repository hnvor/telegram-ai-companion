"""FSM первичной анкеты."""

import json
from datetime import datetime

import structlog
from aiogram import F, Router
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message
from zoneinfo import ZoneInfo

from src.bot.keyboards import remove_kb
from src.config import settings
from src.core.embeddings import embed
from src.core.llm import chat_json
from src.core.prompts import GOALS_PARSE_PROMPT, PUSHES_PARSE_PROMPT
from src.db.repo import ConversationsRepo, FactsRepo, ProfileRepo
from src.domain.models import Fact, Profile

log = structlog.get_logger()
VALID_PUSHES = frozenset({"water", "sleep", "workout", "evening_checkin", "morning_brief"})

router = Router()


class Onboard(StatesGroup):
    NAME = State()
    TIMEZONE = State()
    GOALS = State()
    PROJECTS = State()
    HEALTH = State()
    PUSHES = State()
    TONE = State()


@router.message(CommandStart())
async def start(message: Message, state: FSMContext) -> None:
    user_id = message.from_user.id  # type: ignore[union-attr]

    profile = await ProfileRepo.get(user_id)
    if profile and profile.onboarding_completed_at:
        await message.answer(
            "I already know you 👋. Just message me — about tasks, how you feel, thoughts.\n"
            "Commands: /help",
        )
        return

    if profile is None:
        profile = Profile(user_id=user_id, timezone=settings.default_timezone)
        await ProfileRepo.upsert(profile)

    await state.set_state(Onboard.NAME)
    await message.answer(
        "Hi. I'm your personal assistant — I'll remember your tasks and how you're doing, "
        "nudge you when needed, and back off when you're tired.\n\n"
        "First, a short 5-7 minute onboarding. A one-line answer to each question is fine — "
        "everything gets refined as we go.\n\n"
        "What should I call you?"
    )


@router.message(Onboard.NAME, F.text)
async def on_name(message: Message, state: FSMContext) -> None:
    name = message.text.strip()  # type: ignore[union-attr]
    user_id = message.from_user.id  # type: ignore[union-attr]
    await ProfileRepo.patch(user_id, display_name=name)
    await state.set_state(Onboard.TIMEZONE)
    await message.answer(
        f"Okay, {name}. Timezone?\n"
        f"Default right now is {settings.default_timezone}. "
        f"If that works — type \"ok\". Otherwise give your own, e.g. Europe/Kyiv or Asia/Bangkok."
    )


@router.message(Onboard.TIMEZONE, F.text)
async def on_tz(message: Message, state: FSMContext) -> None:
    user_id = message.from_user.id  # type: ignore[union-attr]
    text = message.text.strip()  # type: ignore[union-attr]
    if text.lower() not in ("ок", "ok", "да", "yes", "okay", ""):
        try:
            ZoneInfo(text)
            await ProfileRepo.patch(user_id, timezone=text)
        except Exception:
            await message.answer("I don't recognize that timezone. Try the Europe/Kyiv format or type \"ok\".")
            return

    await state.set_state(Onboard.GOALS)
    await message.answer(
        "What are your 1-3 main goals for the next 3 months right now? "
        "A comma-separated list or one per line is fine."
    )


@router.message(Onboard.GOALS, F.text)
async def on_goals(message: Message, state: FSMContext) -> None:
    user_id = message.from_user.id  # type: ignore[union-attr]
    text = (message.text or "").strip()

    goals: list[str] = []
    context_note: str | None = None
    parsed = await _parse_with_llm(user_id, GOALS_PARSE_PROMPT, text)
    if parsed and isinstance(parsed.get("goals"), list):
        goals = [str(g)[:200] for g in parsed["goals"] if str(g).strip()][:5]
        context_note = parsed.get("context") or None
    if not goals:
        # фоллбэк на простой split, если LLM не справился
        goals = _split_list(text)
    if not goals:
        # вообще ничего не вычленилось — сохраним сырой текст как контекст
        goals = [text[:200]]

    await ProfileRepo.patch(user_id, goals=goals)
    facts_to_save = [f"Goal: {g}" for g in goals]
    if context_note:
        facts_to_save.append(f"Goals context: {context_note}")
    await _save_facts(user_id, "goal", facts_to_save)

    await state.set_state(Onboard.PROJECTS)
    await message.answer("What projects are you working on right now? (a list or a single line is fine)")


@router.message(Onboard.PROJECTS, F.text)
async def on_projects(message: Message, state: FSMContext) -> None:
    user_id = message.from_user.id  # type: ignore[union-attr]
    projects = _split_list(message.text)  # type: ignore[arg-type]
    await ProfileRepo.patch(user_id, projects=projects)
    await _save_facts(user_id, "project", [f"User project: {p}" for p in projects])

    await state.set_state(Onboard.HEALTH)
    await message.answer(
        "How's your health right now? Anything bothering you, anything you'd like to improve? "
        "Any ongoing habits/medications? One long sentence is fine."
    )


@router.message(Onboard.HEALTH, F.text)
async def on_health(message: Message, state: FSMContext) -> None:
    user_id = message.from_user.id  # type: ignore[union-attr]
    text = message.text.strip()  # type: ignore[union-attr]
    if text and text.lower() not in ("нет", "no", "none", "пропустить", "skip", "-"):
        await _save_facts(user_id, "health", [f"Health at start: {text}"])

    await state.set_state(Onboard.PUSHES)
    await message.answer(
        "Which proactive nudges do you want? A few, comma-separated:\n"
        "• water — remind you to drink\n"
        "• sleep — push you to sleep on time\n"
        "• workouts — nudge you to move\n"
        "• evening check-in — ask how the day went\n"
        "• morning brief — plan for the day\n"
        "If you want all of them — type \"all\"."
    )


@router.message(Onboard.PUSHES, F.text)
async def on_pushes(message: Message, state: FSMContext) -> None:
    user_id = message.from_user.id  # type: ignore[union-attr]
    text = (message.text or "").strip()

    pushes: list[str] = []
    notes: str | None = None
    parsed = await _parse_with_llm(user_id, PUSHES_PARSE_PROMPT, text)
    if parsed and isinstance(parsed.get("pushes"), list):
        pushes = [str(p) for p in parsed["pushes"] if str(p) in VALID_PUSHES]
        notes = parsed.get("notes") or None

    if not pushes:
        # фоллбэк: дефолт «всё кроме воды»
        pushes = ["sleep", "workout", "evening_checkin", "morning_brief"]

    profile = await ProfileRepo.get(user_id)
    prefs = dict(profile.preferences) if profile else {}
    prefs["pushes"] = pushes
    if notes:
        prefs["push_notes"] = notes
    await ProfileRepo.patch(user_id, preferences=prefs)

    if notes:
        await _save_facts(user_id, "preference", [f"Nudge preference: {notes}"])

    await state.set_state(Onboard.TONE)
    await message.answer(
        "Last question. Which tone feels right to start with?\n"
        "1) friend — warm, a bit of humor, gentle push\n"
        "2) coach — direct, demanding\n"
        "3) mentor — calm, asks questions\n"
        "(I'll adapt on my own based on your reactions later)"
    )


@router.message(Onboard.TONE, F.text)
async def on_tone(message: Message, state: FSMContext) -> None:
    user_id = message.from_user.id  # type: ignore[union-attr]
    text = message.text.strip().lower()  # type: ignore[union-attr]

    if text.startswith("1") or "friend" in text:
        tone = {"warmth": 0.8, "directness": 0.5, "humor": 0.7, "push_intensity": 0.5}
    elif text.startswith("2") or "coach" in text:
        tone = {"warmth": 0.4, "directness": 0.9, "humor": 0.3, "push_intensity": 0.85}
    elif text.startswith("3") or "mentor" in text:
        tone = {"warmth": 0.7, "directness": 0.4, "humor": 0.4, "push_intensity": 0.3}
    else:
        tone = {"warmth": 0.7, "directness": 0.6, "humor": 0.6, "push_intensity": 0.55}

    profile = await ProfileRepo.get(user_id)
    prefs = dict(profile.preferences) if profile else {}
    prefs["tone"] = tone
    await ProfileRepo.patch(
        user_id,
        preferences=prefs,
        onboarding_completed_at=datetime.utcnow(),
    )

    await state.clear()
    await message.answer(
        "Done 👌\n\n"
        "From here — just message me. Text or voice. I'll remember everything.\n"
        "Start with whatever's on your mind right now. Or type /help.",
        reply_markup=remove_kb(),
    )

    # Записываем сам факт онбординга в conversations для контекста
    await ConversationsRepo.append(
        user_id,
        "system",
        "Onboarding complete. Profile set up.",
    )


def _split_list(text: str) -> list[str]:
    """Разбивает текст по запятым / новым строкам в список непустых строк."""
    if not text:
        return []
    parts: list[str] = []
    for line in text.replace(";", ",").split("\n"):
        for chunk in line.split(","):
            chunk = chunk.strip(" -•*\t")
            if chunk:
                parts.append(chunk)
    return parts


async def _save_facts(user_id: int, kind: str, contents: list[str]) -> None:
    if not contents:
        return
    vecs = await embed(contents)
    for content, vec in zip(contents, vecs, strict=False):
        fact = Fact(user_id=user_id, kind=kind, content=content, confidence=0.9)  # type: ignore[arg-type]
        try:
            await FactsRepo.insert(fact, vec)
        except Exception:
            pass


async def _parse_with_llm(user_id: int, system: str, user_text: str) -> dict | None:
    """Парсит свободный ответ пользователя через Haiku → dict. None если не вышло."""
    try:
        raw = await chat_json(
            user_id=user_id,
            system=system,
            user_message=user_text,
            purpose="onboarding_parse",
            max_tokens=400,
        )
    except Exception as e:
        log.warning("onboarding.parse_failed", error=str(e))
        return None
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text
        if text.endswith("```"):
            text = text.rsplit("```", 1)[0]
    try:
        return json.loads(text.strip())
    except Exception as e:
        log.warning("onboarding.parse_json_failed", error=str(e), raw=text[:200])
        return None
