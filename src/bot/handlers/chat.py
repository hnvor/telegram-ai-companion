"""Главный обработчик текстовых сообщений (всё что не команда и не FSM)."""

import asyncio
import re

import structlog
from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from src.core import memory
from src.core.extraction import extract_and_store
from src.core.llm import chat_with_tools
from src.core.prompts import SYSTEM_BASE
from src.core.routines import detect_and_log_routines
from src.core.task_closure import auto_close_from_message
from src.core.tools import TOOLS_SPEC
from src.db.repo import ConversationsRepo, ProfileRepo, TasksRepo
from src.domain.models import TaskItem

router = Router()
log = structlog.get_logger()

TASK_PATTERN = re.compile(r"\[task:\s*(.+?)\]", re.IGNORECASE)


@router.message(F.text & ~F.text.startswith("/"))
async def on_text(message: Message, state: FSMContext) -> None:
    # Если пользователь в каком-то FSM (онбординг и т.п.) — не перехватываем
    if await state.get_state() is not None:
        return

    user_id = message.from_user.id  # type: ignore[union-attr]
    text = message.text or ""

    profile = await ProfileRepo.get(user_id)
    if profile is None or profile.onboarding_completed_at is None:
        await message.answer(
            "Сначала пройдём короткий онбординг — нажми /start"
        )
        return

    # Сохраняем входящее сообщение
    user_msg_id = await ConversationsRepo.append(user_id, "user", text)

    # Собираем контекст и отвечаем
    bundle = await memory.build_context(user_id, text)
    dynamic = memory.format_dynamic_system(bundle)
    messages = memory.to_anthropic_messages(bundle.recent_conversation, text)

    await message.bot.send_chat_action(message.chat.id, "typing")

    try:
        response = await chat_with_tools(
            user_id=user_id,
            system_static=SYSTEM_BASE,
            system_dynamic=dynamic,
            messages=messages,
            tools=TOOLS_SPEC,
            purpose="chat",
        )
    except Exception as e:
        log.exception("chat.llm_failed", error=str(e))
        await message.answer("Что-то пошло не так с моим мозгом, попробуй ещё раз через минуту.")
        return

    cleaned, task_titles = _extract_task_markers(response)
    if cleaned:
        await message.answer(cleaned)
    else:
        await message.answer(response)

    # Логируем ответ
    await ConversationsRepo.append(user_id, "assistant", response)

    # Создаём задачи из маркеров
    for title in task_titles:
        try:
            t = await TasksRepo.create(TaskItem(user_id=user_id, title=title))
            await message.answer(f"📌 Записал задачу #{t.id}: {title}")
        except Exception as e:
            log.warning("task.create_failed", error=str(e))

    # Извлекаем факты в фоне
    asyncio.create_task(extract_and_store(user_id, text, source_message_id=user_msg_id))

    # Авто-детект закрытых задач из реплики (предлагает кнопкой ✅, не закрывает молча)
    asyncio.create_task(auto_close_from_message(message.bot, user_id, text))

    # Авто-детект бытовых рутин (помылся, побрился, гулял) — тихо обновляет last_done_at
    asyncio.create_task(detect_and_log_routines(user_id, text))


def _extract_task_markers(text: str) -> tuple[str, list[str]]:
    """Достаёт `[task: ...]` маркеры из ответа агента, возвращает чистый текст и список задач."""
    titles = [m.group(1).strip() for m in TASK_PATTERN.finditer(text)]
    cleaned = TASK_PATTERN.sub("", text).strip()
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned, titles
