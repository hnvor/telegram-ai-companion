"""Команды и callback'и для GTD-задач."""

from datetime import datetime, timedelta, timezone

import structlog
from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from src.bot.keyboards import task_actions_kb
from src.db.repo import TasksRepo
from src.domain.models import TaskItem

router = Router()
log = structlog.get_logger()


@router.message(Command("task"))
async def on_task_create(message: Message) -> None:
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Используй: /task купить молоко завтра")
        return
    title = parts[1].strip()
    user_id = message.from_user.id  # type: ignore[union-attr]
    task = await TasksRepo.create(TaskItem(user_id=user_id, title=title))
    await message.answer(
        f"📌 #{task.id} {task.title}",
        reply_markup=task_actions_kb(task.id) if task.id else None,
    )


@router.message(Command("tasks"))
async def on_tasks_list(message: Message) -> None:
    user_id = message.from_user.id  # type: ignore[union-attr]
    tasks = await TasksRepo.list_active(user_id, limit=30)
    if not tasks:
        await message.answer("Открытых задач нет 🎉")
        return
    lines = ["Активные задачи:"]
    for t in tasks:
        proj = f" [{t.project}]" if t.project else ""
        due = f" → {t.due_at:%d.%m %H:%M}" if t.due_at else ""
        marker = "▶" if t.status == "doing" else "○"
        lines.append(f"{marker} #{t.id} {t.title}{proj}{due}")
    await message.answer("\n".join(lines))


@router.message(Command("done"))
async def on_task_done(message: Message) -> None:
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip().isdigit():
        await message.answer("Используй: /done 42")
        return
    task_id = int(parts[1])
    task = await TasksRepo.get(task_id)
    if task is None or task.user_id != message.from_user.id:  # type: ignore[union-attr]
        await message.answer("Не нашёл такую задачу.")
        return
    await TasksRepo.mark_done(task_id)
    await message.answer(f"✅ #{task_id} {task.title}")


@router.callback_query(F.data.startswith("task:"))
async def on_task_callback(cb: CallbackQuery) -> None:
    parts = (cb.data or "").split(":")
    if len(parts) < 3:
        await cb.answer()
        return
    action = parts[1]

    if action == "done":
        task_id = int(parts[2])
        await TasksRepo.mark_done(task_id)
        await cb.message.edit_text(f"✅ Закрыто #{task_id}")  # type: ignore[union-attr]
        await cb.answer("Готово")
        return

    if action == "drop":
        task_id = int(parts[2])
        await TasksRepo.update_status(task_id, "dropped")
        await cb.message.edit_text(f"❌ Дроп #{task_id}")  # type: ignore[union-attr]
        await cb.answer()
        return

    if action == "postpone":
        # task:postpone:1h:42  /  task:postpone:1d:42
        if len(parts) < 4:
            await cb.answer()
            return
        delta_str, task_id_str = parts[2], parts[3]
        task_id = int(task_id_str)
        if delta_str == "1h":
            new_remind = datetime.now(timezone.utc) + timedelta(hours=1)
        elif delta_str == "1d":
            new_remind = datetime.now(timezone.utc) + timedelta(days=1)
        else:
            new_remind = datetime.now(timezone.utc) + timedelta(hours=2)
        await TasksRepo.postpone(task_id, new_remind)
        await cb.message.edit_text(  # type: ignore[union-attr]
            f"⏰ #{task_id} перенесена на {new_remind:%d.%m %H:%M UTC}"
        )
        await cb.answer()
        return

    await cb.answer()
