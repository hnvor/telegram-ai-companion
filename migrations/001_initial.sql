-- Personal Agent — initial schema
-- Run on Supabase (or any Postgres 14+ with pgvector available)

CREATE EXTENSION IF NOT EXISTS vector;

-- ============================================================================
-- profile: одна строка на пользователя (single-tenant, но user_id хранится)
-- ============================================================================
CREATE TABLE IF NOT EXISTS profile (
    user_id                 BIGINT PRIMARY KEY,
    display_name            TEXT,
    timezone                TEXT DEFAULT 'Asia/Bangkok',
    wake_window             TEXT,                          -- 'HH:MM-HH:MM' локального
    sleep_window            TEXT,
    goals                   JSONB NOT NULL DEFAULT '[]'::jsonb,
    projects                JSONB NOT NULL DEFAULT '[]'::jsonb,
    preferences             JSONB NOT NULL DEFAULT '{}'::jsonb,
    onboarding_completed_at TIMESTAMPTZ,
    paused_until            TIMESTAMPTZ,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ============================================================================
-- conversations: лог всех сообщений (краткосрочная память + аудит)
-- ============================================================================
CREATE TABLE IF NOT EXISTS conversations (
    id          BIGSERIAL PRIMARY KEY,
    user_id     BIGINT NOT NULL,
    role        TEXT NOT NULL CHECK (role IN ('user', 'assistant', 'system')),
    content     TEXT NOT NULL,
    metadata    JSONB,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS conversations_recent_idx
    ON conversations (user_id, created_at DESC);

-- ============================================================================
-- facts: извлечённые факты — основа долгосрочной памяти
-- ============================================================================
CREATE TABLE IF NOT EXISTS facts (
    id                  BIGSERIAL PRIMARY KEY,
    user_id             BIGINT NOT NULL,
    kind                TEXT NOT NULL,
    content             TEXT NOT NULL,
    source_message_id   BIGINT REFERENCES conversations(id) ON DELETE SET NULL,
    confidence          REAL NOT NULL DEFAULT 0.8,
    embedding           VECTOR(1024),
    superseded_by       BIGINT REFERENCES facts(id) ON DELETE SET NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_referenced_at  TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS facts_active_kind_idx
    ON facts (user_id, kind) WHERE superseded_by IS NULL;

CREATE INDEX IF NOT EXISTS facts_embedding_idx
    ON facts USING hnsw (embedding vector_cosine_ops);

-- ============================================================================
-- tasks: GTD
-- ============================================================================
CREATE TABLE IF NOT EXISTS tasks (
    id                BIGSERIAL PRIMARY KEY,
    user_id           BIGINT NOT NULL,
    title             TEXT NOT NULL,
    details           TEXT,
    project           TEXT,
    status            TEXT NOT NULL DEFAULT 'open'
                      CHECK (status IN ('open','doing','done','dropped')),
    priority          INT NOT NULL DEFAULT 3 CHECK (priority BETWEEN 1 AND 5),
    due_at            TIMESTAMPTZ,
    remind_at         TIMESTAMPTZ,
    postponed_count   INT NOT NULL DEFAULT 0,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at      TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS tasks_active_idx
    ON tasks (user_id, status, due_at NULLS LAST);

-- ============================================================================
-- diary_entries
-- ============================================================================
CREATE TABLE IF NOT EXISTS diary_entries (
    id          BIGSERIAL PRIMARY KEY,
    user_id     BIGINT NOT NULL,
    entry_date  DATE NOT NULL,
    mood        SMALLINT CHECK (mood BETWEEN 1 AND 10),
    energy      SMALLINT CHECK (energy BETWEEN 1 AND 10),
    raw_text    TEXT NOT NULL,
    structured  JSONB,
    embedding   VECTOR(1024),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (user_id, entry_date)
);

CREATE INDEX IF NOT EXISTS diary_embedding_idx
    ON diary_entries USING hnsw (embedding vector_cosine_ops);

CREATE INDEX IF NOT EXISTS diary_date_idx
    ON diary_entries (user_id, entry_date DESC);

-- ============================================================================
-- habits + habit_logs
-- ============================================================================
CREATE TABLE IF NOT EXISTS habits (
    id          BIGSERIAL PRIMARY KEY,
    user_id     BIGINT NOT NULL,
    name        TEXT NOT NULL,
    cadence     TEXT NOT NULL,
    target      JSONB,                                   -- {amount: 8, unit: 'glasses'}
    active      BOOLEAN NOT NULL DEFAULT TRUE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS habit_logs (
    id        BIGSERIAL PRIMARY KEY,
    habit_id  BIGINT NOT NULL REFERENCES habits(id) ON DELETE CASCADE,
    done_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    value     JSONB
);

CREATE INDEX IF NOT EXISTS habit_logs_habit_idx
    ON habit_logs (habit_id, done_at DESC);

-- ============================================================================
-- usage_log: трекинг токенов / стоимости (для /usage)
-- ============================================================================
CREATE TABLE IF NOT EXISTS usage_log (
    id              BIGSERIAL PRIMARY KEY,
    user_id         BIGINT NOT NULL,
    model           TEXT NOT NULL,
    input_tokens    INT NOT NULL DEFAULT 0,
    output_tokens   INT NOT NULL DEFAULT 0,
    cache_read      INT NOT NULL DEFAULT 0,
    cache_write     INT NOT NULL DEFAULT 0,
    purpose         TEXT,                                -- 'chat' | 'extraction' | 'tone_calib' | 'voice'
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS usage_log_recent_idx
    ON usage_log (user_id, created_at DESC);
