"""Структурный портрет жизни пользователя.

Один JSON документ в `life_state`, обновляемый фоновой Haiku-задачей раз в день
(или по требованию). В отличие от `facts` (плоский список) и `conversations`
(сырая история), это плотное резюме «кто этот человек сейчас и куда движется».
Подмешивается в каждый системный промпт.
"""
import json
from datetime import datetime, timedelta, timezone

import structlog

from src.core.llm import chat_json
from src.core.prompts import LIFE_STATE_UPDATE_PROMPT
from src.db.client import get_pool
from src.db.repo import LifeStateRepo

log = structlog.get_logger()


DEFAULT_STATE: dict = {
    "core": None,
    "direction": None,
    "health": {
        "mental": None,
        "medication": None,
        "somatic": None,
        "sleep": None,
        "physical": None,
    },
    "projects": [],
    "relationships": [],
    "experiments": [],
    "patterns": [],
    "knowns": [],
    "open_questions": [],
}


async def _recent_messages_text(user_id: int, since: datetime, limit: int = 200) -> str:
    pool = await get_pool()
    rows = await pool.fetch(
        """
        SELECT role, content, created_at FROM conversations
        WHERE user_id = $1 AND created_at > $2
        ORDER BY created_at ASC
        LIMIT $3
        """,
        user_id, since, limit,
    )
    if not rows:
        return ""
    lines = []
    for r in rows:
        ts = r["created_at"].strftime("%Y-%m-%d %H:%M")
        role = "U" if r["role"] == "user" else "A"
        text = (r["content"] or "").replace("\n", " ").strip()
        if len(text) > 800:
            text = text[:800] + "…"
        lines.append(f"[{ts} {role}] {text}")
    return "\n".join(lines)


async def update_life_state(user_id: int, since_hours: int = 30, max_tokens: int = 3500) -> bool:
    """Перестраивает state на основе последних `since_hours` часов разговоров.
    Возвращает True если state был изменён.
    """
    current = await LifeStateRepo.get(user_id) or {}
    if not current:
        current = DEFAULT_STATE

    since = datetime.now(timezone.utc) - timedelta(hours=since_hours)
    convo_text = await _recent_messages_text(user_id, since)
    if not convo_text:
        log.info("life_state.skip_no_messages", user_id=user_id)
        return False

    user_message = (
        "## ТЕКУЩИЙ STATE\n"
        + json.dumps(current, ensure_ascii=False, indent=2)
        + "\n\n## НОВЫЕ СООБЩЕНИЯ\n"
        + convo_text
        + "\n\n## ЗАДАЧА\nВерни обновлённый JSON state."
    )

    try:
        raw = await chat_json(
            user_id=user_id,
            system=LIFE_STATE_UPDATE_PROMPT,
            user_message=user_message,
            purpose="life_state_update",
            max_tokens=max_tokens,
        )
    except Exception as e:
        log.warning("life_state.llm_failed", error=str(e))
        return False

    new_state = _parse_state(raw)
    if new_state is None:
        return False

    if json.dumps(new_state, sort_keys=True, ensure_ascii=False) == json.dumps(
        current, sort_keys=True, ensure_ascii=False
    ):
        return False

    await LifeStateRepo.upsert(user_id, new_state)
    log.info("life_state.updated", user_id=user_id, keys=list(new_state.keys()))
    return True


def _parse_state(raw: str) -> dict | None:
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text
        if text.endswith("```"):
            text = text.rsplit("```", 1)[0]
        text = text.strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        log.warning("life_state.invalid_json", raw=raw[:300])
        return None
    if not isinstance(data, dict):
        return None
    return data


def format_life_state_block(state: dict) -> str:
    """Превращает state в компактный текстовый блок для system prompt."""
    if not state:
        return ""
    lines = ["## ПОРТРЕТ ЖИЗНИ ПОЛЬЗОВАТЕЛЯ (живой, обновляется ежедневно)"]
    if state.get("core"):
        lines.append(f"- Суть: {state['core']}")
    if state.get("direction"):
        lines.append(f"- Направление: {state['direction']}")

    health = state.get("health") or {}
    health_lines = []
    for k, label in [
        ("mental", "психика"),
        ("medication", "препараты"),
        ("somatic", "тело"),
        ("sleep", "сон"),
        ("physical", "физика"),
    ]:
        v = health.get(k)
        if v:
            health_lines.append(f"  - {label}: {v}")
    if health_lines:
        lines.append("- Здоровье:")
        lines.extend(health_lines)

    if state.get("projects"):
        lines.append("- Проекты:")
        for p in state["projects"][:6]:
            if isinstance(p, dict):
                name = p.get("name", "?")
                st = p.get("status", "")
                latest = p.get("latest", "")
                lines.append(f"  - {name} [{st}] {latest}".rstrip())
    if state.get("relationships"):
        lines.append("- Близкие:")
        for r in state["relationships"][:5]:
            if isinstance(r, dict):
                lines.append(f"  - {r.get('name', '?')} ({r.get('role', '?')}): {r.get('latest', '')}".rstrip())
    if state.get("experiments"):
        lines.append("- Эксперименты:")
        for e in state["experiments"][:8]:
            if isinstance(e, dict):
                lines.append(f"  - {e.get('what', '?')} ({e.get('when', '?')}): {e.get('result', '')}".rstrip())
    if state.get("patterns"):
        lines.append("- Паттерны:")
        for pt in state["patterns"][:8]:
            if isinstance(pt, dict):
                lines.append(f"  - {pt.get('what', '?')} → {pt.get('impact', '')}".rstrip())
    if state.get("knowns"):
        lines.append("- Известно: " + "; ".join(str(x) for x in state["knowns"][:10]))
    if state.get("open_questions"):
        lines.append("- Открытые вопросы: " + "; ".join(str(x) for x in state["open_questions"][:5]))
    return "\n".join(lines)
