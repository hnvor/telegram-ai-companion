from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove


def remove_kb() -> ReplyKeyboardRemove:
    return ReplyKeyboardRemove()


def mood_keyboard(prefix: str = "mood") -> InlineKeyboardMarkup:
    """Шкала 1-10 для mood/energy."""
    row1 = [InlineKeyboardButton(text=str(i), callback_data=f"{prefix}:{i}") for i in range(1, 6)]
    row2 = [InlineKeyboardButton(text=str(i), callback_data=f"{prefix}:{i}") for i in range(6, 11)]
    return InlineKeyboardMarkup(inline_keyboard=[row1, row2])


def task_actions_kb(task_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Сделал", callback_data=f"task:done:{task_id}"),
                InlineKeyboardButton(text="⏰ +1 час", callback_data=f"task:postpone:1h:{task_id}"),
                InlineKeyboardButton(text="📅 Завтра", callback_data=f"task:postpone:1d:{task_id}"),
            ],
            [InlineKeyboardButton(text="❌ Дроп", callback_data=f"task:drop:{task_id}")],
        ]
    )


def experiment_kb(experiment_id: int) -> InlineKeyboardMarkup:
    """Кнопки на челлендж/эксперимент в момент предложения."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🟢 Беру", callback_data=f"exp:accept:{experiment_id}"),
                InlineKeyboardButton(text="⏸ Не сейчас", callback_data=f"exp:later:{experiment_id}"),
                InlineKeyboardButton(text="✋ Не подходит", callback_data=f"exp:reject:{experiment_id}"),
            ]
        ]
    )


def experiment_done_kb(experiment_id: int) -> InlineKeyboardMarkup:
    """Кнопки на закрытие принятого эксперимента."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Сделал", callback_data=f"exp:done:{experiment_id}"),
                InlineKeyboardButton(text="❌ Не вышло", callback_data=f"exp:fail:{experiment_id}"),
            ]
        ]
    )


def confirm_kb(yes: str = "Да", no: str = "Нет", prefix: str = "confirm") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=yes, callback_data=f"{prefix}:yes"),
                InlineKeyboardButton(text=no, callback_data=f"{prefix}:no"),
            ]
        ]
    )
