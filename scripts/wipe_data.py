"""Полная чистка пользовательских данных, кроме profile.

Profile сохраняется (онбординг, цели, tone). Всё остальное — TRUNCATE с reset id.
"""
import asyncio
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


TABLES_TO_WIPE = [
    "conversations",
    "facts",
    "tasks",
    "diary_entries",
    "habit_logs",
    "habits",
    "locations",
    "pattern_signals",
    "tool_calls",
    "usage_log",
]


async def main():
    root = Path(__file__).resolve().parents[1]
    env = _read_env(root / ".env")
    dsn = env["DATABASE_URL"]

    conn = await asyncpg.connect(dsn, ssl="require", statement_cache_size=0)
    try:
        before = {}
        for t in TABLES_TO_WIPE + ["profile"]:
            before[t] = await conn.fetchval(f"SELECT COUNT(*) FROM {t}")

        joined = ", ".join(TABLES_TO_WIPE)
        await conn.execute(f"TRUNCATE TABLE {joined} RESTART IDENTITY CASCADE")

        after = {}
        for t in TABLES_TO_WIPE + ["profile"]:
            after[t] = await conn.fetchval(f"SELECT COUNT(*) FROM {t}")

        print(f"{'table':<20} {'before':>10} {'after':>10}")
        print("-" * 42)
        for t in TABLES_TO_WIPE + ["profile"]:
            mark = " <- kept" if t == "profile" else ""
            print(f"{t:<20} {before[t]:>10} {after[t]:>10}{mark}")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
