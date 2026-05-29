-- Phase 8: геолокация + pattern signals

-- Лог локаций пользователя (отправленных через Telegram или /where)
CREATE TABLE IF NOT EXISTS locations (
    id          BIGSERIAL PRIMARY KEY,
    user_id     BIGINT NOT NULL,
    lat         DOUBLE PRECISION,
    lon         DOUBLE PRECISION,
    label       TEXT,                          -- 'Хошимин, Вьетнам' (геокодинг или ручной /where X)
    accuracy_m  REAL,                          -- если Telegram прислал точность
    source      TEXT NOT NULL DEFAULT 'telegram', -- 'telegram' | 'manual' | 'inferred'
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS locations_user_recent_idx
    ON locations (user_id, created_at DESC);

-- Сигналы паттерн-детектора (что было выявлено и какое действие предпринято)
CREATE TABLE IF NOT EXISTS pattern_signals (
    id              BIGSERIAL PRIMARY KEY,
    user_id         BIGINT NOT NULL,
    signal_kind     TEXT NOT NULL,             -- 'low_mood_streak' | 'high_postpone' | 'no_movement' | 'sleep_debt' | 'rumination' | ...
    severity        TEXT NOT NULL DEFAULT 'medium', -- 'low' | 'medium' | 'high'
    evidence        JSONB,                     -- {dates: [...], samples: [...], stats: {...}}
    action_taken    TEXT,                      -- 'sent_message' | 'suggested_activity' | 'no_action_cooldown'
    cooldown_until  TIMESTAMPTZ,               -- чтобы не спамить тем же сигналом
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS pattern_signals_user_idx
    ON pattern_signals (user_id, signal_kind, created_at DESC);

-- Лог tool-call'ов агента (Phase 9: places/weather/etc) для аудита и дебага
CREATE TABLE IF NOT EXISTS tool_calls (
    id              BIGSERIAL PRIMARY KEY,
    user_id         BIGINT NOT NULL,
    tool_name       TEXT NOT NULL,             -- 'find_nearby' | 'get_weather' | 'web_search' | ...
    input           JSONB NOT NULL,
    output          JSONB,
    error           TEXT,
    duration_ms     INT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS tool_calls_recent_idx
    ON tool_calls (user_id, created_at DESC);
