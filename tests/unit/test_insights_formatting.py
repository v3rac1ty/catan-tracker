"""Tests for the `/insights` embed builders (player, meta, head-to-head)."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from fractions import Fraction

import discord

from catan_bot.db.models import Season
from catan_bot.domain.analytics import (
    BOARD_LEADER_MIN,
    CLOSE_FINISH_MIN,
    FAIR_SHARE_MIN,
    INGREDIENTS_MIN,
    LEAD_SOURCES_MIN,
    OVERSHOOT_MIN,
    AwardStat,
    MatchupHighlights,
    MetaSummary,
    PlayerSummary,
    RecordSplit,
    meta_summary,
    player_summaries,
    player_summary,
)
from catan_bot.domain.participation import ParticipationRecord
from catan_bot.formatting import (
    EMBED_FIELD_NAME_MAX,
    EMBED_FIELD_VALUE_MAX,
    EMBED_MAX_FIELDS,
    EMBED_TOTAL_MAX,
    _signed_lead,
    build_head_to_head_embed,
    build_meta_insights_embed,
    build_player_insights_embed,
)
from catan_bot.services.results import (
    HeadToHeadView,
    InsightsFilter,
    MetaInsightsView,
    OpponentRecord,
    PlayerInsightsView,
)

ALL_TIME = InsightsFilter(scope="all_time", season=None, game_type=None)
NO_SEASON = InsightsFilter(scope="season", season=None, game_type=None)
NORMAL_SOURCES = ("settlements", "cities", "longest_road", "largest_army", "vp_cards")


def _season(name: str) -> Season:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    return Season(
        season_id=1,
        guild_id=1,
        name=name,
        starts_on=date(2026, 1, 1),
        ends_on=date(2026, 3, 31),
        ends_at=now,
        min_games=2,
        status="active",
        resolved_at=None,
        announced_at=None,
        created_by=10,
        created_at=now,
    )


def _row(
    game_id: int,
    user_id: int,
    *,
    winner: bool,
    breakdown: dict[str, int] | None,
    game_type: str = "normal",
    player_count: int = 3,
    played_on: date = date(2026, 1, 3),  # a Saturday
) -> ParticipationRecord:
    return ParticipationRecord(
        game_id=game_id,
        played_on=played_on,
        played_at=None,
        game_type=game_type,
        extension_5_6=False,
        target_points=10,
        player_count=player_count,
        user_id=user_id,
        is_winner=winner,
        total_points=None if breakdown is None else sum(breakdown.values()),
        breakdown=breakdown,
    )


def _normal(settlements: int, cities: int, road: int = 0, army: int = 0, vp: int = 0) -> dict:
    return {
        "settlements": settlements,
        "cities": cities,
        "longest_road": road,
        "largest_army": army,
        "vp_cards": vp,
    }


def _records() -> list[ParticipationRecord]:
    """Five 3-player Normal games (one unscored) and a 4-player Cities & Knights game."""
    day = date(2026, 1, 3)
    rows: list[ParticipationRecord] = []
    # Game 1: 101 wins with road; 102 has army.
    rows += [
        _row(1, 101, winner=True, breakdown=_normal(4, 4, road=2), played_on=day),
        _row(1, 102, winner=False, breakdown=_normal(3, 4, army=2), played_on=day),
        _row(1, 103, winner=False, breakdown=_normal(2, 2), played_on=day),
    ]
    # Game 2: 102 wins with road + army + a VP card.
    day2 = day + timedelta(days=7)
    rows += [
        _row(2, 101, winner=False, breakdown=_normal(4, 2, vp=1), played_on=day2),
        _row(2, 102, winner=True, breakdown=_normal(2, 4, road=2, army=2, vp=1), played_on=day2),
        _row(2, 103, winner=False, breakdown=_normal(3, 2), played_on=day2),
    ]
    # Game 3: 101 wins narrowly.
    day3 = day + timedelta(days=14)
    rows += [
        _row(3, 101, winner=True, breakdown=_normal(5, 4, army=2), played_on=day3),
        _row(3, 102, winner=False, breakdown=_normal(4, 4, road=2), played_on=day3),
        _row(3, 103, winner=False, breakdown=_normal(3, 2), played_on=day3),
    ]
    # Game 4: nobody recorded scores.
    day4 = day + timedelta(days=21)
    rows += [
        _row(4, 101, winner=False, breakdown=None, played_on=day4),
        _row(4, 102, winner=False, breakdown=None, played_on=day4),
        _row(4, 103, winner=True, breakdown=None, played_on=day4),
    ]
    # Game 5: 101 wins again.
    day5 = day + timedelta(days=28)
    rows += [
        _row(5, 101, winner=True, breakdown=_normal(4, 4, road=2), played_on=day5),
        _row(5, 102, winner=False, breakdown=_normal(3, 4), played_on=day5),
        _row(5, 103, winner=False, breakdown=_normal(3, 2, army=2), played_on=day5),
    ]
    # Game 6: Cities & Knights, 4 players.
    ck = {
        "settlements": 3,
        "cities": 4,
        "longest_road": 0,
        "metropolis_bonus": 2,
        "defender_of_catan": 1,
        "merchant": 1,
        "constitution": 0,
        "printer": 0,
    }
    ck_loser = {key: 0 for key in ck} | {"settlements": 3, "cities": 2}
    day6 = day + timedelta(days=35)
    rows += [
        _row(
            6,
            101,
            winner=True,
            breakdown=ck | {"longest_road": 2, "constitution": 1},
            game_type="cities_knights",
            player_count=4,
            played_on=day6,
        ),
        _row(
            6,
            102,
            winner=False,
            breakdown=ck_loser,
            game_type="cities_knights",
            player_count=4,
            played_on=day6,
        ),
        _row(
            6,
            103,
            winner=False,
            breakdown=ck_loser,
            game_type="cities_knights",
            player_count=4,
            played_on=day6,
        ),
        _row(
            6,
            104,
            winner=False,
            breakdown=ck_loser,
            game_type="cities_knights",
            player_count=4,
            played_on=day6,
        ),
    ]
    return rows


def _player_view(
    user_id: int = 101, insights_filter: InsightsFilter = ALL_TIME
) -> PlayerInsightsView:
    return PlayerInsightsView(insights_filter, player_summary(_records(), user_id))


def _meta_view(insights_filter: InsightsFilter = ALL_TIME) -> MetaInsightsView:
    records = _records()
    return MetaInsightsView(insights_filter, meta_summary(records), player_summaries(records))


def _fields(embed: discord.Embed) -> dict[str, str]:
    return {field.name: field.value for field in embed.fields}


def _all_text(embed: discord.Embed) -> str:
    parts = [embed.title or "", embed.description or ""]
    for field in embed.fields:
        parts += [field.name, field.value]
    if embed.footer.text:
        parts.append(embed.footer.text)
    return "\n".join(parts)


def _assert_within_limits(embed: discord.Embed) -> None:
    assert len(embed) <= EMBED_TOTAL_MAX
    assert len(embed.fields) <= EMBED_MAX_FIELDS
    assert all(0 < len(field.name) <= EMBED_FIELD_NAME_MAX for field in embed.fields)
    assert all(0 < len(field.value) <= EMBED_FIELD_VALUE_MAX for field in embed.fields)


def _assert_clean(embed: discord.Embed) -> None:
    """No placeholder leaks and nothing that could ping anyone."""
    text = _all_text(embed)
    assert "None" not in text
    assert "Fraction" not in text
    assert "nan" not in text.lower().replace("finance", "")
    assert "@everyone" not in text
    assert "@here" not in text
    assert "<@&" not in text


def _opponent(opponent_id: int, together: int = 8, wins: int = 5, losses: int = 3):
    return OpponentRecord(opponent_id, together, wins, losses)


# ---------------------------------------------------------------------------
# Titles and filter lines
# ---------------------------------------------------------------------------


def test_player_embed_all_time_title_filter_and_mention() -> None:
    embed = build_player_insights_embed(_player_view())

    assert embed.title == "Player Insights"
    assert embed.description is not None
    assert "<@101>" in embed.description
    assert "All-time" in embed.description
    assert "Game type" not in embed.description


def test_season_name_is_escaped_in_every_embed() -> None:
    nasty = _season("**Fall** @everyone")
    season_filter = InsightsFilter(scope="season", season=nasty, game_type=None)
    embeds = [
        build_player_insights_embed(_player_view(insights_filter=season_filter)),
        build_meta_insights_embed(_meta_view(season_filter)),
        build_head_to_head_embed(HeadToHeadView(season_filter, 101, [_opponent(102)])),
    ]

    for embed in embeds:
        assert embed.description is not None
        assert "Season: " in embed.description
        assert r"\*\*Fall\*\*" in embed.description
        assert "@everyone" not in embed.description
        _assert_clean(embed)


def test_game_type_label_is_shown_when_filtered() -> None:
    filtered = InsightsFilter(scope="all_time", season=None, game_type="cities_knights")

    for embed in (
        build_player_insights_embed(_player_view(insights_filter=filtered)),
        build_meta_insights_embed(_meta_view(filtered)),
        build_head_to_head_embed(HeadToHeadView(filtered, 101, [_opponent(102)])),
    ):
        assert embed.description is not None
        assert "Game type: Cities & Knights" in embed.description


def test_defaulted_game_type_is_marked_and_also_played_is_sorted() -> None:
    filtered = InsightsFilter(
        scope="all_time",
        season=None,
        game_type="normal",
        available_game_types={
            "seafarers_cities_knights": 2,
            "cities_knights": 3,
            "seafarers": 3,
            "normal": 5,
            "unknown_type": 0,
        },
        game_type_defaulted=True,
    )
    embed = build_player_insights_embed(_player_view(insights_filter=filtered))

    assert embed.description is not None
    assert "Game type: Normal (most played)" in embed.description
    assert (
        "Also played: Seafarers (3 games) · Cities & Knights (3 games) · "
        "Seafarers + Cities & Knights (2 games). Pick game_type to see them."
    ) in embed.description


def test_also_played_singular_and_absent_when_no_other_types() -> None:
    filtered = InsightsFilter(
        scope="all_time",
        season=None,
        game_type="normal",
        available_game_types={"normal": 5, "cities_knights": 1},
    )
    embed = build_meta_insights_embed(_meta_view(filtered))

    assert embed.description is not None
    assert (
        "Also played: Cities & Knights (1 game). Pick game_type to see them." in embed.description
    )

    no_alternates = InsightsFilter(
        scope="all_time", season=None, game_type="normal", available_game_types={"normal": 5}
    )
    assert "Also played:" not in (
        build_meta_insights_embed(_meta_view(no_alternates)).description or ""
    )


def test_unknown_game_type_label_is_escaped() -> None:
    filtered = InsightsFilter(scope="all_time", season=None, game_type="**odd**")
    embed = build_meta_insights_embed(_meta_view(filtered))

    assert embed.description is not None
    assert r"\*\*odd\*\*" in embed.description


# ---------------------------------------------------------------------------
# Player embed content
# ---------------------------------------------------------------------------


def test_player_embed_shows_record_streak_and_form() -> None:
    fields = _fields(build_player_insights_embed(_player_view()))

    # 101 played all 6 games (the Cities & Knights one included): wins 1, 3, 5, 6.
    assert fields["Record"].startswith("4-2 (66.7%) in 6 games")
    assert "Streak: W2 (best win streak 2)" in fields["Record"]
    assert "Last 6: 4-2 (66.7%)" in fields["Record"]


def test_player_embed_losing_streak_is_prefixed_with_l() -> None:
    fields = _fields(build_player_insights_embed(_player_view(user_id=104)))

    assert "Streak: L1" in fields["Record"]


def test_score_stats_carry_their_sample_sizes() -> None:
    summary = player_summary(_records(), 101)
    fields = _fields(build_player_insights_embed(PlayerInsightsView(ALL_TIME, summary)))

    points = fields["Points"]
    assert summary.scored_games == 5
    assert f"({summary.scored_games} scored games)" in points
    assert "In wins: " in points and f"({summary.scored_wins} scored games)" in points
    assert f"({summary.scored_losses} scored game)" in points  # singular
    assert "pts" in points
    assert "Of target:" in points and "%" in points
    for line in points.splitlines():
        assert line.endswith(")"), line
    board = fields["Board"]
    assert "Settlements:" in board and "avg (" in board
    margins = fields["Margins"]
    assert "Average win margin:" in margins
    assert "scored game" in margins


def test_player_embed_averages_use_one_decimal() -> None:
    summary = player_summary(_records(), 102)
    fields = _fields(build_player_insights_embed(PlayerInsightsView(ALL_TIME, summary)))

    assert summary.avg_points is not None
    assert f"Average: {float(summary.avg_points):.1f} pts" in fields["Points"]


def test_player_awards_show_held_rate_and_win_rate_split() -> None:
    fields = _fields(build_player_insights_embed(_player_view()))

    awards = fields["Awards"]
    assert "Longest road: held" in awards
    assert "won" in awards and "with it" in awards
    assert "without" in awards
    assert "Largest army:" in awards
    assert "Merchant:" in awards  # the Cities & Knights game contributes awards too


def test_unscored_player_sees_no_scores_recorded_yet() -> None:
    records = [
        _row(1, 1, winner=True, breakdown=None),
        _row(1, 2, winner=False, breakdown=None),
    ]
    embed = build_player_insights_embed(PlayerInsightsView(ALL_TIME, player_summary(records, 1)))
    fields = _fields(embed)

    assert fields["Points"] == "No scores recorded yet."
    assert "Record" in fields
    for skipped in ("Board", "Awards", "Margins"):
        assert skipped not in fields
    _assert_clean(embed)


def test_player_embed_has_split_fields() -> None:
    fields = _fields(build_player_insights_embed(_player_view()))

    assert "3 players: 3-2 (60.0%)" in fields["By player count"]
    assert "4 players: 1-0 (100.0%)" in fields["By player count"]
    assert "By game type" not in fields


def test_player_embed_passes_the_clean_and_limit_checks() -> None:
    for user_id in (101, 102, 103, 104):
        embed = build_player_insights_embed(_player_view(user_id=user_id))
        _assert_clean(embed)
        _assert_within_limits(embed)


# ---------------------------------------------------------------------------
# Meta embed content
# ---------------------------------------------------------------------------


def test_meta_embed_titles_and_sections() -> None:
    embed = build_meta_insights_embed(_meta_view())
    fields = _fields(embed)

    assert embed.title == "Meta Insights"
    assert fields["Games"] == "6 confirmed games (5 with the winner's score)"
    for name in (
        "How winners score",
        "Awards & winning",
        "Play styles",
        "VP cards",
        "Winning scores & margins",
        "Most frequent holders",
        "Calendar",
    ):
        assert name in fields, name
    _assert_clean(embed)
    _assert_within_limits(embed)


def test_meta_how_winners_score_uses_labels_and_sample_sizes() -> None:
    value = _fields(build_meta_insights_embed(_meta_view()))["How winners score"]

    assert "winners vs everyone else" in value
    assert "Houses:" in value
    assert "Longest road:" in value
    assert "Metropolis:" in value
    assert "scored appearances)" in value
    assert "players)" not in value
    assert "settlements" not in value  # labels, not raw keys


def test_meta_winning_scores_and_margins_have_samples() -> None:
    value = _fields(build_meta_insights_embed(_meta_view()))["Winning scores & margins"]

    assert "Average winning score:" in value
    assert "(5 scored games)" in value
    assert "Average margin:" in value
    assert "Most common winning score" in value


def test_meta_most_frequent_holders_name_top_player_per_award() -> None:
    value = _fields(build_meta_insights_embed(_meta_view()))["Most frequent holders"]

    # 101 and 102 each held Longest road in 2 normal games; the lower id wins the tie.
    assert "Longest road: <@101>" in value
    assert "Largest army:" in value


def test_meta_calendar_names_busiest_weekday() -> None:
    value = _fields(build_meta_insights_embed(_meta_view()))["Calendar"]

    assert value == "Busiest day: Saturday (6 games of 6)"


def test_meta_play_styles_and_vp_cards() -> None:
    fields = _fields(build_meta_insights_embed(_meta_view()))

    assert "City-heavy:" in fields["Play styles"]
    assert "win rate" in fields["Play styles"]
    assert "Winners holding VP cards:" in fields["VP cards"]
    assert "of the winning score" in fields["VP cards"]


def test_meta_with_only_loser_scores_does_not_claim_nothing_is_recorded() -> None:
    records = [
        _row(1, 1, winner=True, breakdown=None),
        _row(1, 2, winner=False, breakdown=_normal(3, 2)),
        _row(1, 3, winner=False, breakdown=_normal(2, 2)),
    ]
    view = MetaInsightsView(ALL_TIME, meta_summary(records), player_summaries(records))
    embed = build_meta_insights_embed(view)
    fields = _fields(embed)

    assert fields["Games"] == "1 confirmed game (0 with the winner's score)"
    assert "No scores recorded yet." not in _all_text(embed)
    assert "How winners score" in fields
    assert "no data" in fields["How winners score"]  # winners have no sample
    assert "Winning scores & margins" not in fields
    _assert_clean(embed)


def test_meta_awards_and_play_styles_count_appearances_not_players() -> None:
    fields = _fields(build_meta_insights_embed(_meta_view()))

    assert (
        "holders won 80.0% (5 appearances) vs 9.1% without (11 appearances)"
        in (fields["Awards & winning"])
    )
    assert "(4 scored appearances)" in fields["Play styles"]
    assert "player" not in fields["Play styles"]
    assert "player" not in fields["Awards & winning"]


def test_meta_with_no_scores_says_so_and_skips_score_fields() -> None:
    records = [
        _row(1, 1, winner=True, breakdown=None),
        _row(1, 2, winner=False, breakdown=None),
    ]
    view = MetaInsightsView(ALL_TIME, meta_summary(records), player_summaries(records))
    embed = build_meta_insights_embed(view)
    fields = _fields(embed)

    assert fields["Games"] == (
        "1 confirmed game (0 with the winner's score)\nNo scores recorded yet."
    )
    assert "How winners score" not in fields
    assert "Winning scores & margins" not in fields
    assert "Most frequent holders" not in fields
    _assert_clean(embed)


# ---------------------------------------------------------------------------
# Empty states
# ---------------------------------------------------------------------------


def test_season_scope_without_active_season_uses_no_active_season_description() -> None:
    empty_meta = meta_summary([])
    embeds = [
        build_player_insights_embed(PlayerInsightsView(NO_SEASON, player_summary([], 101))),
        build_meta_insights_embed(MetaInsightsView(NO_SEASON, empty_meta, [])),
        build_head_to_head_embed(HeadToHeadView(NO_SEASON, 101, [])),
    ]

    for embed in embeds:
        assert embed.description == "There's no active season."
        assert not embed.fields
        assert embed.title


def test_no_games_description_respects_filters() -> None:
    filtered = InsightsFilter(
        scope="all_time",
        season=None,
        game_type="seafarers",
        available_game_types={"normal": 2, "seafarers": 0},
    )
    embeds = [
        build_player_insights_embed(PlayerInsightsView(filtered, player_summary([], 101))),
        build_meta_insights_embed(MetaInsightsView(filtered, meta_summary([]), [])),
        build_head_to_head_embed(HeadToHeadView(filtered, 101, [])),
    ]

    for embed in embeds:
        assert embed.description is not None
        assert "No confirmed games yet." in embed.description
        assert "Game type: Seafarers" in embed.description
        assert "Also played: Normal (2 games). Pick game_type to see them." in embed.description
        assert not embed.fields
        _assert_clean(embed)


def test_no_games_in_an_active_season_names_the_season() -> None:
    season_filter = InsightsFilter(scope="season", season=_season("Winter"), game_type=None)
    embed = build_meta_insights_embed(MetaInsightsView(season_filter, meta_summary([]), []))

    assert embed.description is not None
    assert "Season: Winter" in embed.description
    assert "No confirmed games yet." in embed.description


# ---------------------------------------------------------------------------
# Head-to-head
# ---------------------------------------------------------------------------


def test_head_to_head_rows_format_and_order() -> None:
    opponents = [
        OpponentRecord(300, 8, 5, 3),
        OpponentRecord(200, 5, 1, 4),
        OpponentRecord(100, 1, 1, 0),
    ]
    embed = build_head_to_head_embed(HeadToHeadView(ALL_TIME, 101, opponents))
    lines = embed.fields[0].value.splitlines()

    assert embed.title == "Head-to-Head"
    assert embed.description is not None and "<@101>" in embed.description
    assert embed.fields[0].name == "Opponents"
    assert lines == [
        "<@300> — 8 games: won 5, they won 3, others won 0 (62.5%)",
        "<@200> — 5 games: won 1, they won 4, others won 0 (20.0%)",
        "<@100> — 1 game: won 1, they won 0, others won 0 (100.0%)",
    ]
    _assert_clean(embed)


def test_head_to_head_rate_counts_games_won_by_third_parties() -> None:
    embed = build_head_to_head_embed(HeadToHeadView(ALL_TIME, 101, [OpponentRecord(2, 10, 3, 2)]))

    assert embed.fields[0].value == "<@2> — 10 games: won 3, they won 2, others won 5 (30.0%)"


def test_head_to_head_with_40_opponents_keeps_all_rows_in_order() -> None:
    opponents = [OpponentRecord(1_000_000_000_000_000_000 + i, 50 - i, 20, 15) for i in range(40)]
    embed = build_head_to_head_embed(HeadToHeadView(ALL_TIME, 101, opponents))

    _assert_within_limits(embed)
    _assert_clean(embed)
    rendered = [line for field in embed.fields for line in field.value.splitlines()]
    assert [line.split(" ")[0] for line in rendered] == [f"<@{o.opponent_id}>" for o in opponents]
    assert embed.footer.text is None


def test_head_to_head_with_hundreds_of_opponents_stays_within_limits() -> None:
    opponents = [OpponentRecord(1_000_000_000_000_000_000 + i, 9, 5, 3) for i in range(400)]
    embed = build_head_to_head_embed(HeadToHeadView(ALL_TIME, 101, opponents))

    _assert_within_limits(embed)
    _assert_clean(embed)
    rendered = [line for field in embed.fields for line in field.value.splitlines()]
    assert 0 < len(rendered) < 400
    assert embed.footer.text == f"Showing {len(rendered)} of 400 opponents."
    # What is shown is a prefix of the given order, never a reshuffle.
    assert rendered[0].startswith(f"<@{opponents[0].opponent_id}>")
    assert rendered[-1].startswith(f"<@{opponents[len(rendered) - 1].opponent_id}>")


# ---------------------------------------------------------------------------
# Extremes
# ---------------------------------------------------------------------------


def _award_stat(key: str) -> AwardStat:
    return AwardStat(
        key=key,
        opportunities=99,
        held=33,
        held_rate=Fraction(1, 3),
        wins_when_held=20,
        win_rate_when_held=Fraction(20, 33),
        games_without=66,
        wins_without=10,
        win_rate_without=Fraction(10, 66),
    )


def _extreme_summary(user_id: int, award_count: int = 30) -> PlayerSummary:
    base = player_summary(_records(), 101)
    split = RecordSplit(games=7, wins=3, win_rate=Fraction(3, 7))
    awards = {f"award_{index:02d}_{'x' * 40}": _award_stat("k") for index in range(award_count)}
    return PlayerSummary(
        **{
            **{name: getattr(base, name) for name in PlayerSummary.__slots__},
            "user_id": user_id,
            "awards": awards,
            "by_player_count": {count: split for count in range(2, 7)},
            "by_game_type": {f"type_{index}_{'y' * 30}": split for index in range(30)},
        }
    )


def test_player_embed_with_many_awards_and_splits_stays_within_limits() -> None:
    embed = build_player_insights_embed(PlayerInsightsView(ALL_TIME, _extreme_summary(101)))

    _assert_within_limits(embed)
    _assert_clean(embed)
    assert "Record" in _fields(embed)


def test_meta_embed_with_many_sources_and_players_stays_within_limits() -> None:
    meta = meta_summary(_records())
    keys = [f"source_{index:02d}_{'z' * 30}" for index in range(40)]
    meta = MetaSummary(
        **{
            **{name: getattr(meta, name) for name in MetaSummary.__slots__},
            "winner_composition": {key: Fraction(7, 3) for key in keys},
            "loser_composition": {key: Fraction(5, 3) for key in keys},
            "winner_composition_samples": {key: 12 for key in keys},
            "loser_composition_samples": {key: 30 for key in keys},
            "awards": {key: _award_stat(key) for key in keys},
        }
    )
    players = [_extreme_summary(1_000_000_000_000_000_000 + i, award_count=0) for i in range(40)]
    players = [
        PlayerSummary(
            **{
                **{name: getattr(p, name) for name in PlayerSummary.__slots__},
                "awards": {key: _award_stat(key) for key in keys},
            }
        )
        for p in players
    ]
    embed = build_meta_insights_embed(MetaInsightsView(ALL_TIME, meta, players))

    _assert_within_limits(embed)
    _assert_clean(embed)
    assert "Games" in _fields(embed)


def test_hostile_user_text_never_produces_a_ping() -> None:
    hostile = InsightsFilter(
        scope="season",
        season=_season("@everyone @here <@&123456789012345678> <#123456789012345678>"),
        game_type="@everyone",
    )
    embeds = [
        build_player_insights_embed(_player_view(insights_filter=hostile)),
        build_meta_insights_embed(_meta_view(hostile)),
        build_head_to_head_embed(HeadToHeadView(hostile, 101, [_opponent(102)])),
    ]

    for embed in embeds:
        text = _all_text(embed)
        assert "@everyone" not in text
        assert "@here" not in text
        assert "<@&" not in text
        assert "<#1" not in text
        # The only mentions left are plain user mentions built from int ids.
        assert "<@101>" in text or embed.title == "Meta Insights"


# ---------------------------------------------------------------------------
# M5: when you win, rivalries, time-of-day calendar
# ---------------------------------------------------------------------------


def _split(games: int, wins: int) -> RecordSplit:
    return RecordSplit(games, wins, Fraction(wins, games) if games else None)


def _timed_player_view(**changes: object) -> PlayerInsightsView:
    fields: dict[str, object] = {
        "by_time_of_day": {
            "daytime": _split(2, 1),
            "evening": _split(8, 5),
            "late_night": _split(1, 0),
        },
        "timed_games": 11,
        "by_weekday": {0: _split(4, 1), 2: _split(1, 1), 5: _split(6, 5)},
    } | changes
    summary = replace(player_summary(_records(), 101), **fields)
    return PlayerInsightsView(ALL_TIME, summary)


def test_when_you_win_shows_buckets_sample_and_weekday_extremes() -> None:
    embed = build_player_insights_embed(_timed_player_view())
    value = _fields(embed)["When you win"]

    assert value.splitlines() == [
        "Daytime: 1-1 (50.0%)",
        "Evening: 5-3 (62.5%)",
        "Late night: 0-1 (0.0%)",
        "(11 games with a recorded time)",
        # Wednesday has one game, below the two-game minimum.
        "Best day: Saturday 5-1 (83.3%)",
        "Worst day: Monday 1-3 (25.0%)",
        "(weekdays with 2+ games)",
    ]
    _assert_clean(embed)
    _assert_within_limits(embed)


def test_when_you_win_skips_empty_buckets_and_missing_data() -> None:
    plain = _fields(build_player_insights_embed(_player_view()))
    assert "When you win" not in plain  # no time-of-day or weekday data at all

    only_weekdays = replace(
        player_summary(_records(), 101), by_weekday={0: _split(3, 2), 4: _split(2, 0)}
    )
    value = _fields(build_player_insights_embed(PlayerInsightsView(ALL_TIME, only_weekdays)))[
        "When you win"
    ]
    assert "recorded time" not in value
    assert value.startswith("Best day: Monday 2-1 (66.7%)")


def test_when_you_win_omits_weekday_line_without_a_real_comparison() -> None:
    one_day = _timed_player_view(by_weekday={5: _split(6, 5)})
    equal = _timed_player_view(by_weekday={0: _split(2, 1), 5: _split(4, 2)})
    tiny = _timed_player_view(by_weekday={0: _split(1, 1), 5: _split(1, 0)})

    for view in (one_day, equal, tiny):
        value = _fields(build_player_insights_embed(view))["When you win"]
        assert "day:" not in value
        assert value.splitlines()[0] == "Daytime: 1-1 (50.0%)"


def test_when_you_win_singular_sample_wording() -> None:
    view = _timed_player_view(by_time_of_day={"evening": _split(1, 1)}, timed_games=1)

    assert (
        "(1 game with a recorded time)"
        in _fields(build_player_insights_embed(view))["When you win"]
    )


def _h2h_view(highlights: MatchupHighlights | None) -> HeadToHeadView:
    return HeadToHeadView(
        ALL_TIME,
        101,
        [
            OpponentRecord(7, 8, 2, 5),
            OpponentRecord(8, 6, 4, 1),
            OpponentRecord(9, 9, 4, 4),
        ],
        highlights,
    )


def test_rivalries_field_lists_each_present_highlight() -> None:
    embed = build_head_to_head_embed(_h2h_view(MatchupHighlights(7, 8, 9, 3)))
    fields = _fields(embed)

    assert fields["Rivalries"].splitlines() == [
        "Nemesis: <@7> won 5 of 8 shared games",
        "Best matchup: <@8> \u2014 won 4 of 6 shared games",
        "Closest rival: <@9> \u2014 won 4, they won 4 (9 shared games)",
        "(min 3 shared games)",
    ]
    assert list(fields)[0] == "Rivalries"
    assert "Opponents" in fields
    _assert_clean(embed)
    _assert_within_limits(embed)


def test_rivalries_omit_none_lines_and_skip_when_all_none() -> None:
    partial = _fields(build_head_to_head_embed(_h2h_view(MatchupHighlights(None, 8, None, 3))))
    assert partial["Rivalries"].splitlines() == [
        "Best matchup: <@8> \u2014 won 4 of 6 shared games",
        "(min 3 shared games)",
    ]

    for highlights in (None, MatchupHighlights(None, None, None, 3)):
        embed = build_head_to_head_embed(_h2h_view(highlights))
        assert "Rivalries" not in _fields(embed)
        assert "Opponents" in _fields(embed)
        _assert_clean(embed)


def test_rivalries_tolerate_a_highlight_missing_from_opponents() -> None:
    embed = build_head_to_head_embed(
        HeadToHeadView(
            ALL_TIME, 101, [OpponentRecord(7, 8, 2, 5)], MatchupHighlights(99, None, None, 3)
        )
    )

    assert _fields(embed)["Rivalries"].splitlines()[0] == "Nemesis: <@99>"
    _assert_clean(embed)


def test_rivalries_with_many_opponents_still_stay_within_limits() -> None:
    opponents = [OpponentRecord(1_000_000_000_000_000_000 + i, 9, 5, 3) for i in range(400)]
    first = opponents[0].opponent_id
    view = HeadToHeadView(ALL_TIME, 101, opponents, MatchupHighlights(first, first, first, 3))
    embed = build_head_to_head_embed(view)

    _assert_within_limits(embed)
    _assert_clean(embed)
    assert "Rivalries" in _fields(embed)
    assert embed.footer.text is not None and "of 400 opponents" in embed.footer.text


def test_meta_calendar_gains_time_of_day_counts() -> None:
    view = _meta_view()
    meta = replace(view.meta, games_by_time_of_day={"daytime": 2, "evening": 7, "late_night": 0})
    embed = build_meta_insights_embed(replace(view, meta=meta))
    value = _fields(embed)["Calendar"]

    assert "Busiest day: Saturday" in value
    assert (
        "Time of day: Daytime 2 \u2022 Evening 7 \u2022 Late night 0 (9 games with a recorded time)"
        in value
    )
    _assert_clean(embed)


def test_meta_calendar_without_time_data_is_unchanged() -> None:
    assert _fields(build_meta_insights_embed(_meta_view()))["Calendar"] == (
        "Busiest day: Saturday (6 games of 6)"
    )


# ---------------------------------------------------------------------------
# M6: deciding factors, wins vs fair share, winning ingredients
# ---------------------------------------------------------------------------

EXPLORATORY = " — exploratory"


_BLANK_DECIDING = {
    "lead_sources": {},
    "lead_source_samples": 0,
    "board_leader_games": 0,
    "board_leader_wins": 0,
    "board_leader_ties": 0,
    "close_finish_games": 0,
    "close_finish_avg": None,
    "close_finish_distribution": {},
    "overshoot_distribution": {},
    "overshoot_samples": 0,
    "exact_target_rate": None,
}


def _meta_with(**changes: object) -> MetaInsightsView:
    """The fixture meta view with every deciding-factor stat blanked, then `changes`."""
    view = _meta_view()
    return replace(view, meta=replace(view.meta, **(_BLANK_DECIDING | changes)))


def _deciding(**changes: object) -> str | None:
    return _fields(build_meta_insights_embed(_meta_with(**changes))).get("Deciding factors")


def _player_with(**changes: object) -> PlayerInsightsView:
    return PlayerInsightsView(ALL_TIME, replace(player_summary(_records(), 101), **changes))


def _lines(value: str | None) -> list[str]:
    assert value is not None
    return value.split("\n")


def test_deciding_factors_present_with_all_four_lines() -> None:
    value = _deciding(
        lead_sources={
            "cities": Fraction(14, 10),
            "longest_road": Fraction(9, 10),
            "settlements": Fraction(-3, 10),
            "largest_army": Fraction(1, 10),
            "vp_cards": Fraction(5, 10),
        },
        lead_source_samples=24,
        board_leader_games=20,
        board_leader_wins=13,
        board_leader_ties=3,
        close_finish_games=18,
        close_finish_avg=Fraction(7, 6),
        close_finish_distribution={0: 2, 1: 12, 2: 4},
        overshoot_distribution={0: 3, 1: 5, 2: 2},
        overshoot_samples=10,
        exact_target_rate=Fraction(3, 10),
    )

    assert _lines(value) == [
        "Winning lead mostly from: Cities +1.4, Longest road +0.9, VP cards +0.5 "
        "(24 fully scored games)",
        "Board leader won 65.0% (13 of 20 games; 3 ties excluded)",
        "Crowded finishes: avg 1.2 losers finished at target − 2 or higher (18 games)",
        "Winners overshoot the target by 0.9 on average; exact-target wins 30.0% (10 wins)",
    ]


def test_deciding_factors_absent_without_data() -> None:
    embed = build_meta_insights_embed(_meta_with())

    assert "Deciding factors" not in _fields(embed)
    _assert_clean(embed)


def test_deciding_factors_render_from_real_domain_output() -> None:
    embed = build_meta_insights_embed(_meta_view())
    value = _fields(embed)["Deciding factors"]

    assert "Winning lead mostly from:" in value
    assert "fully scored games)" in value
    assert "Board leader won" in value
    assert "Crowded finishes:" in value
    assert "Winners overshoot the target by" in value
    assert EXPLORATORY in value  # the fixture has only five games
    _assert_clean(embed)
    _assert_within_limits(embed)


def test_deciding_factors_skips_only_lines_with_no_data() -> None:
    value = _deciding(board_leader_games=12, board_leader_wins=6, board_leader_ties=0)

    assert _lines(value) == ["Board leader won 50.0% (6 of 12 games)"]


def test_lead_sources_with_no_positive_source_or_no_samples_are_skipped() -> None:
    assert _deciding(lead_sources={"cities": Fraction(-1)}, lead_source_samples=12) is None
    assert _deciding(lead_sources={"cities": Fraction(1)}, lead_source_samples=0) is None


def test_lead_sources_show_at_most_three_with_catalog_order_on_ties() -> None:
    sources = {key: Fraction(1) for key in ("vp_cards", "cities", "settlements", "largest_army")}
    value = _deciding(lead_sources=sources, lead_source_samples=10)

    assert value is not None
    assert value.startswith("Winning lead mostly from: Houses +1.0, Cities +1.0, Largest army +1.0")
    assert "VP cards" not in value


def test_lead_sources_singular_sample_wording() -> None:
    value = _deciding(lead_sources={"cities": Fraction(2)}, lead_source_samples=1)

    assert value == f"Winning lead mostly from: Cities +2.0 (1 fully scored game){EXPLORATORY}"


def test_exploratory_boundary_for_every_meta_line() -> None:
    cases = (
        (
            LEAD_SOURCES_MIN,
            lambda n: {"lead_sources": {"cities": Fraction(1)}, "lead_source_samples": n},
        ),
        (BOARD_LEADER_MIN, lambda n: {"board_leader_games": n, "board_leader_wins": 1}),
        (
            CLOSE_FINISH_MIN,
            lambda n: {"close_finish_games": n, "close_finish_avg": Fraction(1)},
        ),
        (
            OVERSHOOT_MIN,
            lambda n: {
                "overshoot_samples": n,
                "overshoot_distribution": {0: n},
                "exact_target_rate": Fraction(1),
            },
        ),
    )
    for minimum, build in cases:
        below = _lines(_deciding(**build(minimum - 1)))
        at_min = _lines(_deciding(**build(minimum)))
        assert len(below) == len(at_min) == 1
        assert below[0].endswith(EXPLORATORY), below
        assert not at_min[0].endswith(EXPLORATORY), at_min


def test_board_leader_ties_excluded_wording() -> None:
    one = _deciding(board_leader_games=10, board_leader_wins=7, board_leader_ties=1)
    several = _deciding(board_leader_games=10, board_leader_wins=7, board_leader_ties=4)
    single_game = _deciding(board_leader_games=1, board_leader_wins=1, board_leader_ties=0)

    assert one == "Board leader won 70.0% (7 of 10 games; 1 tie excluded)"
    assert several == "Board leader won 70.0% (7 of 10 games; 4 ties excluded)"
    assert single_game == f"Board leader won 100.0% (1 of 1 game){EXPLORATORY}"


def test_board_leader_with_only_ties_is_skipped() -> None:
    assert _deciding(board_leader_games=0, board_leader_wins=0, board_leader_ties=5) is None


def test_overshoot_line_variants() -> None:
    no_rate = _deciding(overshoot_samples=12, overshoot_distribution={1: 6, 3: 6})
    under = _deciding(
        overshoot_samples=10,
        overshoot_distribution={-1: 10},
        exact_target_rate=Fraction(0),
    )

    assert no_rate == "Winners overshoot the target by 2.0 on average (12 wins)"
    assert under == (
        "Winners finish 1.0 under the target on average; exact-target wins 0.0% (10 wins)"
    )


def test_crowded_finishes_needs_a_recorded_average() -> None:
    assert _deciding(close_finish_games=12, close_finish_avg=None) is None
    value = _deciding(close_finish_games=1, close_finish_avg=Fraction(0))
    assert value == (
        f"Crowded finishes: avg 0.0 losers finished at target − 2 or higher (1 game){EXPLORATORY}"
    )


def test_wins_vs_fair_share_line_in_record_field() -> None:
    summary = replace(
        player_summary(_records(), 101),
        wins=7,
        games=FAIR_SHARE_MIN,
        expected_wins=Fraction(26, 5),
        wins_vs_expected=Fraction(35, 26),
    )
    embed = build_player_insights_embed(PlayerInsightsView(ALL_TIME, summary))
    record = _lines(_fields(embed)["Record"])

    assert "Wins vs fair share: 7 vs 5.2 expected (1.35×)" in record
    _assert_clean(embed)


def test_wins_vs_fair_share_exploratory_boundary() -> None:
    def fair_share_line(games: int) -> str:
        summary = replace(
            player_summary(_records(), 101),
            games=games,
            expected_wins=Fraction(2),
            wins_vs_expected=Fraction(3, 2),
        )
        view = PlayerInsightsView(ALL_TIME, summary)
        record = _lines(_fields(build_player_insights_embed(view))["Record"])
        return next(line for line in record if line.startswith("Wins vs fair share"))

    assert fair_share_line(FAIR_SHARE_MIN - 1).endswith(EXPLORATORY)
    assert not fair_share_line(FAIR_SHARE_MIN).endswith(EXPLORATORY)


def test_wins_vs_fair_share_absent_without_expected_wins() -> None:
    view = _player_with(expected_wins=None, wins_vs_expected=None)
    embed = build_player_insights_embed(view)

    assert "fair share" not in _fields(embed)["Record"]
    _assert_clean(embed)


def _ingredient_view(
    ingredients: dict[str, Fraction], wins: int = 8, losses: int = 6
) -> PlayerInsightsView:
    return _player_with(
        winning_ingredients=ingredients,
        ingredient_win_samples=wins,
        ingredient_loss_samples=losses,
    )


def test_winning_ingredients_top_three_positive_with_samples() -> None:
    view = _ingredient_view(
        {
            "cities": Fraction(8, 100),
            "settlements": Fraction(-5, 100),
            "longest_road": Fraction(4, 100),
            "largest_army": Fraction(2, 100),
            "vp_cards": Fraction(1, 100),
        },
        wins=12,
        losses=9,
    )
    embed = build_player_insights_embed(view)

    assert _lines(_fields(embed)["Winning ingredients"]) == [
        "Cities: +8% of target more in wins",
        "Longest road: +4% of target more in wins",
        "Largest army: +2% of target more in wins",
        "(12 wins / 9 losses)",
    ]
    _assert_clean(embed)
    _assert_within_limits(embed)


def test_winning_ingredients_skipped_when_only_negative_or_zero() -> None:
    view = _ingredient_view({"cities": Fraction(-1, 10), "settlements": Fraction(0)})

    assert "Winning ingredients" not in _fields(build_player_insights_embed(view))


def test_winning_ingredients_never_drop_a_small_positive_difference() -> None:
    view = _ingredient_view({"cities": Fraction(4, 1000), "settlements": Fraction(1, 100)})

    assert _lines(_fields(build_player_insights_embed(view))["Winning ingredients"])[:2] == [
        "Houses: +1% of target more in wins",
        "Cities: +<1% of target more in wins",
    ]
    tiny = _ingredient_view({"cities": Fraction(4, 1000)})
    assert _lines(_fields(build_player_insights_embed(tiny))["Winning ingredients"]) == [
        "Cities: +<1% of target more in wins",
        "(8 wins / 6 losses)",
    ]
    half = _ingredient_view({"cities": Fraction(1, 200)})
    assert (
        "Cities: +1% of target" in _fields(build_player_insights_embed(half))["Winning ingredients"]
    )


def test_small_lead_sources_render_as_less_than_a_tenth() -> None:
    value = _deciding(
        lead_sources={
            "cities": Fraction(4, 100),
            "longest_road": Fraction(5, 100),
            "settlements": Fraction(0),
            "largest_army": Fraction(-1, 100),
        },
        lead_source_samples=12,
    )

    assert (
        value == "Winning lead mostly from: Longest road +0.1, Cities +<0.1 (12 fully scored games)"
    )


def test_signed_lead_helper_covers_zero_and_negative_tiny_values() -> None:
    assert _signed_lead(Fraction(0)) == "0.0"
    assert _signed_lead(Fraction(4, 100)) == "+<0.1"
    assert _signed_lead(Fraction(-4, 100)) == "−<0.1"
    assert _signed_lead(Fraction(-3, 10)) == "−0.3"
    assert _signed_lead(Fraction(14, 10)) == "+1.4"


def test_winning_ingredients_need_both_samples() -> None:
    positive = {"cities": Fraction(1, 10)}

    for wins, losses in ((0, 5), (5, 0), (0, 0)):
        embed = build_player_insights_embed(_ingredient_view(positive, wins, losses))
        assert "Winning ingredients" not in _fields(embed)


def test_winning_ingredients_exploratory_boundary_on_either_side() -> None:
    positive = {"cities": Fraction(1, 10)}

    def footer(wins: int, losses: int) -> str:
        view = _ingredient_view(positive, wins, losses)
        return _lines(_fields(build_player_insights_embed(view))["Winning ingredients"])[-1]

    assert footer(INGREDIENTS_MIN - 1, 20).endswith(EXPLORATORY)
    assert footer(20, INGREDIENTS_MIN - 1).endswith(EXPLORATORY)
    assert footer(INGREDIENTS_MIN, INGREDIENTS_MIN) == (
        f"({INGREDIENTS_MIN} wins / {INGREDIENTS_MIN} losses)"
    )
    assert footer(1, 1) == f"(1 win / 1 loss){EXPLORATORY}"


def test_new_fields_never_render_none_and_stay_within_limits() -> None:
    meta_view = _meta_with(
        lead_sources={f"source_{index}_{'q' * 30}": Fraction(index + 1, 3) for index in range(8)},
        lead_source_samples=15,
        board_leader_games=15,
        board_leader_wins=9,
        board_leader_ties=2,
        close_finish_games=15,
        close_finish_avg=Fraction(4, 3),
        overshoot_samples=15,
        overshoot_distribution={0: 5, 1: 10},
        exact_target_rate=Fraction(1, 3),
    )
    player_view = _ingredient_view({f"key_{index}": Fraction(index + 1, 50) for index in range(10)})
    meta_embed = build_meta_insights_embed(meta_view)
    for embed in (meta_embed, build_player_insights_embed(player_view)):
        _assert_clean(embed)
        _assert_within_limits(embed)
    assert "Deciding factors" in _fields(meta_embed)


def test_deciding_factors_fit_beside_the_many_sources_stress_case() -> None:
    meta = meta_summary(_records())
    keys = [f"source_{index:02d}_{'z' * 30}" for index in range(40)]
    meta = replace(
        meta,
        winner_composition={key: Fraction(7, 3) for key in keys},
        loser_composition={key: Fraction(5, 3) for key in keys},
        winner_composition_samples={key: 12 for key in keys},
        loser_composition_samples={key: 30 for key in keys},
        awards={key: _award_stat(key) for key in keys},
        lead_sources={key: Fraction(1, 3) for key in keys},
        lead_source_samples=30,
        board_leader_games=30,
        board_leader_wins=15,
        close_finish_games=30,
        close_finish_avg=Fraction(1),
        overshoot_samples=30,
        overshoot_distribution={0: 30},
        exact_target_rate=Fraction(1),
    )
    players = [_extreme_summary(1_000_000_000_000_000_000 + i, award_count=0) for i in range(40)]
    embed = build_meta_insights_embed(MetaInsightsView(ALL_TIME, meta, players))

    _assert_within_limits(embed)
    _assert_clean(embed)
