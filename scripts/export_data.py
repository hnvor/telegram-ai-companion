"""Экспорт всей пользовательской истории из Supabase для анализа."""
import asyncio
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import asyncpg


def _read_env(path: Path) -> dict:
    env = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip()
    return env


def _json_default(o):
    if isinstance(o, datetime):
        return o.astimezone(timezone.utc).isoformat()
    if isinstance(o, (bytes, bytearray, memoryview)):
        return f"<bytes:{len(bytes(o))}>"
    if hasattr(o, "isoformat"):
        return o.isoformat()
    return str(o)


async def main():
    root = Path(__file__).resolve().parents[1]
    env = _read_env(root / ".env")
    dsn = env["DATABASE_URL"]
    user_id = int(env["ALLOWED_USER_ID"])

    out_dir = root / "exports"
    out_dir.mkdir(exist_ok=True)

    conn = await asyncpg.connect(dsn, ssl="require", statement_cache_size=0)
    try:
        tables = {
            "profile": "SELECT * FROM profile WHERE user_id=$1",
            "conversations": "SELECT id, role, content, metadata, created_at FROM conversations WHERE user_id=$1 ORDER BY created_at",
            "facts": "SELECT id, kind, content, source_message_id, confidence, superseded_by, created_at, last_referenced_at FROM facts WHERE user_id=$1 ORDER BY created_at",
            "tasks": "SELECT * FROM tasks WHERE user_id=$1 ORDER BY created_at",
            "diary_entries": "SELECT id, entry_date, mood, energy, raw_text, structured, created_at FROM diary_entries WHERE user_id=$1 ORDER BY entry_date",
            "habits": "SELECT * FROM habits WHERE user_id=$1 ORDER BY created_at",
            "habit_logs": "SELECT hl.* FROM habit_logs hl JOIN habits h ON h.id=hl.habit_id WHERE h.user_id=$1 ORDER BY done_at",
            "locations": "SELECT * FROM locations WHERE user_id=$1 ORDER BY created_at",
            "pattern_signals": "SELECT * FROM pattern_signals WHERE user_id=$1 ORDER BY created_at",
            "tool_calls": "SELECT * FROM tool_calls WHERE user_id=$1 ORDER BY created_at",
            "usage_log": "SELECT id, model, input_tokens, output_tokens, cache_read, cache_write, purpose, created_at FROM usage_log WHERE user_id=$1 ORDER BY created_at",
        }

        summary = {}
        for name, q in tables.items():
            try:
                rows = await conn.fetch(q, user_id)
            except asyncpg.PostgresError as e:
                summary[name] = f"ERROR: {e}"
                continue
            rows_dicts = [dict(r) for r in rows]
            (out_dir / f"{name}.json").write_text(
                json.dumps(rows_dicts, ensure_ascii=False, indent=2, default=_json_default),
                encoding="utf-8",
            )
            summary[name] = len(rows_dicts)

        (out_dir / "_summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
