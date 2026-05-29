"""Тонкий слой доступа к Postgres. Без ORM — чистый asyncpg + pgvector."""

import json
from datetime import date, datetime, timedelta
from typing import Any

import asyncpg

from src.db.client import get_pool
from src.domain.models import (
    ConversationMsg,
    DiaryEntry,
    Fact,
    Habit,
    Profile,
    TaskItem,
)


# ============================================================================
# helpers
# ============================================================================


def _row_to_dict(row: asyncpg.Record | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return dict(row)


def _jsonb(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _maybe_json(value: Any, default: Any = None) -> Any:
    """Supabase Pooler не пробрасывает type codecs, поэтому jsonb может прийти как str.
    Парсим в питон-объект безопасно."""
    if value is None:
        return default
    if isinstance(value, (list, dict)):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except Exception:
            return default
    return value


def _profile_from_row(row: asyncpg.Record) -> dict:
    d = dict(row)
    d["goals"] = _maybe_json(d.get("goals"), [])
    d["projects"] = _maybe_json(d.get("projects"), [])
    d["preferences"] = _maybe_json(d.get("preferences"), {})
    return d


def _diary_from_row(row: asyncpg.Record) -> dict:
    d = dict(row)
    d["structured"] = _maybe_json(d.get("structured"))
    return d


def _habit_from_row(row: asyncpg.Record) -> dict:
    d = dict(row)
    d["target"] = _maybe_json(d.get("target"))
    return d


def _conv_from_row(row: asyncpg.Record) -> dict:
    d = dict(row)
    d["metadata"] = _maybe_json(d.get("metadata"))
    return d


# ============================================================================
# Profile
# ============================================================================


class ProfileRepo:
    @staticmethod
    async def get(user_id: int) -> Profile | None:
        pool = await get_pool()
        row = await pool.fetchrow("SELECT * FROM profile WHERE user_id = $1", user_id)
        if row is None:
            return None
        return Profile(**_profile_from_row(row))

    @staticmethod
    async def upsert(profile: Profile) -> Profile:
        pool = await get_pool()
        row = await pool.fetchrow(
            """
            INSERT INTO profile (user_id, display_name, timezone, wake_window, sleep_window,
                                 goals, projects, preferences, onboarding_completed_at, paused_until)
            VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7::jsonb, $8::jsonb, $9, $10)
            ON CONFLICT (user_id) DO UPDATE SET
                display_name = EXCLUDED.display_name,
                timezone = EXCLUDED.timezone,
                wake_window = EXCLUDED.wake_window,
                sleep_window = EXCLUDED.sleep_window,
                goals = EXCLUDED.goals,
                projects = EXCLUDED.projects,
                preferences = EXCLUDED.preferences,
                onboarding_completed_at = COALESCE(EXCLUDED.onboarding_completed_at, profile.onboarding_completed_at),
                paused_until = EXCLUDED.paused_until,
                updated_at = now()
            RETURNING *
            """,
            profile.user_id,
            profile.display_name,
            profile.timezone,
            profile.wake_window,
            profile.sleep_window,
            _jsonb(profile.goals),
            _jsonb(profile.projects),
            _jsonb(profile.preferences),
            profile.onboarding_completed_at,
            profile.paused_until,
        )
        return Profile(**_profile_from_row(row))

    _PATCHABLE_FIELDS = frozenset(
        {
            "display_name",
            "timezone",
            "wake_window",
            "sleep_window",
            "goals",
            "projects",
            "preferences",
            "onboarding_completed_at",
            "paused_until",
        }
    )
    _JSONB_FIELDS = frozenset({"goals", "projects", "preferences"})

    @staticmethod
    async def patch(user_id: int, **fields: Any) -> None:
        if not fields:
            return
        unknown = set(fields) - ProfileRepo._PATCHABLE_FIELDS
        if unknown:
            raise ValueError(f"Unknown profile fields: {unknown}")
        pool = await get_pool()
        sets = []
        values: list[Any] = []
        for i, (k, v) in enumerate(fields.items(), start=2):
            if k in ProfileRepo._JSONB_FIELDS:
                sets.append(f"{k} = ${i}::jsonb")
                values.append(_jsonb(v))
            else:
                sets.append(f"{k} = ${i}")
                values.append(v)
        sets.append("updated_at = now()")
        await pool.execute(
            f"UPDATE profile SET {', '.join(sets)} WHERE user_id = $1",
            user_id,
            *values,
        )

    @staticmethod
    async def set_paused(user_id: int, until: datetime | None) -> None:
        pool = await get_pool()
        await pool.execute(
            "UPDATE profile SET paused_until = $2, updated_at = now() WHERE user_id = $1",
            user_id,
            until,
        )


# ============================================================================
# Conversations
# ============================================================================


class ConversationsRepo:
    @staticmethod
    async def append(
        user_id: int,
        role: str,
        content: str,
        metadata: dict[str, Any] | None = None,
    ) -> int:
        pool = await get_pool()
        row = await pool.fetchrow(
            """
            INSERT INTO conversations (user_id, role, content, metadata)
            VALUES ($1, $2, $3, $4::jsonb)
            RETURNING id
            """,
            user_id,
            role,
            content,
            _jsonb(metadata) if metadata is not None else None,
        )
        return row["id"]

    @staticmethod
    async def recent(user_id: int, limit: int = 15) -> list[ConversationMsg]:
        pool = await get_pool()
        rows = await pool.fetch(
            """
            SELECT id, role, content, metadata, created_at
            FROM conversations
            WHERE user_id = $1
            ORDER BY created_at DESC
            LIMIT $2
            """,
            user_id,
            limit,
        )
        return list(reversed([ConversationMsg(**_conv_from_row(r)) for r in rows]))


# ============================================================================
# Facts
# ============================================================================


class FactsRepo:
    @staticmethod
    async def insert(
        fact: Fact, embedding: list[float], dedup_threshold: float = 0.93
    ) -> int | None:
        """Вставляет факт, если нет семантического дубля (cosine similarity >= threshold).
        Возвращает id вставленного факта или None если был дубль."""
        pool = await get_pool()
        # Проверка на дубль: ищем ближайший активный факт
        if dedup_threshold > 0:
            row = await pool.fetchrow(
                """
                SELECT id, embedding <=> $2 AS distance
                FROM facts
                WHERE user_id = $1 AND superseded_by IS NULL AND embedding IS NOT NULL
                ORDER BY embedding <=> $2 LIMIT 1
                """,
                fact.user_id,
                embedding,
            )
            if row and row["distance"] is not None and row["distance"] < (1 - dedup_threshold):
                # Уже есть похожий факт — обновим last_referenced_at и пропустим
                await pool.execute(
                    "UPDATE facts SET last_referenced_at = now() WHERE id = $1", row["id"]
                )
                return None

        row = await pool.fetchrow(
            """
            INSERT INTO facts (user_id, kind, content, source_message_id, confidence, embedding)
            VALUES ($1, $2, $3, $4, $5, $6)
            RETURNING id
            """,
            fact.user_id,
            fact.kind,
            fact.content,
            fact.source_message_id,
            fact.confidence,
            embedding,
        )
        return row["id"]

    @staticmethod
    async def supersede(old_id: int, new_id: int) -> None:
        pool = await get_pool()
        await pool.execute(
            "UPDATE facts SET superseded_by = $2 WHERE id = $1",
            old_id,
            new_id,
        )

    @staticmethod
    async def search_similar(
        user_id: int, embedding: list[float], limit: int = 7
    ) -> list[Fact]:
        pool = await get_pool()
        rows = await pool.fetch(
            """
            SELECT id, user_id, kind, content, source_message_id, confidence,
                   superseded_by, created_at, last_referenced_at
            FROM facts
            WHERE user_id = $1 AND superseded_by IS NULL
            ORDER BY embedding <=> $2
            LIMIT $3
            """,
            user_id,
            embedding,
            limit,
        )
        return [Fact(**dict(r)) for r in rows]

    @staticmethod
    async def search_candidates(
        user_id: int, embedding: list[float], limit: int = 30
    ) -> list[tuple[Fact, float]]:
        """Возвращает кандидатов с cosine distance — для последующего re-rank."""
        pool = await get_pool()
        rows = await pool.fetch(
            """
            SELECT id, user_id, kind, content, source_message_id, confidence,
                   superseded_by, created_at, last_referenced_at,
                   (embedding <=> $2) AS distance
            FROM facts
            WHERE user_id = $1 AND superseded_by IS NULL AND embedding IS NOT NULL
            ORDER BY embedding <=> $2
            LIMIT $3
            """,
            user_id, embedding, limit,
        )
        out: list[tuple[Fact, float]] = []
        for r in rows:
            d = dict(r)
            distance = float(d.pop("distance", 1.0) or 1.0)
            out.append((Fact(**d), distance))
        return out

    @staticmethod
    async def list_by_kind(user_id: int, kind: str, limit: int = 20) -> list[Fact]:
        pool = await get_pool()
        rows = await pool.fetch(
            """
            SELECT id, user_id, kind, content, source_message_id, confidence,
                   superseded_by, created_at, last_referenced_at
            FROM facts
            WHERE user_id = $1 AND kind = $2 AND superseded_by IS NULL
            ORDER BY created_at DESC
            LIMIT $3
            """,
            user_id,
            kind,
            limit,
        )
        return [Fact(**dict(r)) for r in rows]

    @staticmethod
    async def mark_referenced(ids: list[int]) -> None:
        if not ids:
            return
        pool = await get_pool()
        await pool.execute(
            "UPDATE facts SET last_referenced_at = now() WHERE id = ANY($1::bigint[])",
            ids,
        )


# ============================================================================
# Tasks
# ============================================================================


class TasksRepo:
    @staticmethod
    async def create(task: TaskItem) -> TaskItem:
        pool = await get_pool()
        row = await pool.fetchrow(
            """
            INSERT INTO tasks (user_id, title, details, project, status, priority, due_at, remind_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            RETURNING *
            """,
            task.user_id,
            task.title,
            task.details,
            task.project,
            task.status,
            task.priority,
            task.due_at,
            task.remind_at,
        )
        return TaskItem(**dict(row))

    @staticmethod
    async def get(task_id: int) -> TaskItem | None:
        pool = await get_pool()
        row = await pool.fetchrow("SELECT * FROM tasks WHERE id = $1", task_id)
        return TaskItem(**dict(row)) if row else None

    @staticmethod
    async def list_active(
        user_id: int,
        limit: int = 30,
        max_age_days: int | None = None,
        max_postponed: int | None = None,
    ) -> list[TaskItem]:
        pool = await get_pool()
        where = ["user_id = $1", "status IN ('open', 'doing')"]
        args: list = [user_id]
        if max_age_days is not None:
            args.append(max_age_days)
            where.append(f"created_at > now() - make_interval(days => ${len(args)})")
        if max_postponed is not None:
            args.append(max_postponed)
            where.append(f"postponed_count < ${len(args)}")
        args.append(limit)
        sql = (
            "SELECT * FROM tasks "
            f"WHERE {' AND '.join(where)} "
            "ORDER BY priority ASC, due_at ASC NULLS LAST, created_at ASC "
            f"LIMIT ${len(args)}"
        )
        rows = await pool.fetch(sql, *args)
        return [TaskItem(**dict(r)) for r in rows]

    @staticmethod
    async def mark_done(task_id: int) -> None:
        pool = await get_pool()
        await pool.execute(
            "UPDATE tasks SET status = 'done', completed_at = now() WHERE id = $1",
            task_id,
        )

    @staticmethod
    async def update_status(task_id: int, status: str) -> None:
        pool = await get_pool()
        await pool.execute(
            "UPDATE tasks SET status = $2 WHERE id = $1",
            task_id,
            status,
        )

    @staticmethod
    async def postpone(task_id: int, new_remind_at: datetime) -> None:
        pool = await get_pool()
        await pool.execute(
            """
            UPDATE tasks
            SET remind_at = $2, postponed_count = postponed_count + 1
            WHERE id = $1
            """,
            task_id,
            new_remind_at,
        )

    @staticmethod
    async def chronic_procrastinated(user_id: int, threshold: int = 3) -> list[TaskItem]:
        pool = await get_pool()
        rows = await pool.fetch(
            """
            SELECT * FROM tasks
            WHERE user_id = $1 AND status IN ('open','doing') AND postponed_count >= $2
            ORDER BY postponed_count DESC
            """,
            user_id,
            threshold,
        )
        return [TaskItem(**dict(r)) for r in rows]


# ============================================================================
# Diary
# ============================================================================


class DiaryRepo:
    @staticmethod
    async def upsert(entry: DiaryEntry, embedding: list[float] | None) -> DiaryEntry:
        pool = await get_pool()
        row = await pool.fetchrow(
            """
            INSERT INTO diary_entries (user_id, entry_date, mood, energy, raw_text, structured, embedding)
            VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7)
            ON CONFLICT (user_id, entry_date) DO UPDATE SET
                mood = EXCLUDED.mood,
                energy = EXCLUDED.energy,
                raw_text = diary_entries.raw_text || E'\n---\n' || EXCLUDED.raw_text,
                structured = EXCLUDED.structured,
                embedding = EXCLUDED.embedding
            RETURNING *
            """,
            entry.user_id,
            entry.entry_date,
            entry.mood,
            entry.energy,
            entry.raw_text,
            _jsonb(entry.structured) if entry.structured else None,
            embedding,
        )
        return DiaryEntry(**_diary_from_row(row))

    @staticmethod
    async def search_similar(
        user_id: int, embedding: list[float], limit: int = 3
    ) -> list[DiaryEntry]:
        pool = await get_pool()
        rows = await pool.fetch(
            """
            SELECT id, user_id, entry_date, mood, energy, raw_text, structured, created_at
            FROM diary_entries
            WHERE user_id = $1 AND embedding IS NOT NULL
            ORDER BY embedding <=> $2
            LIMIT $3
            """,
            user_id,
            embedding,
            limit,
        )
        return [DiaryEntry(**_diary_from_row(r)) for r in rows]

    @staticmethod
    async def get_by_date(user_id: int, d: date) -> DiaryEntry | None:
        pool = await get_pool()
        row = await pool.fetchrow(
            "SELECT * FROM diary_entries WHERE user_id = $1 AND entry_date = $2",
            user_id,
            d,
        )
        return DiaryEntry(**_diary_from_row(row)) if row else None

    @staticmethod
    async def export_all(user_id: int) -> list[dict[str, Any]]:
        pool = await get_pool()
        rows = await pool.fetch(
            "SELECT entry_date, mood, energy, raw_text, structured FROM diary_entries "
            "WHERE user_id = $1 ORDER BY entry_date ASC",
            user_id,
        )
        return [
            {**dict(r), "structured": _maybe_json(dict(r).get("structured"))} for r in rows
        ]


# ============================================================================
# Habits
# ============================================================================


class HabitsRepo:
    @staticmethod
    async def list_active(user_id: int) -> list[Habit]:
        pool = await get_pool()
        rows = await pool.fetch(
            "SELECT * FROM habits WHERE user_id = $1 AND active = TRUE",
            user_id,
        )
        return [Habit(**_habit_from_row(r)) for r in rows]

    @staticmethod
    async def create(habit: Habit) -> Habit:
        pool = await get_pool()
        row = await pool.fetchrow(
            """
            INSERT INTO habits (user_id, name, cadence, target, active)
            VALUES ($1, $2, $3, $4::jsonb, $5)
            RETURNING *
            """,
            habit.user_id,
            habit.name,
            habit.cadence,
            _jsonb(habit.target) if habit.target else None,
            habit.active,
        )
        return Habit(**_habit_from_row(row))

    @staticmethod
    async def log(habit_id: int, value: dict[str, Any] | None = None) -> None:
        pool = await get_pool()
        await pool.execute(
            "INSERT INTO habit_logs (habit_id, value) VALUES ($1, $2::jsonb)",
            habit_id,
            _jsonb(value) if value else None,
        )

    @staticmethod
    async def count_today(habit_id: int, day_start: datetime, day_end: datetime) -> int:
        pool = await get_pool()
        row = await pool.fetchrow(
            "SELECT COUNT(*) AS n FROM habit_logs WHERE habit_id = $1 AND done_at >= $2 AND done_at < $3",
            habit_id,
            day_start,
            day_end,
        )
        return int(row["n"]) if row else 0


# ============================================================================
# Usage logging
# ============================================================================


class UsageRepo:
    @staticmethod
    async def log(
        user_id: int,
        model: str,
        input_tokens: int,
        output_tokens: int,
        cache_read: int = 0,
        cache_write: int = 0,
        purpose: str = "chat",
    ) -> None:
        pool = await get_pool()
        await pool.execute(
            """
            INSERT INTO usage_log (user_id, model, input_tokens, output_tokens, cache_read, cache_write, purpose)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            """,
            user_id,
            model,
            input_tokens,
            output_tokens,
            cache_read,
            cache_write,
            purpose,
        )

    @staticmethod
    async def summary(user_id: int, since: datetime) -> list[dict[str, Any]]:
        pool = await get_pool()
        rows = await pool.fetch(
            """
            SELECT model,
                   SUM(input_tokens) AS input_tokens,
                   SUM(output_tokens) AS output_tokens,
                   SUM(cache_read) AS cache_read,
                   SUM(cache_write) AS cache_write,
                   COUNT(*) AS calls
            FROM usage_log
            WHERE user_id = $1 AND created_at >= $2
            GROUP BY model
            """,
            user_id,
            since,
        )
        return [dict(r) for r in rows]


# ============================================================================
# Life state — структурный портрет жизни пользователя
# ============================================================================


class LifeStateRepo:
    @staticmethod
    async def get(user_id: int) -> dict[str, Any]:
        pool = await get_pool()
        row = await pool.fetchrow(
            "SELECT data FROM life_state WHERE user_id = $1", user_id
        )
        if row is None:
            return {}
        return _maybe_json(row["data"], {}) or {}

    @staticmethod
    async def upsert(user_id: int, data: dict[str, Any]) -> None:
        pool = await get_pool()
        await pool.execute(
            """
            INSERT INTO life_state (user_id, data, updated_at)
            VALUES ($1, $2::jsonb, now())
            ON CONFLICT (user_id) DO UPDATE SET
                data = EXCLUDED.data,
                updated_at = now()
            """,
            user_id,
            _jsonb(data),
        )

    @staticmethod
    async def updated_at(user_id: int) -> datetime | None:
        pool = await get_pool()
        return await pool.fetchval(
            "SELECT updated_at FROM life_state WHERE user_id = $1", user_id
        )


# ============================================================================
# Awareness anchors
# ============================================================================


class AnchorsRepo:
    @staticmethod
    async def log(user_id: int, text: str) -> int:
        pool = await get_pool()
        return await pool.fetchval(
            "INSERT INTO awareness_anchors (user_id, text) VALUES ($1, $2) RETURNING id",
            user_id,
            text,
        )

    @staticmethod
    async def last_sent_at(user_id: int) -> datetime | None:
        pool = await get_pool()
        return await pool.fetchval(
            "SELECT MAX(sent_at) FROM awareness_anchors WHERE user_id = $1",
            user_id,
        )

    @staticmethod
    async def recent_texts(user_id: int, limit: int = 8) -> list[str]:
        pool = await get_pool()
        rows = await pool.fetch(
            "SELECT text FROM awareness_anchors WHERE user_id = $1 "
            "ORDER BY sent_at DESC LIMIT $2",
            user_id, limit,
        )
        return [r["text"] for r in rows]

    @staticmethod
    async def count_today(user_id: int, day_start_utc: datetime) -> int:
        pool = await get_pool()
        return int(await pool.fetchval(
            "SELECT COUNT(*) FROM awareness_anchors "
            "WHERE user_id = $1 AND sent_at >= $2",
            user_id, day_start_utc,
        ) or 0)

    @staticmethod
    async def ignored_streak(user_id: int) -> int:
        """Сколько последних якорей подряд остались без user-ответа в течение 2 часов."""
        pool = await get_pool()
        rows = await pool.fetch(
            """
            SELECT a.id, a.sent_at,
                   EXISTS(
                     SELECT 1 FROM conversations c
                     WHERE c.user_id = a.user_id AND c.role = 'user'
                       AND c.created_at > a.sent_at
                       AND c.created_at < a.sent_at + interval '2 hours'
                   ) AS responded
            FROM awareness_anchors a
            WHERE a.user_id = $1
            ORDER BY a.sent_at DESC
            LIMIT 10
            """,
            user_id,
        )
        streak = 0
        for r in rows:
            if r["responded"]:
                break
            streak += 1
        return streak


# ============================================================================
# Weekly plans
# ============================================================================


class WeeklyPlanRepo:
    @staticmethod
    async def get(user_id: int, week_start: date) -> dict[str, Any] | None:
        pool = await get_pool()
        row = await pool.fetchrow(
            "SELECT id, week_start, focuses, experiment, challenge, review, created_at "
            "FROM weekly_plans WHERE user_id = $1 AND week_start = $2",
            user_id, week_start,
        )
        if row is None:
            return None
        d = dict(row)
        d["focuses"] = _maybe_json(d.get("focuses"), [])
        d["experiment"] = _maybe_json(d.get("experiment"))
        d["challenge"] = _maybe_json(d.get("challenge"))
        d["review"] = _maybe_json(d.get("review"))
        return d

    @staticmethod
    async def upsert(
        user_id: int, week_start: date,
        focuses: list, experiment: dict | None, challenge: dict | None,
    ) -> int:
        pool = await get_pool()
        return await pool.fetchval(
            """
            INSERT INTO weekly_plans (user_id, week_start, focuses, experiment, challenge)
            VALUES ($1, $2, $3::jsonb, $4::jsonb, $5::jsonb)
            ON CONFLICT (user_id, week_start) DO UPDATE SET
                focuses = EXCLUDED.focuses,
                experiment = EXCLUDED.experiment,
                challenge = EXCLUDED.challenge
            RETURNING id
            """,
            user_id, week_start, _jsonb(focuses),
            _jsonb(experiment) if experiment else None,
            _jsonb(challenge) if challenge else None,
        )

    @staticmethod
    async def previous(user_id: int, before_week: date) -> dict[str, Any] | None:
        pool = await get_pool()
        row = await pool.fetchrow(
            "SELECT id, week_start, focuses, experiment, challenge, review "
            "FROM weekly_plans WHERE user_id = $1 AND week_start < $2 "
            "ORDER BY week_start DESC LIMIT 1",
            user_id, before_week,
        )
        if row is None:
            return None
        d = dict(row)
        d["focuses"] = _maybe_json(d.get("focuses"), [])
        d["experiment"] = _maybe_json(d.get("experiment"))
        d["challenge"] = _maybe_json(d.get("challenge"))
        d["review"] = _maybe_json(d.get("review"))
        return d

    @staticmethod
    async def set_review(plan_id: int, review: dict) -> None:
        pool = await get_pool()
        await pool.execute(
            "UPDATE weekly_plans SET review = $2::jsonb WHERE id = $1",
            plan_id, _jsonb(review),
        )


# ============================================================================
# Experiments log (challenges + weekly experiments)
# ============================================================================


class ExperimentsRepo:
    @staticmethod
    async def create(
        user_id: int, title: str, description: str | None, source: str = "challenge",
    ) -> int:
        pool = await get_pool()
        return await pool.fetchval(
            "INSERT INTO experiments_log (user_id, title, description, source) "
            "VALUES ($1, $2, $3, $4) RETURNING id",
            user_id, title, description, source,
        )

    @staticmethod
    async def set_accepted(experiment_id: int, accepted: bool) -> None:
        pool = await get_pool()
        await pool.execute(
            "UPDATE experiments_log SET accepted = $2, accepted_at = now() WHERE id = $1",
            experiment_id, accepted,
        )

    @staticmethod
    async def set_completed(experiment_id: int, completed: bool, result: str | None = None) -> None:
        pool = await get_pool()
        await pool.execute(
            "UPDATE experiments_log SET completed = $2, result = $3, completed_at = now() "
            "WHERE id = $1",
            experiment_id, completed, result,
        )

    @staticmethod
    async def recent(user_id: int, days: int = 60, limit: int = 30) -> list[dict[str, Any]]:
        pool = await get_pool()
        rows = await pool.fetch(
            "SELECT id, proposed_at, title, description, source, accepted, completed, result "
            "FROM experiments_log "
            "WHERE user_id = $1 AND proposed_at > now() - make_interval(days => $2) "
            "ORDER BY proposed_at DESC LIMIT $3",
            user_id, days, limit,
        )
        return [dict(r) for r in rows]

    @staticmethod
    async def get(experiment_id: int) -> dict[str, Any] | None:
        pool = await get_pool()
        row = await pool.fetchrow("SELECT * FROM experiments_log WHERE id = $1", experiment_id)
        return dict(row) if row else None


# ============================================================================
# Routines (банальные вещи: душ, бритьё, ногти, движение, вода)
# ============================================================================


class RoutinesRepo:
    @staticmethod
    async def list_active(user_id: int) -> list[dict[str, Any]]:
        pool = await get_pool()
        rows = await pool.fetch(
            "SELECT id, name, label, cadence_days, last_done_at FROM routines "
            "WHERE user_id=$1 AND active=TRUE ORDER BY id",
            user_id,
        )
        return [dict(r) for r in rows]

    @staticmethod
    async def overdue(user_id: int) -> list[dict[str, Any]]:
        """Routines, у которых (now - last_done_at) > cadence_days. last=NULL — просрочено."""
        pool = await get_pool()
        rows = await pool.fetch(
            """
            SELECT id, name, label, cadence_days, last_done_at,
                   CASE WHEN last_done_at IS NULL THEN NULL
                        ELSE EXTRACT(EPOCH FROM (now() - last_done_at)) / 86400
                   END AS days_since
            FROM routines
            WHERE user_id=$1 AND active=TRUE
              AND (last_done_at IS NULL
                   OR (now() - last_done_at) > make_interval(secs => cadence_days * 86400))
            ORDER BY (now() - COALESCE(last_done_at, now() - interval '30 days')) DESC
            """,
            user_id,
        )
        return [dict(r) for r in rows]

    @staticmethod
    async def mark_done(user_id: int, name: str) -> bool:
        pool = await get_pool()
        result = await pool.execute(
            "UPDATE routines SET last_done_at = now() WHERE user_id=$1 AND name=$2",
            user_id, name,
        )
        return result.endswith(" 1")

    @staticmethod
    async def mark_done_by_id(routine_id: int) -> None:
        pool = await get_pool()
        await pool.execute(
            "UPDATE routines SET last_done_at = now() WHERE id = $1", routine_id
        )
