-- Per-player score collection and the recurring leaderboard post.
--
-- `/game report` will stop building one big score sheet in the reporting
-- channel: the pending game row is created immediately and every
-- participant is DM'd their own one-page sheet, with each row saved into
-- `game_participants` as it arrives (see game_participants_scores_paired in
-- 0003, which already tolerates a game holding some scored rows and some
-- NULL ones). `game_score_requests` is the bookkeeping table the 24-hour
-- scheduler sweep needs to find who still owes a score and re-prompt them,
-- up to a bounded number of rounds, without ever guessing at delivery
-- state it wasn't told.
--
-- The `guild_config` additions are unrelated but bundled here since both
-- are Phase 1 foundation work: a configurable recurring leaderboard post
-- (per-game or a fixed daily time), scoped to the season or all-time, that
-- remembers the last posted ranking so a future phase can render movement
-- arrows without re-deriving history.

CREATE TABLE game_score_requests (
    game_id         BIGINT NOT NULL,
    guild_id        BIGINT NOT NULL,
    user_id         BIGINT NOT NULL,
    dm_channel_id   BIGINT,
    dm_message_id   BIGINT,
    delivery_status TEXT NOT NULL DEFAULT 'pending'
        CHECK (delivery_status IN ('pending', 'delivered', 'blocked')),
    requested_at    TIMESTAMPTZ NOT NULL,
    next_prompt_at  TIMESTAMPTZ,
    prompts_sent    SMALLINT NOT NULL DEFAULT 0 CHECK (prompts_sent BETWEEN 0 AND 10),
    submitted_at    TIMESTAMPTZ,
    PRIMARY KEY (game_id, user_id),
    -- Paired like games_played_time_requires_timezone (0003) /
    -- game_participants_scores_paired (0003): a DM either has both a
    -- channel and a message id, or neither -- never a channel with no
    -- message (or vice versa) left dangling by a partial write.
    CONSTRAINT game_score_requests_dm_fields_paired
        CHECK ((dm_channel_id IS NULL) = (dm_message_id IS NULL)),
    FOREIGN KEY (game_id, guild_id) REFERENCES games (game_id, guild_id)
);

-- The scheduler sweep only ever looks for rows that still need a re-prompt
-- and haven't been submitted yet; a partial index keeps that scan to
-- exactly the rows that matter instead of every request ever made.
CREATE INDEX game_score_requests_pending_prompt
    ON game_score_requests (next_prompt_at)
    WHERE submitted_at IS NULL;

ALTER TABLE guild_config
    ADD COLUMN leaderboard_mode TEXT NOT NULL DEFAULT 'off'
        CHECK (leaderboard_mode IN ('off', 'per_game', 'daily')),
    ADD COLUMN leaderboard_channel_id BIGINT,
    ADD COLUMN leaderboard_scope TEXT NOT NULL DEFAULT 'season'
        CHECK (leaderboard_scope IN ('season', 'all_time')),
    ADD COLUMN leaderboard_daily_time TIME NOT NULL DEFAULT '22:00',
    ADD COLUMN leaderboard_last_posted_on DATE,
    -- An ordered JSON array of user ids from the previously posted board --
    -- not a relational table, since it's read back and replaced as a single
    -- unit (movement arrows compare this whole ordering to the new one) and
    -- is never queried by individual user id.
    ADD COLUMN leaderboard_last_ranking JSONB
        CHECK (leaderboard_last_ranking IS NULL
               OR jsonb_typeof(leaderboard_last_ranking) = 'array');
