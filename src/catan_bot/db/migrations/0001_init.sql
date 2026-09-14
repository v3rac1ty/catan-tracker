-- Catan Tracker initial schema.
-- Applied by catan_migrator via src/catan_bot/db/migrate.py.

CREATE TABLE guild_config (
    guild_id            BIGINT PRIMARY KEY,
    timezone            TEXT NOT NULL DEFAULT 'UTC'
                             CHECK (char_length(timezone) BETWEEN 1 AND 64),
    announce_channel_id BIGINT,
    admin_role_id       BIGINT,
    default_min_games   INT NOT NULL DEFAULT 2
                             CHECK (default_min_games BETWEEN 1 AND 100),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE players (
    guild_id   BIGINT NOT NULL REFERENCES guild_config (guild_id),
    user_id    BIGINT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (guild_id, user_id)
);

CREATE TABLE seasons (
    season_id    BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    guild_id     BIGINT NOT NULL REFERENCES guild_config (guild_id),
    name         TEXT NOT NULL CHECK (char_length(name) BETWEEN 1 AND 100),
    starts_on    DATE NOT NULL,
    ends_on      DATE NOT NULL,
    ends_at      TIMESTAMPTZ NOT NULL,
    min_games    INT NOT NULL DEFAULT 2 CHECK (min_games BETWEEN 1 AND 100),
    status       TEXT NOT NULL DEFAULT 'active'
                     CHECK (status IN ('active', 'completed', 'cancelled')),
    resolved_at  TIMESTAMPTZ,
    announced_at TIMESTAMPTZ,
    created_by   BIGINT NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (ends_on >= starts_on),
    -- Lets `games` carry a composite (season_id, guild_id) FK, so a game
    -- can never reference a season belonging to a different guild.
    UNIQUE (season_id, guild_id)
);

-- Only one active season per guild at a time.
CREATE UNIQUE INDEX seasons_one_active_per_guild
    ON seasons (guild_id)
    WHERE status = 'active';

CREATE TABLE season_results (
    season_id BIGINT NOT NULL,
    guild_id  BIGINT NOT NULL,
    user_id   BIGINT NOT NULL,
    rank      INT NOT NULL CHECK (rank >= 1),
    games     INT NOT NULL CHECK (games >= 0),
    wins      INT NOT NULL,
    eligible  BOOLEAN NOT NULL,
    outcome   TEXT CHECK (outcome IN ('payer', 'payee')),
    PRIMARY KEY (season_id, user_id),
    CONSTRAINT season_results_wins_le_games CHECK (wins BETWEEN 0 AND games),
    -- Ties this result to a season *and* that season's own guild, so a
    -- result can never be attached to a season from a different guild.
    FOREIGN KEY (season_id, guild_id) REFERENCES seasons (season_id, guild_id),
    -- The user must be a registered player of that same guild.
    FOREIGN KEY (guild_id, user_id) REFERENCES players (guild_id, user_id)
);

CREATE TABLE games (
    game_id      BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    guild_id     BIGINT NOT NULL REFERENCES guild_config (guild_id),
    season_id    BIGINT,
    played_on    DATE NOT NULL,
    status       TEXT NOT NULL DEFAULT 'pending'
                     CHECK (status IN ('pending', 'confirmed', 'rejected', 'voided')),
    reported_by  BIGINT NOT NULL,
    confirmed_by BIGINT,
    confirmed_at TIMESTAMPTZ,
    voided_by    BIGINT,
    voided_at    TIMESTAMPTZ,
    void_reason  TEXT CHECK (char_length(void_reason) <= 200),
    rejected_by  BIGINT,
    rejected_at  TIMESTAMPTZ,
    channel_id   BIGINT,
    message_id   BIGINT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT games_confirmed_by_ne_reported_by
        CHECK (confirmed_by IS NULL OR confirmed_by <> reported_by),
    CONSTRAINT games_confirmed_requires_fields
        CHECK (status <> 'confirmed' OR (confirmed_by IS NOT NULL AND confirmed_at IS NOT NULL)),
    CONSTRAINT games_voided_requires_fields
        CHECK (status <> 'voided' OR (voided_by IS NOT NULL AND voided_at IS NOT NULL)),
    CONSTRAINT games_rejected_requires_fields
        CHECK (status <> 'rejected' OR (rejected_by IS NOT NULL AND rejected_at IS NOT NULL)),
    -- Lets `game_participants` carry a composite (game_id, guild_id) FK, so
    -- a participant row can never reference a game in a different guild.
    UNIQUE (game_id, guild_id),
    FOREIGN KEY (season_id, guild_id) REFERENCES seasons (season_id, guild_id)
);

-- Speeds up the season-resolution / leaderboard query (confirmed games for
-- a guild, optionally scoped to a season).
CREATE INDEX games_confirmed_by_guild_season
    ON games (guild_id, season_id)
    WHERE status = 'confirmed';

CREATE TABLE game_participants (
    game_id   BIGINT NOT NULL,
    user_id   BIGINT NOT NULL,
    guild_id  BIGINT NOT NULL,
    is_winner BOOLEAN NOT NULL DEFAULT false,
    PRIMARY KEY (game_id, user_id),
    FOREIGN KEY (guild_id, user_id) REFERENCES players (guild_id, user_id),
    -- Also guarantees a participant's guild_id matches their game's guild_id.
    FOREIGN KEY (game_id, guild_id) REFERENCES games (game_id, guild_id)
);

-- At most one winner per game. Winner + losers are inserted in a single
-- transaction, which guarantees exactly one winner for confirmed games.
CREATE UNIQUE INDEX game_participants_one_winner_per_game
    ON game_participants (game_id)
    WHERE is_winner;

CREATE INDEX game_participants_guild_user
    ON game_participants (guild_id, user_id);

CREATE TABLE events (
    event_id    BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    guild_id    BIGINT NOT NULL REFERENCES guild_config (guild_id),
    title       TEXT NOT NULL CHECK (char_length(title) BETWEEN 1 AND 100),
    description TEXT CHECK (char_length(description) <= 1000),
    location    TEXT CHECK (char_length(location) <= 200),
    starts_at   TIMESTAMPTZ NOT NULL,
    status      TEXT NOT NULL DEFAULT 'scheduled'
                    CHECK (status IN ('scheduled', 'cancelled', 'completed')),
    created_by  BIGINT NOT NULL,
    channel_id  BIGINT,
    message_id  BIGINT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX events_scheduled_starts_at
    ON events (starts_at)
    WHERE status = 'scheduled';

CREATE TABLE event_rsvps (
    event_id   BIGINT NOT NULL REFERENCES events (event_id),
    user_id    BIGINT NOT NULL,
    response   TEXT NOT NULL CHECK (response IN ('going', 'maybe', 'not_going')),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (event_id, user_id)
);

CREATE TABLE event_reminders (
    event_id       BIGINT NOT NULL REFERENCES events (event_id),
    offset_minutes INT NOT NULL CHECK (offset_minutes IN (1440, 60)),
    remind_at      TIMESTAMPTZ NOT NULL,
    sent_at        TIMESTAMPTZ,
    PRIMARY KEY (event_id, offset_minutes)
);

CREATE INDEX event_reminders_pending
    ON event_reminders (remind_at)
    WHERE sent_at IS NULL;
