-- Game rules, recorded play time, and per-player scoring metadata.
-- Existing reports deliberately retain NULL score/time fields: their detail
-- was not recorded at the time and must not be invented during migration.

ALTER TABLE games
    ADD COLUMN game_type TEXT NOT NULL DEFAULT 'normal'
        CHECK (game_type IN ('normal', 'seafarers', 'cities_knights',
                            'seafarers_cities_knights')),
    ADD COLUMN extension_5_6 BOOLEAN NOT NULL DEFAULT false,
    ADD COLUMN scenario TEXT CHECK (char_length(scenario) BETWEEN 1 AND 100),
    ADD COLUMN target_points SMALLINT CHECK (target_points BETWEEN 1 AND 99),
    ADD COLUMN played_at TIMESTAMPTZ,
    ADD COLUMN played_timezone TEXT CHECK (char_length(played_timezone) BETWEEN 1 AND 64),
    ADD CONSTRAINT games_played_time_requires_timezone
        CHECK ((played_at IS NULL) = (played_timezone IS NULL));

ALTER TABLE game_participants
    ADD COLUMN total_points SMALLINT CHECK (total_points BETWEEN 0 AND 99),
    ADD COLUMN score_breakdown JSONB
        CHECK (score_breakdown IS NULL OR jsonb_typeof(score_breakdown) = 'object'),
    ADD CONSTRAINT game_participants_scores_paired
        CHECK ((total_points IS NULL) = (score_breakdown IS NULL));

-- This deliberately agrees with the repository history order.  Within a
-- date, unknown legacy times come after known play times, then game_id makes
-- ties stable without renumbering historical reports.
CREATE INDEX games_guild_played_order
    ON games (guild_id, played_on DESC, played_at DESC NULLS LAST, game_id DESC);
