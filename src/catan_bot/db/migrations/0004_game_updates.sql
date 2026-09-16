-- Editable confirmed-game history.  Participant rows are retained as an
-- append-only-ish roster history: only the active rows count toward reports
-- and statistics, while game_updates preserves the authoritative snapshots.

ALTER TABLE games
    ADD COLUMN revision INTEGER NOT NULL DEFAULT 0 CHECK (revision >= 0),
    ADD COLUMN updated_by BIGINT,
    ADD COLUMN updated_at TIMESTAMPTZ,
    ADD COLUMN update_reason TEXT CHECK (char_length(update_reason) BETWEEN 1 AND 200),
    ADD CONSTRAINT games_update_actor_and_time_paired
        CHECK ((updated_by IS NULL) = (updated_at IS NULL));

ALTER TABLE game_participants
    ADD COLUMN is_active BOOLEAN NOT NULL DEFAULT true;

DROP INDEX game_participants_one_winner_per_game;
CREATE UNIQUE INDEX game_participants_one_active_winner_per_game
    ON game_participants (game_id)
    WHERE is_active AND is_winner;

CREATE TABLE game_updates (
    game_id         BIGINT NOT NULL,
    revision        INTEGER NOT NULL CHECK (revision >= 1),
    guild_id        BIGINT NOT NULL,
    updated_by      BIGINT NOT NULL,
    updated_at      TIMESTAMPTZ NOT NULL,
    reason          TEXT CHECK (char_length(reason) BETWEEN 1 AND 200),
    before_snapshot JSONB NOT NULL CHECK (jsonb_typeof(before_snapshot) = 'object'),
    after_snapshot  JSONB NOT NULL CHECK (jsonb_typeof(after_snapshot) = 'object'),
    PRIMARY KEY (game_id, revision),
    FOREIGN KEY (game_id, guild_id) REFERENCES games (game_id, guild_id)
);

-- Default privileges give the runtime SELECT/INSERT/UPDATE on new tables.
-- The audit is deliberately append-only to the application role.
REVOKE UPDATE, DELETE ON game_updates FROM catan_app;
