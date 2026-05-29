"""Callback-handlers для exp:* — приём/отказ/завершение челленджей и экспериментов."""
import structlog
from aiogram import F, Router
from aiogram.types import CallbackQuery

from src.bot.keyboards import experiment_done_kb
from src.db.repo import ExperimentsRepo

router = Router()
log = structlog.get_logger()


@router.callback_query(F.data.startswith("exp:"))
async def on_experiment_cb(cb: CallbackQuery) -> None:
    if not cb.data:
        return
    parts = cb.data.split(":")
    if len(parts) < 3:
        return
    _, action, raw_id = parts[0], parts[1], parts[2]
    try:
        exp_id = int(raw_id)
    except ValueError:
        await cb.answer("Битый id", show_alert=False)
        return

    exp = await ExperimentsRepo.get(exp_id)
    if exp is None:
        await cb.answer("Эксперимент не найден", show_alert=False)
        return

    msg = cb.message
    if action == "accept":
        await ExperimentsRepo.set_accepted(exp_id, True)
        await cb.answer("Беру 👍")
        if msg:
            try:
                await msg.edit_reply_markup(reply_markup=experiment_done_kb(exp_id))
            except Exception:
                pass
            try:
                await msg.answer(
                    f"Окей, держу в виду: «{exp['title'][:120]}». "
                    "Как сделаешь — нажми ✅ ниже, отметим как закрытое."
                )
            except Exception:
                pass
    elif action == "later":
        await ExperimentsRepo.set_accepted(exp_id, False)
        await cb.answer("Окей, в другой раз")
        if msg:
            try:
                await msg.edit_reply_markup(reply_markup=None)
            except Exception:
                pass
    elif action == "reject":
        await ExperimentsRepo.set_accepted(exp_id, False)
        await cb.answer("Понял, не подходит")
        if msg:
            try:
                await msg.edit_reply_markup(reply_markup=None)
            except Exception:
                pass
    elif action == "done":
        await ExperimentsRepo.set_completed(exp_id, True)
        await cb.answer("🔥 Засчитано!")
        if msg:
            try:
                await msg.edit_reply_markup(reply_markup=None)
            except Exception:
                pass
            try:
                await msg.answer(f"Отметил «{exp['title'][:120]}» как сделанное. Расскажи как зашло, если есть силы.")
            except Exception:
                pass
    elif action == "fail":
        await ExperimentsRepo.set_completed(exp_id, False)
        await cb.answer("Бывает, ничего страшного")
        if msg:
            try:
                await msg.edit_reply_markup(reply_markup=None)
            except Exception:
                pass
            try:
                await msg.answer(f"Окей, «{exp['title'][:80]}» не зашло. Что помешало?")
            except Exception:
                pass
    else:
        await cb.answer()
