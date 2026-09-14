-- Catan Tracker initial schema.
-- Applied by catan_migrator via src/catan_bot/db/migrate.py.

CREATE TABLE guild_config (
    guild_id            BIGINT PRIMARY KEY,
    timezone            TEXT NOT NULL DEFAULT 'UTC',
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
    CHECK (ends_on >= starts_on)
);

-- Only one active season per guild at a time.
CREATE UNIQUE INDEX seasons_one_active_per_guild
    ON seasons (guild_id)
    WHERE status = 'active';

CREATE TABLE season_results (
    season_id BIGINT NOT NULL REFERENCES seasons (season_id),
    user_id   BIGINT NOT NULL,
    rank      INT NOT NULL,
    games     INT NOT NULL,
    wins      INT NOT NULL,
    eligible  BOOLEAN NOT NULL,
    outcome   TEXT CHECK (outcome IN ('payer', 'payee')),
    PRIMARY KEY (season_id, user_id)
);

CREATE TABLE games (
    game_id      BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    guild_id     BIGINT NOT NULL REFERENCES guild_config (guild_id),
    season_id    BIGINT REFERENCES seasons (season_id),
    played_on    DATE NOT NULL,
    status       TEXT NOT NULL DEFAULT 'pending'
                     CHECK (status IN ('pending', 'confirmed', 'rejected', 'voided')),
    reported_by  BIGINT NOT NULL,
    confirmed_by BIGINT,
    confirmed_at TIMESTAMPTZ,
    voided_by    BIGINT,
    void_reason  TEXT CHECK (char_length(void_reason) <= 200),
    channel_id   BIGINT,
    message_id   BIGINT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (confirmed_by IS NULL OR confirmed_by <> reported_by)
);

CREATE TABLE game_participants (
    game_id   BIGINT NOT NULL REFERENCES games (game_id),
    user_id   BIGINT NOT NULL,
    guild_id  BIGINT NOT NULL,
    is_winner BOOLEAN NOT NULL DEFAULT false,
    PRIMARY KEY (game_id, user_id),
    FOREIGN KEY (guild_id, user_id) REFERENCES players (guild_id, user_id)
);

-- At most one winner per game. Winner + losers are inserted in a single
-- transaction, which guarantees exactly one winner for confirmed games.
CREATE UNIQUE INDEX game_participants_one_winner_per_game
    ON game_participants (game_id)
    WHERE is_winner;

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
