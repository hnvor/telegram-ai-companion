"""Авто-детект закрытых задач в реплике пользователя.

Запускается фоном после каждого сообщения. Если Haiku решит, что пользователь
упомянул закрытие активной задачи — бот пришлёт inline-предложение закрыть её
кнопкой ✅. Никаких автоматических done без подтверждения — false positive
у LLM штатно случаются.
"""
import json

import structlog
from aiogram import Bot

from src.bot.keyboards import task_actions_kb
from src.core.llm import chat_json
from src.core.prompts import TASK_CLOSURE_PROMPT
from src.db.repo import TasksRepo

log = structlog.get_logger()


async def auto_close_from_message(bot: Bot, user_id: int, user_text: str) -> None:
    if not user_text or len(user_text.strip()) < 5:
        return

    tasks = await TasksRepo.list_active(user_id, limit=20)
    if not tasks:
        return

    task_list = "\n".join(f"- id={t.id}: {t.title}" for t in tasks if t.id is not None)
    prompt = (
        f"User message:\n{user_text}\n\nOpen tasks:\n{task_list}\n\n"
        "Return a JSON array of the closed ids."
    )
    try:
        raw = await chat_json(
            user_id=user_id,
            system=TASK_CLOSURE_PROMPT,
            user_message=prompt,
            purpose="task_closure",
        )
    except Exception as e:
        log.warning("task_closure.llm_failed", error=str(e))
        return

    ids = _parse_ids(raw)
    if not ids:
        return

    valid = {t.id: t for t in tasks if t.id in ids}
    for tid, t in valid.items():
        try:
            await bot.send_message(
                chat_id=user_id,
                text=f"Looks like you closed #{tid}: {t.title}. Confirm?",
                reply_markup=task_actions_kb(tid),
            )
        except Exception as e:
            log.warning("task_closure.send_failed", error=str(e), task_id=tid)


def _parse_ids(raw: str) -> list[int]:
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text
        if text.endswith("```"):
            text = text.rsplit("```", 1)[0]
        text = text.strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    out: list[int] = []
    for x in data:
        try:
            out.append(int(x))
        except (TypeError, ValueError):
            continue
    return out
