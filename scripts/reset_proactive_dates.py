"""Сброс дат последних проактивных пушей в profile.preferences."""
import asyncio
import json
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


KEYS_TO_DROP = ["evening_checkin_sent_date", "morning_brief_sent_date"]


async def main():
    root = Path(__file__).resolve().parents[1]
    env = _read_env(root / ".env")
    dsn = env["DATABASE_URL"]
    user_id = int(env["ALLOWED_USER_ID"])

    conn = await asyncpg.connect(dsn, ssl="require", statement_cache_size=0)
    try:
        row = await conn.fetchrow("SELECT preferences FROM profile WHERE user_id=$1", user_id)
        prefs = row["preferences"]
        if isinstance(prefs, str):
            prefs = json.loads(prefs)
        before = {k: prefs.get(k) for k in KEYS_TO_DROP}
        for k in KEYS_TO_DROP:
            prefs.pop(k, None)
        await conn.execute(
            "UPDATE profile SET preferences=$1::jsonb, updated_at=now() WHERE user_id=$2",
            json.dumps(prefs),
            user_id,
        )
        print(f"removed: {before}")
        print(f"remaining keys: {sorted(prefs.keys())}")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
