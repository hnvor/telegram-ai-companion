"""Challenge engine: ad-hoc предложение конкретной вещи попробовать.

Запускается между weekly_restart'ами (среда, суббота и т.п.) — даёт пользователю
ОДИН конкретный челлендж на 2-4 дня.
"""
import json

import structlog

from src.core.llm import chat as llm_chat
from src.core.prompts import CHALLENGE_PROMPT
from src.db.repo import ExperimentsRepo, LifeStateRepo

log = structlog.get_logger()


async def generate_challenge(user_id: int) -> dict | None:
    state = await LifeStateRepo.get(user_id) or {}
    if not state:
        return None

    experiments = await ExperimentsRepo.recent(user_id, days=60, limit=30)

    from datetime import datetime
    today_label = datetime.utcnow().strftime("%A")
    user_message = (
        f"Today: {today_label}\n\n"
        "## LIFE PORTRAIT\n"
        + json.dumps(state, ensure_ascii=False, indent=2)
        + "\n\n## EXPERIMENT HISTORY\n"
        + json.dumps(experiments, ensure_ascii=False, indent=2, default=str)[:4000]
        + "\n\n## TASK\nReturn JSON for one challenge."
    )

    try:
        raw = await llm_chat(
            user_id=user_id,
            system_static=CHALLENGE_PROMPT,
            system_dynamic="",
            messages=[{"role": "user", "content": user_message}],
            purpose="challenge",
            max_tokens=600,
            temperature=0.85,
        )
    except Exception as e:
        log.warning("challenge.llm_failed", error=str(e))
        return None

    text = raw.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text
        if text.endswith("```"):
            text = text.rsplit("```", 1)[0]
        text = text.strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        log.warning("challenge.invalid_json", raw=raw[:300])
        return None
    if not isinstance(data, dict) or "what" not in data:
        return None
    return data


def format_challenge_message(challenge: dict) -> str:
    lines = ["🎯 Challenge", "", challenge.get("what", "?")]
    if challenge.get("description"):
        lines.append("")
        lines.append(challenge["description"])
    if challenge.get("why"):
        lines.append("")
        lines.append(challenge["why"])
    return "\n".join(lines)
