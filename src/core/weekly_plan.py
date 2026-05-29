"""Воскресный restart недели + понедельничный фокус.

Раз в неделю в воскресенье вечером Sonnet синтезирует план следующей
недели на основе life_state, прошлой недели и истории экспериментов.
"""
import json
from datetime import date, datetime, timedelta, timezone

import structlog

from src.core.llm import chat as llm_chat
from src.core.prompts import WEEKLY_PLAN_PROMPT
from src.db.repo import (
    ExperimentsRepo,
    LifeStateRepo,
    WeeklyPlanRepo,
    ConversationsRepo,
)

log = structlog.get_logger()


def _week_start(d: date) -> date:
    """Понедельник недели, в которой лежит дата `d`."""
    return d - timedelta(days=d.weekday())


async def _last_week_summary(user_id: int, week_end_utc: datetime) -> str:
    """Сжатая сводка последних 7 суток для контекста."""
    from src.db.client import get_pool

    pool = await get_pool()
    since = week_end_utc - timedelta(days=7)
    rows = await pool.fetch(
        """
        SELECT role, content, created_at FROM conversations
        WHERE user_id=$1 AND created_at > $2 AND created_at <= $3
        ORDER BY created_at ASC LIMIT 250
        """,
        user_id, since, week_end_utc,
    )
    if not rows:
        return "Сообщений нет."
    lines = []
    for r in rows:
        ts = r["created_at"].strftime("%m-%d %H:%M")
        role = "U" if r["role"] == "user" else "A"
        text = (r["content"] or "").replace("\n", " ").strip()
        if len(text) > 400:
            text = text[:400] + "…"
        lines.append(f"[{ts} {role}] {text}")
    return "\n".join(lines)


async def generate_weekly_plan(user_id: int, for_week_start: date) -> dict | None:
    state = await LifeStateRepo.get(user_id) or {}
    if not state:
        log.warning("weekly_plan.no_life_state")
        return None

    week_end_utc = datetime.combine(for_week_start, datetime.min.time(), tzinfo=timezone.utc)
    last_week = await _last_week_summary(user_id, week_end_utc)
    prev_plan = await WeeklyPlanRepo.previous(user_id, before_week=for_week_start)
    experiments = await ExperimentsRepo.recent(user_id, days=60, limit=30)

    user_message = (
        "## ПОРТРЕТ ЖИЗНИ\n"
        + json.dumps(state, ensure_ascii=False, indent=2)
        + "\n\n## ПРОШЛАЯ НЕДЕЛЯ (план + сообщения)\n"
        + (json.dumps(prev_plan, ensure_ascii=False, indent=2, default=str) if prev_plan else "Плана не было.")
        + "\n\n--- сообщения ---\n"
        + last_week
        + "\n\n## ИСТОРИЯ ЭКСПЕРИМЕНТОВ (что зашло, что нет)\n"
        + json.dumps(experiments, ensure_ascii=False, indent=2, default=str)[:4000]
        + "\n\n## ЗАДАЧА\nВерни JSON плана на следующую неделю."
    )

    try:
        raw = await llm_chat(
            user_id=user_id,
            system_static=WEEKLY_PLAN_PROMPT,
            system_dynamic="",
            messages=[{"role": "user", "content": user_message}],
            purpose="weekly_plan",
            max_tokens=2000,
            temperature=0.6,
        )
    except Exception as e:
        log.warning("weekly_plan.llm_failed", error=str(e))
        return None

    plan = _parse_json(raw)
    if plan is None or "focuses" not in plan:
        return None

    await WeeklyPlanRepo.upsert(
        user_id, for_week_start,
        focuses=plan.get("focuses", []),
        experiment=plan.get("experiment"),
        challenge=plan.get("challenge"),
    )

    # Эксперимент и челлендж параллельно логируем в experiments_log,
    # чтобы потом можно было считать «что зашло».
    if plan.get("experiment"):
        e = plan["experiment"]
        await ExperimentsRepo.create(
            user_id,
            title=str(e.get("what", ""))[:200],
            description=" / ".join(filter(None, [e.get("why"), e.get("how")]))[:1000],
            source="weekly",
        )
    if plan.get("challenge"):
        c = plan["challenge"]
        await ExperimentsRepo.create(
            user_id,
            title=str(c.get("what", ""))[:200],
            description=str(c.get("why", ""))[:1000],
            source="weekly",
        )

    return plan


def format_plan_message(plan: dict) -> str:
    lines = ["📅 План недели", ""]
    for i, f in enumerate(plan.get("focuses", []), start=1):
        if isinstance(f, dict):
            lines.append(f"{i}. {f.get('title', '?')}")
            if f.get("why"):
                lines.append(f"   — {f['why']}")
    if plan.get("experiment"):
        e = plan["experiment"]
        lines.append("")
        lines.append("🧪 Эксперимент недели")
        lines.append(f"{e.get('what', '?')}")
        if e.get("how"):
            lines.append(f"как: {e['how']}")
        if e.get("why"):
            lines.append(f"зачем: {e['why']}")
    if plan.get("challenge"):
        c = plan["challenge"]
        lines.append("")
        lines.append("🎯 Челлендж")
        lines.append(f"{c.get('what', '?')}")
        if c.get("why"):
            lines.append(c["why"])
    return "\n".join(lines)


def _parse_json(raw: str) -> dict | None:
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text
        if text.endswith("```"):
            text = text.rsplit("```", 1)[0]
        text = text.strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        log.warning("weekly_plan.invalid_json", raw=raw[:300])
        return None
    return data if isinstance(data, dict) else None
