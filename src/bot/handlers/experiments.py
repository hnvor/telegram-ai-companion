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
        await cb.answer("Broken id", show_alert=False)
        return

    exp = await ExperimentsRepo.get(exp_id)
    if exp is None:
        await cb.answer("Experiment not found", show_alert=False)
        return

    msg = cb.message
    if action == "accept":
        await ExperimentsRepo.set_accepted(exp_id, True)
        await cb.answer("I'm in 👍")
        if msg:
            try:
                await msg.edit_reply_markup(reply_markup=experiment_done_kb(exp_id))
            except Exception:
                pass
            try:
                await msg.answer(
                    f"Okay, keeping it in mind: \"{exp['title'][:120]}\". "
                    "When you've done it — hit ✅ below and we'll mark it closed."
                )
            except Exception:
                pass
    elif action == "later":
        await ExperimentsRepo.set_accepted(exp_id, False)
        await cb.answer("Okay, another time")
        if msg:
            try:
                await msg.edit_reply_markup(reply_markup=None)
            except Exception:
                pass
    elif action == "reject":
        await ExperimentsRepo.set_accepted(exp_id, False)
        await cb.answer("Got it, not for you")
        if msg:
            try:
                await msg.edit_reply_markup(reply_markup=None)
            except Exception:
                pass
    elif action == "done":
        await ExperimentsRepo.set_completed(exp_id, True)
        await cb.answer("🔥 Counted!")
        if msg:
            try:
                await msg.edit_reply_markup(reply_markup=None)
            except Exception:
                pass
            try:
                await msg.answer(f"Marked \"{exp['title'][:120]}\" as done. Tell me how it went, if you have the energy.")
            except Exception:
                pass
    elif action == "fail":
        await ExperimentsRepo.set_completed(exp_id, False)
        await cb.answer("It happens, no big deal")
        if msg:
            try:
                await msg.edit_reply_markup(reply_markup=None)
            except Exception:
                pass
            try:
                await msg.answer(f"Okay, \"{exp['title'][:80]}\" didn't land. What got in the way?")
            except Exception:
                pass
    else:
        await cb.answer()
