"""Детектор бытовых рутин в репликах пользователя + хелпер для блока в system prompt.

Запускается фоном в chat handler. Если пользователь упомянул что-то сделал
(«помылся», «побрился», «гулял»), бот тихо обновляет last_done_at — не отвечает.
В дневном брифе и системном промпте бот показывает список просроченных.
"""
import json

import structlog

from src.core.llm import chat_json
from src.core.prompts import ROUTINE_DETECT_PROMPT
from src.db.repo import RoutinesRepo

log = structlog.get_logger()


async def detect_and_log_routines(user_id: int, user_text: str) -> list[str]:
    if not user_text or len(user_text.strip()) < 4:
        return []

    routines = await RoutinesRepo.list_active(user_id)
    if not routines:
        return []

    listing = "\n".join(f"- name={r['name']}: {r['label']}" for r in routines)
    prompt = (
        f"Сообщение пользователя:\n{user_text}\n\nАктивные рутины:\n{listing}\n\n"
        "Верни JSON-массив name закрытых сегодня."
    )
    try:
        raw = await chat_json(
            user_id=user_id,
            system=ROUTINE_DETECT_PROMPT,
            user_message=prompt,
            purpose="routine_detect",
            max_tokens=200,
        )
    except Exception as e:
        log.warning("routine.llm_failed", error=str(e))
        return []

    names = _parse_list(raw)
    if not names:
        return []
    valid_names = {r["name"] for r in routines}
    closed: list[str] = []
    for n in names:
        if n in valid_names:
            ok = await RoutinesRepo.mark_done(user_id, n)
            if ok:
                closed.append(n)
    if closed:
        log.info("routines.marked_done", user_id=user_id, names=closed)
    return closed


def format_routines_block(overdue: list[dict]) -> str:
    """Блок «банальных вещей» для system prompt. Если ничего не просрочено — пусто."""
    if not overdue:
        return ""
    lines = ["## БАНАЛЬНЫЕ ВЕЩИ (просрочены)"]
    for r in overdue:
        days = r.get("days_since")
        if days is None:
            tail = "ни разу не отмечал"
        else:
            tail = f"{int(round(days))}д назад"
        lines.append(f"- {r['label']} ({tail})")
    lines.append("При уместном случае мягко напомни (одно из них, не списком). "
                 "Если пользователь упомянет что сделал — бот сам отметит, тебе ничего делать не надо.")
    return "\n".join(lines)


def _parse_list(raw: str) -> list[str]:
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
    return [str(x) for x in data if isinstance(x, str)]
