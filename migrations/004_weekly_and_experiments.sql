-- Phase 11: weekly cycle + challenge engine.

CREATE TABLE IF NOT EXISTS weekly_plans (
    id           BIGSERIAL PRIMARY KEY,
    user_id      BIGINT NOT NULL,
    week_start   DATE NOT NULL,                    -- понедельник локального ТЗ
    focuses      JSONB NOT NULL DEFAULT '[]'::jsonb, -- [{title, why, status}]
    experiment   JSONB,                              -- {what, why, how}
    challenge    JSONB,                              -- {what, why}
    review       JSONB,                              -- заполняется на восклый restart следующей недели
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (user_id, week_start)
);
CREATE INDEX IF NOT EXISTS weekly_plans_user_idx
    ON weekly_plans (user_id, week_start DESC);

CREATE TABLE IF NOT EXISTS experiments_log (
    id            BIGSERIAL PRIMARY KEY,
    user_id       BIGINT NOT NULL,
    proposed_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    title         TEXT NOT NULL,
    description   TEXT,
    source        TEXT NOT NULL DEFAULT 'challenge',  -- 'challenge'|'weekly'|'manual'
    accepted      BOOLEAN,                              -- TRUE/FALSE/NULL=пользователь не ответил
    accepted_at   TIMESTAMPTZ,
    completed     BOOLEAN,                              -- TRUE/FALSE/NULL=не отчитался
    result        TEXT,
    completed_at  TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS experiments_log_user_recent_idx
    ON experiments_log (user_id, proposed_at DESC);
