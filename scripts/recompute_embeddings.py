"""Пересчёт embedding для facts и diary_entries (где сейчас NULL),
затем семантический дедуп facts через supersede.

Шаги:
  1. Загружает bge-m3 (~2GB при первом запуске).
  2. UPDATE facts SET embedding = ... WHERE embedding IS NULL.
  3. UPDATE diary_entries SET embedding = ... WHERE embedding IS NULL.
  4. Greedy clustering: пары fact-fact с cosine_sim >= THRESHOLD внутри одного user_id.
  5. В каждом кластере оставляем самый свежий, остальные → superseded_by = canonical.

Можно запустить с --dry-run чтобы посмотреть кластеры без записи supersede.
"""
import argparse
import asyncio
import json
import sys
from pathlib import Path

import asyncpg
from pgvector.asyncpg import register_vector

DEDUP_THRESHOLD = 0.88  # cosine similarity; 1.0 — идентичные, 0.0 — ортогональные
BATCH = 32
WITHIN_KIND_ONLY = True  # объединять только факты одного kind, чтобы не смешивать health/insight/routine


def _read_env(path: Path) -> dict:
    env = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip()
    return env


async def _init_conn(conn: asyncpg.Connection):
    schema = await conn.fetchval(
        "SELECT n.nspname FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace "
        "WHERE t.typname = 'vector' LIMIT 1"
    )
    try:
        await register_vector(conn, schema=schema or "public")
    except TypeError:
        await register_vector(conn)


def _load_model():
    from sentence_transformers import SentenceTransformer

    print("loading BAAI/bge-m3 (CPU, first run ~2GB download)...", flush=True)
    model = SentenceTransformer("BAAI/bge-m3", device="cpu")
    print("model loaded", flush=True)
    return model


def _embed_batch(model, texts: list[str]) -> list[list[float]]:
    vecs = model.encode(
        texts, normalize_embeddings=True, show_progress_bar=False, batch_size=BATCH
    )
    return [v.tolist() for v in vecs]


async def fill_missing(conn, model, table: str, text_col: str):
    rows = await conn.fetch(
        f"SELECT id, {text_col} AS txt FROM {table} WHERE embedding IS NULL ORDER BY id"
    )
    if not rows:
        print(f"{table}: nothing to embed")
        return 0
    print(f"{table}: embedding {len(rows)} rows...", flush=True)
    texts = [r["txt"] or "" for r in rows]
    vecs = _embed_batch(model, texts)
    for r, v in zip(rows, vecs):
        await conn.execute(
            f"UPDATE {table} SET embedding = $1 WHERE id = $2", v, r["id"]
        )
    print(f"{table}: filled {len(rows)} embeddings")
    return len(rows)


async def find_clusters(conn, user_id: int):
    """Greedy clustering: для каждого факта (по created_at ASC) проверяем все уже
    собранные кластеры; присоединяем к ближайшему canonical если cos_sim >= THRESHOLD."""
    rows = await conn.fetch(
        """
        SELECT id, kind, content, created_at, last_referenced_at, confidence, embedding
        FROM facts
        WHERE user_id = $1 AND superseded_by IS NULL AND embedding IS NOT NULL
        ORDER BY created_at ASC, id ASC
        """,
        user_id,
    )
    print(f"facts to cluster: {len(rows)}")

    # каждый кластер: {"members": [row], "canonical_idx": int}
    clusters: list[dict] = []
    # держим numpy для скорости
    import numpy as np
    embs = [np.array(r["embedding"], dtype=np.float32) for r in rows]
    # bge-m3 уже нормализованный, cos_sim = dot
    for i, r in enumerate(rows):
        best_c = -1
        best_sim = -1.0
        for ci, c in enumerate(clusters):
            cmember = rows[c["members"][0]]
            if WITHIN_KIND_ONLY and cmember["kind"] != r["kind"]:
                continue
            for mi in c["members"]:
                sim = float(np.dot(embs[i], embs[mi]))
                if sim > best_sim:
                    best_sim = sim
                    best_c = ci
        if best_c >= 0 and best_sim >= DEDUP_THRESHOLD:
            clusters[best_c]["members"].append(i)
        else:
            clusters.append({"members": [i]})

    # для каждого кластера canonical = последний по created_at
    for c in clusters:
        c["canonical_idx"] = c["members"][-1]
    return rows, clusters


async def apply_supersede(conn, rows, clusters, dry_run: bool):
    superseded_count = 0
    big_clusters = []
    for c in clusters:
        if len(c["members"]) <= 1:
            continue
        canonical = rows[c["canonical_idx"]]
        members = [rows[m] for m in c["members"]]
        big_clusters.append((canonical, members))
        if not dry_run:
            for m in members:
                if m["id"] == canonical["id"]:
                    continue
                await conn.execute(
                    "UPDATE facts SET superseded_by = $2 WHERE id = $1",
                    m["id"], canonical["id"],
                )
                superseded_count += 1
        else:
            superseded_count += len(members) - 1

    print(f"\nClusters with >1 member: {len(big_clusters)}")
    print(f"Facts to supersede: {superseded_count}")
    print()
    for canonical, members in big_clusters:
        print(f"  CANONICAL #{canonical['id']} [{canonical['kind']}] {canonical['content'][:90]}")
        for m in members:
            if m["id"] == canonical["id"]:
                continue
            print(f"    drop  #{m['id']:>3} [{m['kind']}] {m['content'][:90]}")
        print()
    return superseded_count


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--skip-embed", action="store_true",
                    help="не пересчитывать embedding, сразу делать дедуп")
    args = ap.parse_args()

    root = Path(__file__).resolve().parents[1]
    env = _read_env(root / ".env")
    user_id = int(env["ALLOWED_USER_ID"])

    conn = await asyncpg.connect(env["DATABASE_URL"], ssl="require", statement_cache_size=0)
    await _init_conn(conn)
    try:
        if not args.skip_embed:
            model = _load_model()
            await fill_missing(conn, model, "facts", "content")
            await fill_missing(conn, model, "diary_entries", "raw_text")

        rows, clusters = await find_clusters(conn, user_id)
        n = await apply_supersede(conn, rows, clusters, dry_run=args.dry_run)

        active = await conn.fetchval(
            "SELECT COUNT(*) FROM facts WHERE user_id=$1 AND superseded_by IS NULL",
            user_id,
        )
        total = await conn.fetchval("SELECT COUNT(*) FROM facts WHERE user_id=$1", user_id)
        mode = "DRY-RUN" if args.dry_run else "APPLIED"
        print(f"\n{mode}: facts active={active}, total={total}, would-supersede={n if args.dry_run else 'n/a'}")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
