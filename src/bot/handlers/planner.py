"""Команды для проактивного планирования: свидание, день, активность под состояние."""

import structlog
from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from src.core import memory
from src.core.llm import chat_with_tools
from src.core.prompts import SYSTEM_BASE
from src.core.tools import TOOLS_SPEC
from src.db.repo import ConversationsRepo

router = Router()
log = structlog.get_logger()


PLANNER_INSTRUCTIONS = {
    "date": """Тебе дали задачу: спланировать конкретный план свидания на сегодня/ближайшие дни.

ЧТО ДЕЛАТЬ:
1. Узнай локацию через get_user_location (если её нет — попроси город или геопозицию).
2. Глянь погоду через get_weather — это влияет на сценарий.
3. Опционально: wiki_geosearch для одной интересной достопримечательности.
4. Сформулируй сценарий: 2-3 точки/активности с привязкой ко времени, маршрут по смыслу.
5. Опирайся на свои знания о городе и категориях мест — конкретные адреса и названия НЕ выдумывай.
6. Если нужна конкретика по адресу/расписанию — честно скажи «глянь в Google Maps по запросу X».
7. Длина: 8-12 строк.

Если пользователь дал контекст (девушка, бюджет, район, чего хочется) — учти. Если нет — спроси одну ключевую вещь.""",

    "day": """Задача: спланировать день пользователя на сегодня.

ЧТО ДЕЛАТЬ:
1. Возьми активные задачи из system context.
2. Возьми привычки и сегодняшние логи.
3. Проверь погоду через get_weather (вызови get_user_location если нужно).
4. Распиши день блоками: утро, день, вечер. Привязка к энергетическим окнам пользователя.
5. Между блоками дай микро-разгрузки (разминка, прогулка, дыхание).
6. Если погода плохая — не предлагай уличное.
7. Длина: 12-15 строк, конкретно.""",

    "activity": """Задача: предложить пользователю одну активность сейчас под его текущее состояние.

ЧТО ДЕЛАТЬ:
1. Прочитай последний дневник и недавние сообщения из system context — оцени состояние.
2. Если уместно — get_weather (например для решения «улица или дом»).
3. Дай ОДНУ конкретную активность с обоснованием почему именно она. Не три варианта.
4. Если состояние плохое — мягкое (прогулка, парная, тихое кафе). Если ОК — можно энергичнее.
5. Конкретные места/адреса не выдумывай — давай категорию и направление, или скажи «глянь по запросу X в картах».
6. Длина: 4-7 строк."""
}


async def _run_planner(message: Message, mode: str) -> None:
    user_id = message.from_user.id  # type: ignore[union-attr]
    parts = (message.text or "").split(maxsplit=1)
    extra = parts[1].strip() if len(parts) > 1 else ""

    seed_user_msg = {
        "date": f"Спланируй свидание. Детали: {extra}" if extra else "Спланируй мне свидание.",
        "day": f"Распиши мне день. Контекст: {extra}" if extra else "Распиши мне день.",
        "activity": f"Предложи активность сейчас. Контекст: {extra}" if extra else "Предложи мне сейчас одну активность под моё состояние.",
    }[mode]

    bundle = await memory.build_context(user_id, seed_user_msg)
    dynamic = memory.format_dynamic_system(bundle)

    await message.bot.send_chat_action(message.chat.id, "typing")
    try:
        text = await chat_with_tools(
            user_id=user_id,
            system_static=SYSTEM_BASE + "\n\n" + PLANNER_INSTRUCTIONS[mode],
            system_dynamic=dynamic,
            messages=[{"role": "user", "content": seed_user_msg}],
            tools=TOOLS_SPEC,
            purpose=f"planner_{mode}",
            temperature=0.65,
            max_tokens=1500,
            max_iterations=6,
        )
    except Exception as e:
        log.exception("planner.failed", mode=mode, error=str(e))
        await message.answer("Не получилось спланировать сейчас. Попробуй ещё раз через минуту.")
        return

    await message.answer(text)
    await ConversationsRepo.append(user_id, "user", seed_user_msg, {"command": f"plan_{mode}"})
    await ConversationsRepo.append(user_id, "assistant", text, {"plan": mode})


@router.message(Command("plan_date"))
async def plan_date(message: Message) -> None:
    await _run_planner(message, "date")


@router.message(Command("plan_day"))
async def plan_day(message: Message) -> None:
    await _run_planner(message, "day")


@router.message(Command("activity"))
async def activity(message: Message) -> None:
    await _run_planner(message, "activity")
