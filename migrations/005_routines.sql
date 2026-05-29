-- Phase 12: routines — банальные вещи (душ, бритьё, ногти, движение).
-- Дополняет habits — отличается тем что у каждого routine есть cadence_days и
-- last_done_at для понимания «просрочено или нет».

CREATE TABLE IF NOT EXISTS routines (
    id            BIGSERIAL PRIMARY KEY,
    user_id       BIGINT NOT NULL,
    name          TEXT NOT NULL,                 -- 'shower' | 'shave' | 'nails' | 'movement' | 'water' | ...
    label         TEXT NOT NULL,                 -- человеческое название: 'душ', 'побриться'
    cadence_days  REAL NOT NULL DEFAULT 1,       -- через сколько дней нужно
    last_done_at  TIMESTAMPTZ,
    active        BOOLEAN NOT NULL DEFAULT TRUE,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (user_id, name)
);
CREATE INDEX IF NOT EXISTS routines_user_active_idx
    ON routines (user_id, active);
