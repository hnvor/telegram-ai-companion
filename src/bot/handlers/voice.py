"""Handler голосовых сообщений: скачать → транскрибировать → передать в chat."""

import asyncio
import io
import re

import structlog
from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from src.config import settings
from src.core import memory
from src.core.extraction import extract_and_store
from src.core.llm import chat_with_tools
from src.core.prompts import SYSTEM_BASE
from src.core.tools import TOOLS_SPEC
from src.db.repo import ConversationsRepo, ProfileRepo, TasksRepo
from src.domain.models import TaskItem
from src.services.voice import transcribe_voice

router = Router()
log = structlog.get_logger()

TASK_PATTERN = re.compile(r"\[task:\s*(.+?)\]", re.IGNORECASE)


@router.message(F.voice)
async def on_voice(message: Message, state: FSMContext) -> None:
    if not settings.enable_voice:
        await message.answer("Голос пока отключён. Напиши текстом.")
        return

    if await state.get_state() is not None:
        await message.answer("Сейчас идёт онбординг — ответь, пожалуйста, текстом.")
        return

    user_id = message.from_user.id  # type: ignore[union-attr]
    profile = await ProfileRepo.get(user_id)
    if profile is None or profile.onboarding_completed_at is None:
        await message.answer("Сначала пройдём онбординг — нажми /start")
        return

    voice = message.voice
    if voice is None:
        return

    await message.bot.send_chat_action(message.chat.id, "typing")

    # Скачиваем .ogg
    file = await message.bot.get_file(voice.file_id)
    if file.file_path is None:
        await message.answer("Не получилось скачать голосовое.")
        return
    buf = io.BytesIO()
    await message.bot.download_file(file.file_path, buf)
    ogg_bytes = buf.getvalue()

    text = await transcribe_voice(ogg_bytes)
    if not text:
        await message.answer("Не получилось распознать. Попробуй ещё раз или напиши текстом.")
        return

    # Дальше — как обычное текстовое сообщение
    user_msg_id = await ConversationsRepo.append(
        user_id, "user", text, {"voice": True}
    )

    bundle = await memory.build_context(user_id, text)
    dynamic = memory.format_dynamic_system(bundle)
    messages = memory.to_anthropic_messages(bundle.recent_conversation, text)

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
        log.exception("voice.llm_failed", error=str(e))
        await message.answer(f"📝 Распознал: «{text}»\n\nНо что-то с ответом не вышло, попробуй ещё.")
        return

    cleaned, task_titles = _extract_tasks(response)
    await message.answer(f"📝 «{text}»\n\n{cleaned or response}")
    await ConversationsRepo.append(user_id, "assistant", response)

    for title in task_titles:
        try:
            t = await TasksRepo.create(TaskItem(user_id=user_id, title=title))
            await message.answer(f"📌 Записал задачу #{t.id}: {title}")
        except Exception as e:
            log.warning("task.create_failed", error=str(e))

    asyncio.create_task(extract_and_store(user_id, text, source_message_id=user_msg_id))


def _extract_tasks(text: str) -> tuple[str, list[str]]:
    titles = [m.group(1).strip() for m in TASK_PATTERN.finditer(text)]
    cleaned = TASK_PATTERN.sub("", text).strip()
    return re.sub(r"\n{3,}", "\n\n", cleaned), titles
