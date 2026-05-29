-- Phase 10: life_state — структурный портрет жизни пользователя.
-- Один JSON документ на user_id, обновляется фоновой Haiku-задачей.

CREATE TABLE IF NOT EXISTS life_state (
    user_id     BIGINT PRIMARY KEY,
    data        JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- awareness_anchor — лог отправленных якорей осознанности (для адаптации частоты).
CREATE TABLE IF NOT EXISTS awareness_anchors (
    id              BIGSERIAL PRIMARY KEY,
    user_id         BIGINT NOT NULL,
    text            TEXT NOT NULL,
    sent_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    user_responded  BOOLEAN NOT NULL DEFAULT FALSE,
    response_msg_id BIGINT REFERENCES conversations(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS awareness_anchors_user_recent_idx
    ON awareness_anchors (user_id, sent_at DESC);
