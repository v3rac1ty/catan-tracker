"""Regression tests for Discord-safe text and embed limits."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, time
from fractions import Fraction

from catan_bot.db.models import Game, GameWithParticipants, GuildConfig, Season
from catan_bot.domain.ranking import PlayerMovement, RankedPlayer
from catan_bot.domain.scoring import GameRules, PlayerScore, ScoreEntry, score_sources
from catan_bot.formatting import (
    EMBED_DESCRIPTION_MAX,
    EMBED_FIELD_NAME_MAX,
    EMBED_FIELD_VALUE_MAX,
    EMBED_MAX_FIELDS,
    EMBED_TITLE_MAX,
    EMBED_TOTAL_MAX,
    build_config_show_embed,
    build_game_history_embed,
    build_game_report_embed,
    build_game_status_embed,
    build_leaderboard_embed,
    build_leaderboard_post_embed,
    build_season_history_embed,
    escape_user_text,
    format_game_score_table,
)
from catan_bot.services.results import (
    Leaderboard,
    LeaderboardPost,
    PlayerScoreState,
    ScoreCollectionStatus,
)


def _game(game_id: int, reason: str) -> Game:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    return Game(
        game_id=game_id,
        guild_id=1,
        season_id=None,
        played_on=date(2026, 1, 1),
        status="voided",
        reported_by=10,
        confirmed_by=None,
        confirmed_at=None,
        voided_by=11,
        voided_at=now,
        void_reason=reason,
        rejected_by=None,
        rejected_at=None,
        channel_id=None,
        message_id=None,
        created_at=now,
    )


def _season(season_id: int, name: str) -> Season:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    return Season(
        season_id=season_id,
        guild_id=1,
        name=name,
        starts_on=date(2026, 1, 1),
        ends_on=date(2026, 1, 31),
        ends_at=now,
        min_games=2,
        status="completed",
        resolved_at=now,
        announced_at=None,
        created_by=10,
        created_at=now,
    )


def test_escape_user_text_escapes_markup_and_all_discord_mention_forms() -> None:
    result = escape_user_text("**winner** @everyone @12345678901234567 <#12345678901234567>")

    separator = chr(0x200B)
    assert result == (
        r"\*\*winner\*\* @"
        + separator
        + "everyone @"
        + separator
        + "12345678901234567 <#"
        + separator
        + "12345678901234567>"
    )


def test_escape_user_text_strips_default_ignorables_controls_and_fillers() -> None:
    hidden = "".join(
        chr(code)
        for code in (
            0x0000,
            0x034F,
            0x061C,
            0x115F,
            0x17B4,
            0x180B,
            0x200B,
            0x202E,
            0x2065,
            0x2800,
            0x3164,
            0xFEFF,
            0xFFA0,
            0x1BCA0,
            0x1D173,
            0xE0001,
        )
    )

    assert escape_user_text("left" + hidden + "right") == "leftright"


def test_escape_user_text_keeps_contextual_emoji_and_script_shaping() -> None:
    joiner = chr(0x200D)
    non_joiner = chr(0x200C)
    variation = chr(0xFE0F)
    woman = chr(0x1F469)
    heart = chr(0x2764)

    emoji = woman + joiner + heart + variation + joiner + woman
    persian = "می" + non_joiner + "روم"
    assert escape_user_text(emoji) == emoji
    assert escape_user_text(persian) == persian

    assert escape_user_text("a" + joiner + "b") == "ab"
    assert escape_user_text("A" + variation) == "A"


def test_escape_user_text_caps_a_combining_mark_flood() -> None:
    acute = chr(0x0301)
    assert escape_user_text("a" + acute * 20 + "b") == "a" + acute * 4 + "b"


def test_history_with_25_malicious_rows_stays_within_every_embed_limit() -> None:
    payload = ("@everyone **hidden** " + chr(0x200B)) * 10
    embed = build_game_history_embed(
        [_game(game_id, payload) for game_id in range(1, 26)], member_id=None
    )

    assert len(embed) <= EMBED_TOTAL_MAX
    assert len(embed.fields) == EMBED_MAX_FIELDS
    assert all(0 < len(field.name) <= EMBED_FIELD_NAME_MAX for field in embed.fields)
    assert all(0 < len(field.value) <= EMBED_FIELD_VALUE_MAX for field in embed.fields)
    rendered = "".join(field.value for field in embed.fields)
    assert "@everyone" not in rendered
    assert r"\*\*hidden\*\*" in rendered


def test_long_stored_season_names_are_escaped_and_bounded() -> None:
    malicious_name = "@everyone **season** " * 400
    embed = build_season_history_embed(
        [_season(season_id, malicious_name) for season_id in range(1, 26)]
    )

    assert len(embed) <= EMBED_TOTAL_MAX
    assert len(embed.title or "") <= EMBED_TITLE_MAX
    assert len(embed.description or "") <= EMBED_DESCRIPTION_MAX
    assert len(embed.fields) == EMBED_MAX_FIELDS
    assert all(len(field.name) <= EMBED_FIELD_NAME_MAX for field in embed.fields)
    assert all(len(field.value) <= EMBED_FIELD_VALUE_MAX for field in embed.fields)
    assert all("@everyone" not in field.name for field in embed.fields)


def test_config_show_escapes_stored_timezone_text() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    config = GuildConfig(
        guild_id=1,
        timezone="@everyone **UTC**" + chr(0x200B),
        announce_channel_id=None,
        admin_role_id=None,
        default_min_games=2,
        created_at=now,
        updated_at=now,
    )

    embed = build_config_show_embed(config)

    timezone_field = next(field for field in embed.fields if field.name == "Timezone")
    assert "@everyone" not in timezone_field.value
    assert r"\*\*UTC\*\*" in timezone_field.value


def _detailed_game(*, game_type: str = "normal", scenario: str | None = None) -> Game:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    return Game(
        game_id=99,
        guild_id=1,
        season_id=None,
        played_on=date(2026, 1, 1),
        status="confirmed",
        reported_by=10,
        confirmed_by=11,
        confirmed_at=now,
        voided_by=None,
        voided_at=None,
        void_reason=None,
        rejected_by=None,
        rejected_at=None,
        channel_id=None,
        message_id=None,
        created_at=now,
        game_type=game_type,
        extension_5_6=True,
        scenario=scenario,
        target_points=13,
        played_at=datetime(2026, 1, 1, 19, 30, tzinfo=UTC),
        played_timezone="America/Chicago",
    )


def _zero_score(user_id: int, rules: GameRules) -> PlayerScore:
    entries = tuple(ScoreEntry(source.key, 0) for source in score_sources(rules))
    return PlayerScore(user_id=user_id, total_points=0, breakdown=entries)


def test_game_score_table_distinguishes_unrecorded_from_explicit_zero() -> None:
    game = _detailed_game()
    absent = GameWithParticipants(game=game, winner_id=10, loser_ids=(20,))
    assert format_game_score_table(absent) == "Points not recorded"

    rules = GameRules("normal", extension_5_6=False, target_points=10)
    recorded = GameWithParticipants(
        game=game,
        winner_id=10,
        loser_ids=(20,),
        scores=(_zero_score(10, rules), _zero_score(20, rules)),
    )
    table = format_game_score_table(recorded)
    assert "P1 <@10>" in table
    assert "P2 <@20>" in table
    assert "Total" in table
    assert "  0" in table
    assert "—" not in table
    table_lines = table.splitlines()
    header, first_source, total = table_lines[2], table_lines[3], table_lines[-2]
    assert header.index("|  P1") == first_source.index("|   0")
    assert header.index("|  P2") == first_source.index("|   0", first_source.index("|   0") + 1)
    assert header.index("|  P1") == total.index("|   0")


def test_six_player_combined_score_table_stays_compact_and_uses_player_labels() -> None:
    game = _detailed_game(
        game_type="seafarers_cities_knights", scenario="@everyone **Hidden Scenario**"
    )
    rules = GameRules(
        "seafarers_cities_knights",
        extension_5_6=True,
        scenario="@everyone **Hidden Scenario**",
        target_points=15,
    )
    player_ids = (10, 20, 30, 40, 50, 60)
    report = GameWithParticipants(
        game=game,
        winner_id=player_ids[0],
        loser_ids=player_ids[1:],
        scores=tuple(_zero_score(user_id, rules) for user_id in player_ids),
    )
    table = format_game_score_table(report)
    assert all(f"P{index}" in table for index in range(1, 7))
    assert "<@60>" in table  # only compact P labels widen the score columns
    assert len(table) < EMBED_FIELD_VALUE_MAX
    table_lines = table.splitlines()
    header, first_source = table_lines[2], table_lines[3]
    value_start = 0
    for player_index in range(1, 7):
        header_marker = f"|  P{player_index}"
        value_marker = "|   0"
        value_index = first_source.index(value_marker, value_start)
        assert header.index(header_marker) == value_index
        value_start = value_index + 1
    embed = build_game_status_embed(report)
    assert len(embed) <= EMBED_TOTAL_MAX
    assert any(
        field.name == "Game type" and "Seafarers + Cities & Knights" in field.value
        for field in embed.fields
    )
    assert any(field.name == "Extension" for field in embed.fields)
    assert any(field.name == "Date" and "13:30" in field.value for field in embed.fields)
    assert any(
        field.name == "Scenario"
        and "@everyone" not in field.value
        and r"\*\*Hidden Scenario\*\*" in field.value
        for field in embed.fields
    )


def test_game_report_and_status_preserve_score_details_and_legacy_time_message() -> None:
    game = _detailed_game(scenario="Scenario")
    report = GameWithParticipants(game=game, winner_id=10, loser_ids=(20,))
    pending = build_game_report_embed(report)
    assert any(
        field.name == "Point breakdown" and "Points not recorded" in field.value
        for field in pending.fields
    )

    legacy = _game(7, "reason")
    status = build_game_status_embed(
        GameWithParticipants(game=legacy, winner_id=10, loser_ids=(20,))
    )
    assert any(
        field.name == "Date" and "Time not recorded" in field.value for field in status.fields
    )
    assert not any(field.name == "Revision" for field in status.fields)


def test_score_table_marks_unsubmitted_players_with_an_em_dash() -> None:
    """A partial score sheet (Phase 2 collection still in progress) must
    never invent a value for a participant who hasn't submitted yet --
    `format_game_score_table` already handles this via the same NULL-aware
    lookups it uses for a legacy report's missing rows."""
    game = _detailed_game()
    rules = GameRules("normal", extension_5_6=False, target_points=10)
    partial = GameWithParticipants(
        game=game, winner_id=10, loser_ids=(20,), scores=(_zero_score(10, rules),)
    )

    table = format_game_score_table(partial)

    total_line = table.splitlines()[-2]
    assert "Total" in total_line
    assert total_line.count("—") == 1


def test_score_collection_field_shows_progress_icons_for_each_state() -> None:
    game = _detailed_game()
    report = GameWithParticipants(game=game, winner_id=10, loser_ids=(20, 30))
    status = ScoreCollectionStatus(
        game=report,
        requests=(
            PlayerScoreState(user_id=10, delivery_status="delivered", submitted=True),
            PlayerScoreState(user_id=20, delivery_status="blocked", submitted=False),
            PlayerScoreState(user_id=30, delivery_status="pending", submitted=False),
        ),
    )

    embed = build_game_report_embed(report, collection=status)

    field = next(f for f in embed.fields if f.name == "Score entry")
    assert "1 of 3 received" in field.value
    assert "✅ <@10>" in field.value
    assert "🚫 <@20>" in field.value
    assert "/game scores" in field.value
    assert "⏳ <@30>" in field.value


def test_score_collection_field_shows_completion_state() -> None:
    game = _detailed_game()
    report = GameWithParticipants(game=game, winner_id=10, loser_ids=(20,))
    status = ScoreCollectionStatus(
        game=report,
        requests=(
            PlayerScoreState(user_id=10, delivery_status="delivered", submitted=True),
            PlayerScoreState(user_id=20, delivery_status="delivered", submitted=True),
        ),
    )

    embed = build_game_status_embed(report, collection=status)

    field = next(f for f in embed.fields if f.name == "Score entry")
    assert "All scores received (2/2)" in field.value


def test_score_collection_field_is_omitted_without_a_status() -> None:
    game = _detailed_game()
    report = GameWithParticipants(game=game, winner_id=10, loser_ids=(20,))

    embed = build_game_report_embed(report)

    assert not any(field.name == "Score entry" for field in embed.fields)


def test_revised_game_status_shows_safe_audit_metadata() -> None:
    game = replace(
        _detailed_game(),
        revision=2,
        updated_by=77,
        updated_at=datetime(2026, 1, 2, 1, 30, tzinfo=UTC),
        update_reason="@everyone **corrected** <@&12345678901234567>",
    )
    embed = build_game_status_embed(GameWithParticipants(game=game, winner_id=10, loser_ids=(20,)))

    fields = {field.name: field.value for field in embed.fields}
    assert fields["Revision"] == "2"
    assert fields["Updated by"] == "<@77>"
    assert fields["Updated at"] == "<t:1767317400:f>"
    assert "@everyone" not in fields["Update reason"]
    assert "<@&12345678901234567>" not in fields["Update reason"]
    assert r"\*\*corrected\*\*" in fields["Update reason"]


def test_revised_game_history_is_succinct_and_mentions_revision() -> None:
    game = replace(_game(8, "unused"), revision=3, updated_by=77)
    embed = build_game_history_embed([game], member_id=None)

    assert "Updated r3 by <@77>" in embed.fields[0].value


# ---------------------------------------------------------------------------
# build_leaderboard_post_embed (Phase 3: the recurring leaderboard post).
# ---------------------------------------------------------------------------


def _ranked(user_id: int, rank: int, *, eligible: bool = True) -> RankedPlayer:
    return RankedPlayer(
        user_id=user_id, games=4, wins=2, win_rate=Fraction(1, 2), rank=rank, eligible=eligible
    )


def _board(ranked: list[RankedPlayer], *, min_games: int = 2) -> Leaderboard:
    return Leaderboard(scope="all_time", season=None, min_games=min_games, ranked=ranked)


def test_build_leaderboard_post_embed_reuses_leaderboard_embed_standings() -> None:
    """Same ranked list, same standings text either way -- proves the
    recurring-post embed shares its rendering with `build_leaderboard_embed`
    rather than duplicating it."""
    board = _board([_ranked(10, 1), _ranked(20, 2)])
    plain = build_leaderboard_embed(board)
    post = LeaderboardPost(guild_id=1, channel_id=700, board=board, movements=())

    embed = build_leaderboard_post_embed(post)

    plain_standings = next(f.value for f in plain.fields if f.name == "Standings")
    post_standings = next(f.value for f in embed.fields if f.name == "Standings")
    assert plain_standings in post_standings  # post text is a superset (movement suffix added)
    assert "Update" in (embed.title or "")


def test_build_leaderboard_post_embed_shows_movement_arrows() -> None:
    board = _board([_ranked(10, 1), _ranked(20, 2), _ranked(30, 3)])
    movements = (
        PlayerMovement(user_id=10, direction="up", change=2),
        PlayerMovement(user_id=20, direction="down", change=1),
        PlayerMovement(user_id=30, direction="new", change=0),
    )
    post = LeaderboardPost(guild_id=1, channel_id=700, board=board, movements=movements)

    embed = build_leaderboard_post_embed(post)

    standings = next(f.value for f in embed.fields if f.name == "Standings")
    lines = {line.split(" -- ")[0]: line for line in standings.splitlines()}
    assert "\U0001f53c2" in standings  # player 10: up 2
    assert "\U0001f53d1" in standings  # player 20: down 1
    assert "\U0001f195" in standings  # player 30: new
    assert lines  # sanity: the split above actually matched lines


def test_build_leaderboard_post_embed_unchanged_player_gets_a_plain_dash() -> None:
    board = _board([_ranked(10, 1)])
    movements = (PlayerMovement(user_id=10, direction="unchanged", change=0),)
    post = LeaderboardPost(guild_id=1, channel_id=700, board=board, movements=movements)

    embed = build_leaderboard_post_embed(post)

    standings = next(f.value for f in embed.fields if f.name == "Standings")
    assert standings.rstrip().endswith("-")


def test_build_leaderboard_post_embed_leads_with_todays_results() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    game = Game(
        game_id=5,
        guild_id=1,
        season_id=None,
        played_on=date(2026, 1, 1),
        status="confirmed",
        reported_by=10,
        confirmed_by=20,
        confirmed_at=now,
        voided_by=None,
        voided_at=None,
        void_reason=None,
        rejected_by=None,
        rejected_at=None,
        channel_id=None,
        message_id=None,
        created_at=now,
    )
    played = GameWithParticipants(game=game, winner_id=10, loser_ids=(20, 30))
    board = _board([_ranked(10, 1)])
    post = LeaderboardPost(guild_id=1, channel_id=700, board=board, movements=(), games=(played,))

    embed = build_leaderboard_post_embed(post)

    assert embed.fields[0].name == "Today's Results"
    assert "<@10> beat <@20>, <@30>" in embed.fields[0].value


def test_build_leaderboard_post_embed_omits_todays_results_when_no_games() -> None:
    board = _board([_ranked(10, 1)])
    post = LeaderboardPost(guild_id=1, channel_id=700, board=board, movements=())

    embed = build_leaderboard_post_embed(post)

    assert not any(field.name == "Today's Results" for field in embed.fields)


def test_build_leaderboard_post_embed_stays_within_embed_limits_with_many_games() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)

    def _played(game_id: int) -> GameWithParticipants:
        game = Game(
            game_id=game_id,
            guild_id=1,
            season_id=None,
            played_on=date(2026, 1, 1),
            status="confirmed",
            reported_by=10,
            confirmed_by=20,
            confirmed_at=now,
            voided_by=None,
            voided_at=None,
            void_reason=None,
            rejected_by=None,
            rejected_at=None,
            channel_id=None,
            message_id=None,
            created_at=now,
        )
        return GameWithParticipants(game=game, winner_id=10, loser_ids=(20,))

    board = _board([_ranked(uid, rank) for rank, uid in enumerate(range(1, 26), start=1)])
    movements = tuple(PlayerMovement(user_id=uid, direction="up", change=1) for uid in range(1, 26))
    games = tuple(_played(game_id) for game_id in range(1, 51))
    post = LeaderboardPost(
        guild_id=1, channel_id=700, board=board, movements=movements, games=games
    )

    embed = build_leaderboard_post_embed(post)

    assert len(embed) <= EMBED_TOTAL_MAX
    assert len(embed.fields) <= EMBED_MAX_FIELDS
    assert all(0 < len(field.value) <= EMBED_FIELD_VALUE_MAX for field in embed.fields)


# ---------------------------------------------------------------------------
# build_config_show_embed: recurring leaderboard settings (Phase 3).
# ---------------------------------------------------------------------------


def test_config_show_embed_includes_leaderboard_settings() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    config = GuildConfig(
        guild_id=1,
        timezone="UTC",
        announce_channel_id=None,
        admin_role_id=None,
        default_min_games=2,
        created_at=now,
        updated_at=now,
        leaderboard_mode="daily",
        leaderboard_channel_id=555,
        leaderboard_scope="all_time",
        leaderboard_daily_time=time(9, 30),
    )

    embed = build_config_show_embed(config)

    fields = {field.name: field.value for field in embed.fields}
    assert fields["Leaderboard post"] == "Daily digest"
    assert fields["Leaderboard channel"] == "<#555>"
    assert fields["Leaderboard scope"] == "All-time"
    assert fields["Leaderboard daily time"] == "09:30"


def test_config_show_embed_leaderboard_defaults_when_unconfigured() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    config = GuildConfig(
        guild_id=1,
        timezone="UTC",
        announce_channel_id=None,
        admin_role_id=None,
        default_min_games=2,
        created_at=now,
        updated_at=now,
    )

    embed = build_config_show_embed(config)

    fields = {field.name: field.value for field in embed.fields}
    assert fields["Leaderboard post"] == "Off"
    assert fields["Leaderboard channel"] == "Not set"
    assert fields["Leaderboard scope"] == "Season"
    assert fields["Leaderboard daily time"] == "22:00"
