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
    "date": """You've been given a task: plan a concrete date for today / the next few days.

WHAT TO DO:
1. Get the location via get_user_location (if there's none — ask for the city or coordinates).
2. Check the weather via get_weather — it affects the plan.
3. Optionally: wiki_geosearch for one interesting landmark.
4. Lay out the plan: 2-3 spots/activities with timing, a route that makes sense.
5. Rely on your knowledge of the city and place categories — do NOT invent specific addresses or names.
6. If specifics on an address/schedule are needed — honestly say "check Google Maps for X".
7. Length: 8-12 lines.

If the user gave context (a partner, budget, area, what they're after) — factor it in. If not — ask one key thing.""",

    "day": """Task: plan the user's day for today.

WHAT TO DO:
1. Take the active tasks from the system context.
2. Take habits and today's logs.
3. Check the weather via get_weather (call get_user_location if needed).
4. Lay the day out in blocks: morning, afternoon, evening. Tie it to the user's energy windows.
5. Between blocks, add micro-breaks (a stretch, a walk, breathing).
6. If the weather is bad — don't suggest outdoor things.
7. Length: 12-15 lines, concrete.""",

    "activity": """Task: suggest the user one activity right now, suited to their current state.

WHAT TO DO:
1. Read the latest diary entry and recent messages from the system context — gauge their state.
2. If relevant — get_weather (e.g. to decide "outdoors or home").
3. Give ONE concrete activity with a reason why this one. Not three options.
4. If their state is poor — keep it gentle (a walk, a sauna, a quiet cafe). If they're OK — it can be more energetic.
5. Don't invent specific places/addresses — give a category and direction, or say "search for X on maps".
6. Length: 4-7 lines."""
}


async def _run_planner(message: Message, mode: str) -> None:
    user_id = message.from_user.id  # type: ignore[union-attr]
    parts = (message.text or "").split(maxsplit=1)
    extra = parts[1].strip() if len(parts) > 1 else ""

    seed_user_msg = {
        "date": f"Plan a date. Details: {extra}" if extra else "Plan a date for me.",
        "day": f"Lay out my day. Context: {extra}" if extra else "Lay out my day.",
        "activity": f"Suggest an activity right now. Context: {extra}" if extra else "Suggest me one activity right now for my current state.",
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
        await message.answer("Couldn't plan that right now. Try again in a minute.")
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
