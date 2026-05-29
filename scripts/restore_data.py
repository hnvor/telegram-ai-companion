"""Восстановить данные из exports/ в БД после нечаянного wipe.

Embeddings (vector(1024)) не были выгружены — оставляем NULL, потом пересчитаем
через src/services/embeddings.py.

Что восстанавливается:
- conversations (вся переписка)
- facts (без embedding)
- diary_entries (без embedding)
- locations
- pattern_signals
- habits + habit_logs
- tasks: только активные/будущие (с remind_at в будущем или открытые свежие)

НЕ восстанавливаем: tool_calls и usage_log (логи, регенерируются).
"""
import asyncio
import json
from datetime import date, datetime, timezone
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


def _load(p: Path) -> list:
    return json.loads(p.read_text(encoding="utf-8"))


def _parse_dt(s):
    if s is None:
        return None
    return datetime.fromisoformat(s)


def _maybe_jsonb(v):
    """Поля, которые в экспорте могли быть json-строкой или объектом."""
    if v is None:
        return None
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False)
    if isinstance(v, str):
        # экспорт иногда сохранял JSON-в-строке
        try:
            parsed = json.loads(v)
            return json.dumps(parsed, ensure_ascii=False)
        except Exception:
            return v
    return json.dumps(v, ensure_ascii=False)


async def main():
    root = Path(__file__).resolve().parents[1]
    env = _read_env(root / ".env")
    dsn = env["DATABASE_URL"]
    user_id = int(env["ALLOWED_USER_ID"])
    exp = root / "exports"

    conn = await asyncpg.connect(dsn, ssl="require", statement_cache_size=0)
    try:
        async with conn.transaction():
            # --- conversations ---
            convs = _load(exp / "conversations.json")
            for c in convs:
                await conn.execute(
                    """INSERT INTO conversations (id, user_id, role, content, metadata, created_at)
                       VALUES ($1, $2, $3, $4, $5::jsonb, $6)""",
                    c["id"], user_id, c["role"], c["content"],
                    _maybe_jsonb(c.get("metadata")), _parse_dt(c["created_at"]),
                )

            # --- facts (без embedding) ---
            facts = _load(exp / "facts.json")
            for f in facts:
                await conn.execute(
                    """INSERT INTO facts (id, user_id, kind, content, source_message_id,
                                          confidence, superseded_by, created_at, last_referenced_at)
                       VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)""",
                    f["id"], user_id, f["kind"], f["content"], f.get("source_message_id"),
                    float(f.get("confidence") or 0.8), f.get("superseded_by"),
                    _parse_dt(f["created_at"]), _parse_dt(f.get("last_referenced_at")),
                )

            # --- diary (без embedding) ---
            diary = _load(exp / "diary_entries.json")
            for d in diary:
                await conn.execute(
                    """INSERT INTO diary_entries (id, user_id, entry_date, mood, energy,
                                                  raw_text, structured, created_at)
                       VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8)""",
                    d["id"], user_id, date.fromisoformat(d["entry_date"]),
                    d.get("mood"), d.get("energy"),
                    d["raw_text"], _maybe_jsonb(d.get("structured")), _parse_dt(d["created_at"]),
                )

            # --- locations ---
            for loc in _load(exp / "locations.json"):
                await conn.execute(
                    """INSERT INTO locations (id, user_id, lat, lon, label, accuracy_m,
                                              source, created_at)
                       VALUES ($1, $2, $3, $4, $5, $6, $7, $8)""",
                    loc["id"], user_id, loc.get("lat"), loc.get("lon"), loc.get("label"),
                    loc.get("accuracy_m"), loc.get("source") or "telegram",
                    _parse_dt(loc["created_at"]),
                )

            # --- pattern_signals ---
            for s in _load(exp / "pattern_signals.json"):
                await conn.execute(
                    """INSERT INTO pattern_signals (id, user_id, signal_kind, severity,
                                                     evidence, action_taken, cooldown_until, created_at)
                       VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7, $8)""",
                    s["id"], user_id, s["signal_kind"], s.get("severity") or "medium",
                    _maybe_jsonb(s.get("evidence")), s.get("action_taken"),
                    _parse_dt(s.get("cooldown_until")), _parse_dt(s["created_at"]),
                )

            # --- habits + habit_logs ---
            for h in _load(exp / "habits.json"):
                await conn.execute(
                    """INSERT INTO habits (id, user_id, name, cadence, target, active, created_at)
                       VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7)""",
                    h["id"], user_id, h["name"], h["cadence"],
                    _maybe_jsonb(h.get("target")), h.get("active", True),
                    _parse_dt(h["created_at"]),
                )
            for hl in _load(exp / "habit_logs.json"):
                await conn.execute(
                    """INSERT INTO habit_logs (id, habit_id, done_at, value)
                       VALUES ($1, $2, $3, $4::jsonb)""",
                    hl["id"], hl["habit_id"], _parse_dt(hl["done_at"]),
                    _maybe_jsonb(hl.get("value")),
                )

            # --- tasks: только активные с будущим remind_at или последний утренний чекин ---
            now = datetime.now(timezone.utc)
            kept_tasks = []
            for t in _load(exp / "tasks.json"):
                remind = _parse_dt(t.get("remind_at"))
                # критерии: открытая с будущим напоминанием
                if t["status"] == "open" and remind and remind > now:
                    kept_tasks.append(t)
            for t in kept_tasks:
                await conn.execute(
                    """INSERT INTO tasks (id, user_id, title, details, project, status, priority,
                                          due_at, remind_at, postponed_count, created_at, completed_at)
                       VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)""",
                    t["id"], user_id, t["title"], t.get("details"), t.get("project"),
                    t["status"], int(t.get("priority") or 3),
                    _parse_dt(t.get("due_at")), _parse_dt(t.get("remind_at")),
                    int(t.get("postponed_count") or 0),
                    _parse_dt(t["created_at"]), _parse_dt(t.get("completed_at")),
                )

            # --- сдвинуть SERIAL-счётчики, чтобы новые id шли дальше ---
            for tbl in ("conversations", "facts", "diary_entries", "tasks",
                        "habits", "habit_logs", "locations", "pattern_signals"):
                await conn.execute(
                    f"SELECT setval(pg_get_serial_sequence('{tbl}', 'id'), "
                    f"COALESCE((SELECT MAX(id) FROM {tbl}), 1), "
                    f"(SELECT MAX(id) IS NOT NULL FROM {tbl}))"
                )

        # отчёт
        print(f"{'table':<20} {'restored':>10}")
        print("-" * 32)
        for t in ("conversations", "facts", "diary_entries", "locations",
                  "pattern_signals", "habits", "habit_logs", "tasks"):
            n = await conn.fetchval(f"SELECT COUNT(*) FROM {t}")
            print(f"{t:<20} {n:>10}")
        print()
        no_emb_facts = await conn.fetchval(
            "SELECT COUNT(*) FROM facts WHERE embedding IS NULL"
        )
        no_emb_diary = await conn.fetchval(
            "SELECT COUNT(*) FROM diary_entries WHERE embedding IS NULL"
        )
        print(f"facts без embedding (нужно пересчитать): {no_emb_facts}")
        print(f"diary без embedding (нужно пересчитать): {no_emb_diary}")
        print(f"\nkept tasks: {[t['id'] for t in kept_tasks]}")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
