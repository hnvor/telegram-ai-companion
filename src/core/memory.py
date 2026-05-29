"""Сборка контекста для каждого ответа агента.

Делает 4-5 параллельных запросов к БД + один эмбеддинг текущего сообщения,
форматирует всё в system prompt + список messages для LLM.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import structlog

from datetime import date as _date

from src.core.embeddings import embed_one
from src.core.life_state import format_life_state_block
from src.core.routines import format_routines_block
from src.db.repo import (
    ConversationsRepo,
    DiaryRepo,
    FactsRepo,
    HabitsRepo,
    LifeStateRepo,
    ProfileRepo,
    RoutinesRepo,
    TasksRepo,
    WeeklyPlanRepo,
)
from src.domain.models import (
    ContextBundle,
    ConversationMsg,
    Profile,
)

log = structlog.get_logger()


async def build_context(user_id: int, current_message: str) -> ContextBundle:
    """Собирает всё, что агенту нужно знать прямо сейчас."""

    # Эмбеддинг считается параллельно с DB-запросами
    embedding_task = asyncio.create_task(embed_one(current_message))

    profile, active_tasks, recent_msgs, life_state = await asyncio.gather(
        ProfileRepo.get(user_id),
        TasksRepo.list_active(user_id, limit=10),
        ConversationsRepo.recent(user_id, limit=15),
        LifeStateRepo.get(user_id),
    )

    if profile is None:
        profile = Profile(user_id=user_id)

    embedding = await embedding_task

    candidates_task = FactsRepo.search_candidates(user_id, embedding, limit=30)
    diary_task = DiaryRepo.search_similar(user_id, embedding, limit=3)
    candidates, relevant_diary = await asyncio.gather(candidates_task, diary_task)
    relevant_facts = _rerank_facts(candidates, target=7)

    today_snapshot = await _today_snapshot(user_id, profile)
    today_snapshot["life_state"] = life_state

    # План текущей недели (если существует) — для подмешивания в system prompt
    try:
        local_today = datetime.now(ZoneInfo(profile.timezone or "Asia/Bangkok")).date()
        week_start = local_today - timedelta(days=local_today.weekday())
        wp = await WeeklyPlanRepo.get(user_id, week_start)
        today_snapshot["weekly_plan"] = wp
    except Exception:
        today_snapshot["weekly_plan"] = None

    # Просроченные рутины — для блока «банальные вещи»
    try:
        today_snapshot["overdue_routines"] = await RoutinesRepo.overdue(user_id)
    except Exception:
        today_snapshot["overdue_routines"] = []

    # Update last_referenced_at в фоне
    fact_ids = [f.id for f in relevant_facts if f.id]
    if fact_ids:
        asyncio.create_task(FactsRepo.mark_referenced(fact_ids))

    return ContextBundle(
        profile=profile,
        active_tasks=active_tasks,
        recent_conversation=recent_msgs,
        relevant_facts=relevant_facts,
        relevant_diary=relevant_diary,
        today_snapshot=today_snapshot,
    )


async def _today_snapshot(user_id: int, profile: Profile) -> dict:
    """Что произошло сегодня: habits done, mood последнего чекина."""
    try:
        tz = ZoneInfo(profile.timezone or "Asia/Bangkok")
    except Exception:
        tz = ZoneInfo("UTC")

    now_local = datetime.now(tz)
    day_start = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = day_start + timedelta(days=1)

    habits = await HabitsRepo.list_active(user_id)
    habits_today = []
    for h in habits:
        if h.id is None:
            continue
        n = await HabitsRepo.count_today(h.id, day_start, day_end)
        habits_today.append({"name": h.name, "done": n, "target": h.target})

    last_diary = await DiaryRepo.get_by_date(user_id, day_start.date() - timedelta(days=1))

    return {
        "local_time": now_local.strftime("%Y-%m-%d %H:%M %Z"),
        "habits_today": habits_today,
        "yesterday_mood": last_diary.mood if last_diary else None,
        "yesterday_energy": last_diary.energy if last_diary else None,
    }


def format_dynamic_system(bundle: ContextBundle) -> str:
    """Превращает ContextBundle в текстовый блок для system prompt."""
    p = bundle.profile
    tone = p.preferences.get("tone", {}) if isinstance(p.preferences, dict) else {}
    tone_desc = (
        f"warmth={tone.get('warmth', 0.7):.1f}, "
        f"directness={tone.get('directness', 0.5):.1f}, "
        f"humor={tone.get('humor', 0.6):.1f}, "
        f"push_intensity={tone.get('push_intensity', 0.5):.1f}"
    )

    parts: list[str] = []

    # === Life state (живой портрет жизни) — на самом верху ===
    life_state = bundle.today_snapshot.get("life_state") or {}
    if life_state:
        parts.append(format_life_state_block(life_state))
        parts.append("")

    # === Routines overdue (банальные вещи: душ, бритьё, ногти и т.п.) ===
    overdue = bundle.today_snapshot.get("overdue_routines") or []
    if overdue:
        block = format_routines_block(overdue)
        if block:
            parts.append(block)
            parts.append("")

    # === Weekly plan (план текущей недели) ===
    wp = bundle.today_snapshot.get("weekly_plan")
    if wp:
        parts.append("## ПЛАН ТЕКУЩЕЙ НЕДЕЛИ")
        ws = wp.get("week_start")
        if ws:
            parts.append(f"_неделя начала: {ws}_")
        for i, f in enumerate(wp.get("focuses") or [], start=1):
            if isinstance(f, dict):
                parts.append(f"  {i}. {f.get('title', '?')} — {f.get('why', '')}".rstrip(" —"))
        if wp.get("experiment"):
            e = wp["experiment"]
            if isinstance(e, dict):
                parts.append(f"  🧪 Эксперимент: {e.get('what', '?')}")
        if wp.get("challenge"):
            c = wp["challenge"]
            if isinstance(c, dict):
                parts.append(f"  🎯 Челлендж: {c.get('what', '?')}")
        parts.append("")

    # === Profile ===
    parts.append("## ПРОФИЛЬ ПОЛЬЗОВАТЕЛЯ")
    parts.append(f"- Имя: {p.display_name or 'не указано'}")
    parts.append(f"- Часовой пояс: {p.timezone}")
    parts.append(f"- Сейчас локально: {bundle.today_snapshot.get('local_time', '?')}")
    if p.wake_window or p.sleep_window:
        parts.append(f"- Ритм: подъём ~{p.wake_window or '?'}, отбой ~{p.sleep_window or '?'}")
    parts.append(f"- Тон ({tone_desc})")

    if p.goals:
        parts.append("\n### Цели")
        for g in p.goals[:5]:
            parts.append(f"- {g}")
    if p.projects:
        parts.append("\n### Проекты")
        for proj in p.projects[:5]:
            parts.append(f"- {proj}")

    # === Today snapshot ===
    snap = bundle.today_snapshot
    if snap.get("habits_today"):
        parts.append("\n### Сегодня (привычки)")
        for h in snap["habits_today"]:
            parts.append(f"- {h['name']}: {h['done']} раз")
    if snap.get("yesterday_mood") is not None:
        parts.append(
            f"\n### Вчера: mood={snap.get('yesterday_mood')}, energy={snap.get('yesterday_energy')}"
        )

    # === Active tasks ===
    if bundle.active_tasks:
        parts.append("\n## АКТИВНЫЕ ЗАДАЧИ")
        for t in bundle.active_tasks:
            due = f" (до {t.due_at:%d.%m %H:%M})" if t.due_at else ""
            proj = f" [{t.project}]" if t.project else ""
            parts.append(f"- #{t.id} {t.title}{proj}{due}")

    # === Relevant facts (RAG) ===
    if bundle.relevant_facts:
        parts.append("\n## РЕЛЕВАНТНЫЕ ФАКТЫ ИЗ ДОЛГОСРОЧНОЙ ПАМЯТИ")
        for f in bundle.relevant_facts:
            when = f" ({f.created_at:%d.%m.%Y})" if f.created_at else ""
            parts.append(f"- [{f.kind}]{when} {f.content}")

    # === Diary recall ===
    if bundle.relevant_diary:
        parts.append("\n## ПОХОЖИЕ ДНЕВНИКОВЫЕ ЗАПИСИ")
        for d in bundle.relevant_diary:
            mood = f" mood={d.mood}" if d.mood else ""
            parts.append(f"- {d.entry_date}{mood}: {d.raw_text[:280]}")

    return "\n".join(parts)


# Веса по kind: фундаментальные приоритетнее одноразовых insight/routine
_KIND_WEIGHTS = {
    "health": 1.20,
    "preference": 1.15,
    "goal": 1.10,
    "project": 1.05,
    "relationship": 1.10,
    "event": 0.95,
    "routine": 0.90,
    "insight": 0.85,
}
_MIN_KIND_QUOTA = {"health": 1, "preference": 1, "goal": 1}


def _rerank_facts(candidates: list, target: int = 7) -> list:
    """Re-rank кандидатов по similarity + kind weight + freshness + confidence
    + квоты на ключевые kind. Принимает list[(Fact, distance)].
    """
    if not candidates:
        return []

    now = datetime.now(timezone.utc)

    scored = []
    for fact, distance in candidates:
        sim = max(0.0, 1.0 - distance)  # cosine distance → similarity
        kind_w = _KIND_WEIGHTS.get(fact.kind, 1.0)
        # freshness: last_referenced_at новее 30 дней — буст до +0.10
        freshness = 0.0
        ref = fact.last_referenced_at or fact.created_at
        if ref:
            age_days = (now - ref).total_seconds() / 86400
            if age_days < 7:
                freshness = 0.10
            elif age_days < 30:
                freshness = 0.05
        confidence_w = 0.95 + (fact.confidence or 0.8) * 0.10  # 0.95..1.05
        score = sim * kind_w * confidence_w + freshness
        scored.append((score, fact))

    scored.sort(key=lambda x: x[0], reverse=True)
    chosen: list = []
    chosen_ids: set = set()

    # 1. Сначала наполняем квоты для ключевых kinds
    for kind, quota in _MIN_KIND_QUOTA.items():
        added = 0
        for score, fact in scored:
            if added >= quota or len(chosen) >= target:
                break
            if fact.kind == kind and fact.id not in chosen_ids:
                chosen.append(fact)
                chosen_ids.add(fact.id)
                added += 1

    # 2. Добиваем оставшиеся по rank
    for score, fact in scored:
        if len(chosen) >= target:
            break
        if fact.id in chosen_ids:
            continue
        chosen.append(fact)
        chosen_ids.add(fact.id)

    return chosen


def to_anthropic_messages(
    recent: list[ConversationMsg], current_user_message: str
) -> list[dict]:
    """Превращает историю + текущее сообщение в формат messages для Anthropic API."""
    msgs: list[dict] = []
    for m in recent:
        if m.role == "system":
            continue
        msgs.append({"role": m.role, "content": m.content})
    # Anthropic требует чтобы последний message был user
    if not msgs or msgs[-1]["role"] != "user":
        msgs.append({"role": "user", "content": current_user_message})
    elif msgs[-1]["content"] != current_user_message:
        # последний user уже в истории (например, мы только что записали) — не дублируем
        pass
    return msgs
