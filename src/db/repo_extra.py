"""Репозитории для Phase 8/9: locations, pattern_signals, tool_calls."""

import json
from datetime import datetime, timedelta
from typing import Any

from src.db.client import get_pool


def _jsonb(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, default=str)


class LocationsRepo:
    @staticmethod
    async def latest(user_id: int) -> dict | None:
        pool = await get_pool()
        row = await pool.fetchrow(
            """
            SELECT id, lat, lon, label, source, created_at
            FROM locations
            WHERE user_id = $1
            ORDER BY created_at DESC LIMIT 1
            """,
            user_id,
        )
        return dict(row) if row else None

    @staticmethod
    async def recent(user_id: int, limit: int = 10) -> list[dict]:
        pool = await get_pool()
        rows = await pool.fetch(
            "SELECT id, lat, lon, label, created_at FROM locations "
            "WHERE user_id = $1 ORDER BY created_at DESC LIMIT $2",
            user_id,
            limit,
        )
        return [dict(r) for r in rows]


class PatternSignalsRepo:
    @staticmethod
    async def is_in_cooldown(user_id: int, signal_kind: str) -> bool:
        pool = await get_pool()
        row = await pool.fetchrow(
            """
            SELECT 1 FROM pattern_signals
            WHERE user_id = $1 AND signal_kind = $2 AND cooldown_until > now()
            ORDER BY created_at DESC LIMIT 1
            """,
            user_id,
            signal_kind,
        )
        return row is not None

    @staticmethod
    async def record(
        user_id: int,
        signal_kind: str,
        severity: str,
        evidence: dict,
        action_taken: str,
        cooldown_hours: int = 72,
    ) -> int:
        pool = await get_pool()
        cooldown_until = datetime.utcnow() + timedelta(hours=cooldown_hours)
        row = await pool.fetchrow(
            """
            INSERT INTO pattern_signals (user_id, signal_kind, severity, evidence, action_taken, cooldown_until)
            VALUES ($1, $2, $3, $4::jsonb, $5, $6)
            RETURNING id
            """,
            user_id,
            signal_kind,
            severity,
            _jsonb(evidence),
            action_taken,
            cooldown_until,
        )
        return row["id"]


class ToolCallsRepo:
    @staticmethod
    async def log(
        user_id: int,
        tool_name: str,
        input_data: dict,
        output_data: dict | None = None,
        error: str | None = None,
        duration_ms: int | None = None,
    ) -> None:
        pool = await get_pool()
        await pool.execute(
            """
            INSERT INTO tool_calls (user_id, tool_name, input, output, error, duration_ms)
            VALUES ($1, $2, $3::jsonb, $4::jsonb, $5, $6)
            """,
            user_id,
            tool_name,
            _jsonb(input_data) or "{}",
            _jsonb(output_data),
            error,
            duration_ms,
        )
