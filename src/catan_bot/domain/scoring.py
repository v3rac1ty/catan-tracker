"""Pure, rule-aware score entry and validation for reported Catan games.

The domain represents points as a row per player and a column per point
source.  Keeping the columns in one catalog means the Discord workflow can
render the same table that validation uses, including expansion-specific
sources.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Literal

from catan_bot.domain.errors import DomainValidationError

GameType = Literal["normal", "seafarers", "cities_knights", "seafarers_cities_knights"]
_GAME_TYPES = frozenset({"normal", "seafarers", "cities_knights", "seafarers_cities_knights"})
_BIGINT_MAX = 2**63 - 1
_MAX_STORED_POINTS = 99

# A Discord modal supports at most 5 text inputs.  Phase 2's per-player DM
# score sheet is one modal per player, so the numeric (non-award) column
# count must never exceed this -- see `entry_fields`.
_MAX_MODAL_NUMERIC_FIELDS = 5


@dataclass(frozen=True, slots=True)
class GameRules:
    """Rules selected for one game.

    ``target_points`` is optional on the value object so a form can be built
    incrementally; :func:`build_rules` fills defaults and rejects combinations
    for which the target is required.
    """

    game_type: GameType
    extension_5_6: bool = False
    scenario: str | None = None
    target_points: int | None = None

    def __post_init__(self) -> None:
        _validate_game_type(self.game_type)
        if type(self.extension_5_6) is not bool:
            raise DomainValidationError("The 5–6 player extension setting must be true or false.")
        if self.scenario is not None and (
            type(self.scenario) is not str or not self.scenario.strip()
        ):
            raise DomainValidationError("A scenario must have a name when provided.")
        if self.target_points is not None:
            _validate_points(self.target_points, "The target score")
            if self.target_points == 0:
                raise DomainValidationError("The target score must be greater than zero.")


@dataclass(frozen=True, slots=True)
class ScoreSource:
    """A column in the point-entry table.

    ``fixed_points`` describes an award such as Longest Road (2 points).
    ``exclusive`` means no more than one player may claim that award.  The
    other metadata is intentionally declarative so UIs can show useful hints
    without duplicating validation rules.
    """

    key: str
    label: str
    help_text: str
    section: str
    exclusive: bool = False
    requires_even: bool = False
    fixed_points: int | None = None
    min_points: int = 0

    def __post_init__(self) -> None:
        for value, field in (
            (self.key, "A score source key"),
            (self.label, "A score source label"),
            (self.help_text, "A score source description"),
            (self.section, "A score source section"),
        ):
            if type(value) is not str or not value.strip():
                raise DomainValidationError(f"{field} is required.")
        if type(self.exclusive) is not bool or type(self.requires_even) is not bool:
            raise DomainValidationError("Score source validation flags must be true or false.")
        _validate_points(self.min_points, "A score source minimum")
        if self.fixed_points is not None:
            _validate_points(self.fixed_points, "A score source fixed value")
            if self.fixed_points < self.min_points:
                raise DomainValidationError(
                    "A score source fixed value can't be below its minimum."
                )

    @property
    def award_points(self) -> int | None:
        """Compatibility name for UIs that call a fixed award its value."""

        return self.fixed_points


@dataclass(frozen=True, slots=True)
class ScoreEntry:
    """One player's points for one catalogued source."""

    key: str
    points: int

    @property
    def source_key(self) -> str:
        """Alias useful to persistence and form code."""

        return self.key

    def __post_init__(self) -> None:
        if type(self.key) is not str or not self.key.strip():
            raise DomainValidationError("A score source key is required.")
        _validate_points(self.points, "Points")

    def __iter__(self):
        """Allow persistence adapters to treat an entry like a key/value pair."""

        yield self.key
        yield self.points


@dataclass(frozen=True, slots=True)
class PlayerScore:
    """A complete row in the score-entry table."""

    user_id: int
    total_points: int
    breakdown: tuple[ScoreEntry, ...]

    def __post_init__(self) -> None:
        _validate_user_id(self.user_id)
        _validate_points(self.total_points, "The total score")
        if type(self.breakdown) is not tuple:
            raise DomainValidationError("A score breakdown must be a tuple of score entries.")
        if any(type(entry) is not ScoreEntry for entry in self.breakdown):
            raise DomainValidationError("A score breakdown contains an invalid entry.")


def _validate_game_type(game_type: object) -> None:
    if type(game_type) is not str or game_type not in _GAME_TYPES:
        raise DomainValidationError(
            "Choose a supported game type: normal, seafarers, cities_knights, "
            "or seafarers_cities_knights."
        )


def _validate_nonnegative_int(value: object, field: str) -> None:
    if type(value) is not int:
        raise DomainValidationError(f"{field} must be a whole number.")
    if value < 0:
        raise DomainValidationError(f"{field} can't be negative.")
    if value > _BIGINT_MAX:
        raise DomainValidationError(f"{field} is too large to store.")


def _validate_points(value: object, field: str) -> None:
    _validate_nonnegative_int(value, field)
    if value > _MAX_STORED_POINTS:
        raise DomainValidationError(f"{field} must be {_MAX_STORED_POINTS} points or fewer.")


def _validate_user_id(user_id: object) -> None:
    if type(user_id) is not int:
        raise DomainValidationError("A player id must be a whole number.")
    if user_id <= 0:
        raise DomainValidationError("A player id must be a positive number.")
    if user_id > _BIGINT_MAX:
        raise DomainValidationError("A player id is too large to store.")


def build_rules(
    game_type: GameType,
    *,
    extension_5_6: bool = False,
    scenario: str | None = None,
    target_points: int | None = None,
    player_count: int | None = None,
) -> GameRules:
    """Create a validated rule selection and apply target defaults.

    Normal games default to 10 points and Cities & Knights games to 13.  A
    Seafarers game or any named scenario must state its target explicitly,
    because scenarios commonly use a different victory threshold.
    """

    _validate_game_type(game_type)
    if player_count is not None:
        _validate_nonnegative_int(player_count, "The player count")
        if not 2 <= player_count <= 6:
            raise DomainValidationError("A game needs 2 to 6 players.")
        if player_count >= 5 and extension_5_6 is not True:
            raise DomainValidationError(
                "Games with 5 or 6 players require the 5–6 Player Extension."
            )
    if (
        game_type in ("seafarers", "seafarers_cities_knights") or scenario is not None
    ) and target_points is None:
        raise DomainValidationError("Seafarers and scenario games require a target score.")
    if target_points is None:
        target_points = 13 if game_type in ("cities_knights", "seafarers_cities_knights") else 10
    return GameRules(
        game_type=game_type,
        extension_5_6=extension_5_6,
        scenario=scenario,
        target_points=target_points,
    )


def _source(
    key: str,
    label: str,
    help_text: str,
    section: str,
    *,
    exclusive: bool = False,
    requires_even: bool = False,
    fixed_points: int | None = None,
) -> ScoreSource:
    return ScoreSource(
        key,
        label,
        help_text,
        section,
        exclusive=exclusive,
        requires_even=requires_even,
        fixed_points=fixed_points,
    )


def score_sources(
    rules: GameRules | GameType, *, scenario: str | None = None
) -> tuple[ScoreSource, ...]:
    """Return the ordered point columns for a rule set."""

    if isinstance(rules, GameRules):
        game_type = rules.game_type
        scenario = rules.scenario
    else:
        _validate_game_type(rules)
        game_type = rules
    result: list[ScoreSource] = [
        _source("settlements", "Settlements / houses", "1 point per settlement.", "board"),
        _source(
            "cities",
            "Cities",
            "2 points per city; enter an even total.",
            "board",
            requires_even=True,
        ),
    ]
    if game_type in ("seafarers", "seafarers_cities_knights"):
        result.append(
            _source(
                "longest_trade_route",
                "Longest Trade Route",
                "2-point exclusive award.",
                "awards",
                exclusive=True,
                fixed_points=2,
            )
        )
    else:
        result.append(
            _source(
                "longest_road",
                "Longest Road",
                "2-point exclusive award.",
                "awards",
                exclusive=True,
                fixed_points=2,
            )
        )
    if game_type in ("normal", "seafarers"):
        result.extend(
            (
                _source(
                    "largest_army",
                    "Largest Army",
                    "2-point exclusive award.",
                    "awards",
                    exclusive=True,
                    fixed_points=2,
                ),
                _source(
                    "vp_cards",
                    "Victory Point cards",
                    "Points from hidden victory-point cards.",
                    "cards",
                ),
            )
        )
    else:
        result.extend(
            (
                _source(
                    "metropolis_bonus",
                    "Metropolis bonus",
                    "2 points per metropolis; enter an even total.",
                    "cities_knights",
                    requires_even=True,
                ),
                _source(
                    "defender_of_catan",
                    "Defender of Catan",
                    "1 point per Defender of Catan card; multiple defenses can accumulate.",
                    "cities_knights",
                ),
                _source(
                    "merchant",
                    "Merchant",
                    "1-point exclusive Merchant award.",
                    "cities_knights",
                    exclusive=True,
                    fixed_points=1,
                ),
                _source(
                    "constitution",
                    "Constitution",
                    "Victory points from the Constitution progress card.",
                    "cities_knights",
                    exclusive=True,
                    fixed_points=1,
                ),
                _source(
                    "printer",
                    "Printer",
                    "Victory points from the Printer progress card.",
                    "cities_knights",
                    exclusive=True,
                    fixed_points=1,
                ),
            )
        )
    if game_type in ("seafarers", "seafarers_cities_knights") or scenario is not None:
        result.append(
            _source(
                "scenario_points",
                "Scenario points",
                "Points awarded by the selected scenario.",
                "scenario",
            )
        )
    return tuple(result)


def score_sections(
    rules: GameRules | GameType, *, scenario: str | None = None
) -> dict[str, tuple[ScoreSource, ...]]:
    """Group score columns by section for a table or multi-page form."""

    sections: dict[str, list[ScoreSource]] = {}
    for source in score_sources(rules, scenario=scenario):
        sections.setdefault(source.section, []).append(source)
    return {section: tuple(sources) for section, sources in sections.items()}


def score_pages(
    rules: GameRules | GameType, *, columns_per_page: int = 5, scenario: str | None = None
) -> tuple[tuple[ScoreSource, ...], ...]:
    """Split source columns into stable chunks suitable for paginated forms."""

    if type(columns_per_page) is not int or columns_per_page <= 0:
        raise DomainValidationError("Columns per page must be a positive whole number.")
    sources = score_sources(rules, scenario=scenario)
    return tuple(
        sources[i : i + columns_per_page] for i in range(0, len(sources), columns_per_page)
    )


def entry_fields(
    rules: GameRules,
) -> tuple[tuple[ScoreSource, ...], tuple[ScoreSource, ...]]:
    """Split a rule set's score sources into numeric fields and award fields.

    Numeric sources (``fixed_points is None``) are the ones a player types a
    count into; award sources (``fixed_points is not None``) are the ones a
    player just claims or doesn't. Phase 2's per-player DM score sheet is a
    single Discord modal per player, and a modal supports at most 5 text
    inputs -- the whole one-modal-per-player design depends on the numeric
    set never exceeding that. Asserting it here (instead of trusting it to
    keep holding as `score_sources` grows) means a future rules change that
    breaks the assumption fails loudly in this module, not silently in a
    cog that tries to render a 6-field modal.
    """

    if type(rules) is not GameRules:
        raise DomainValidationError("Game rules are required to split score sources.")
    sources = score_sources(rules)
    numeric = tuple(source for source in sources if source.fixed_points is None)
    awards = tuple(source for source in sources if source.fixed_points is not None)
    if len(numeric) > _MAX_MODAL_NUMERIC_FIELDS:
        # This is a design invariant, not bad user input: no `rules` a caller
        # can actually construct trips it today (every real catalog fits),
        # and no domain-validation error message would make sense shown to a
        # player. If a future rules change ever breaks the invariant, this
        # must fail loudly wherever the catalog changed, not be swallowed by
        # a cog treating it like an ordinary `DomainValidationError`.
        raise RuntimeError(
            "This rule set needs more numeric score fields than a single modal "
            f"can hold ({len(numeric)} > {_MAX_MODAL_NUMERIC_FIELDS})."
        )
    return numeric, awards


def build_player_score(
    rules: GameRules,
    user_id: int,
    *,
    numeric: Mapping[str, int],
    awards: Iterable[str],
) -> PlayerScore:
    """Compose one player's full score row from modal inputs and claimed awards.

    Each participant's DM modal only collects `entry_fields`'s numeric
    fields plus which awards (if any) they claim -- the total is never typed
    by a user any more, since ``total_points == sum(breakdown)`` makes it
    fully derivable. An unclaimed award becomes an explicit ``0`` entry (not
    an absent one), matching the invariant `validate_player_score` already
    enforces: every row covers every source in the rule set's catalog.
    Unknown or missing numeric keys, and unknown claimed awards, all raise
    `DomainValidationError` before a `PlayerScore` is ever built; the
    composed row is then run through `validate_player_score` so every other
    rule (parity, exclusivity, metropolis-requires-cities, ...) is enforced
    exactly once, in one place.
    """

    if type(rules) is not GameRules:
        raise DomainValidationError("Game rules are required to build a score.")
    numeric_sources, award_sources = entry_fields(rules)
    numeric_keys = {source.key for source in numeric_sources}
    award_keys = {source.key for source in award_sources}

    if not isinstance(numeric, Mapping):
        raise DomainValidationError("Numeric score entries must be a mapping.")
    numeric_values = dict(numeric)
    unknown_numeric = sorted(set(numeric_values) - numeric_keys)
    if unknown_numeric:
        raise DomainValidationError(
            "Unknown numeric score field(s): " + ", ".join(unknown_numeric) + "."
        )
    missing_numeric = sorted(numeric_keys - set(numeric_values))
    if missing_numeric:
        raise DomainValidationError(
            "Missing numeric score field(s): " + ", ".join(missing_numeric) + "."
        )

    claimed = list(awards)
    if len(set(claimed)) != len(claimed):
        raise DomainValidationError("The same award can't be claimed twice.")
    unknown_awards = sorted(set(claimed) - award_keys)
    if unknown_awards:
        raise DomainValidationError("Unknown award(s) claimed: " + ", ".join(unknown_awards) + ".")
    claimed_set = set(claimed)

    values: dict[str, int] = dict(numeric_values)
    for source in award_sources:
        values[source.key] = source.fixed_points if source.key in claimed_set else 0  # type: ignore[assignment]

    # Iterate the rule set's own catalog order (not numeric-then-awards), so
    # the breakdown reads the same way regardless of how it was assembled --
    # matching `_player_score_from_values` in `db/repositories/games.py`.
    breakdown = tuple(ScoreEntry(source.key, values[source.key]) for source in score_sources(rules))
    total_points = sum(entry.points for entry in breakdown)
    score = PlayerScore(user_id=user_id, total_points=total_points, breakdown=breakdown)
    return validate_player_score(score, rules)


def _score_entries(score: PlayerScore, rules: GameRules) -> dict[str, int]:
    sources = score_sources(rules)
    source_map = {source.key: source for source in sources}
    entries: dict[str, int] = {}
    for entry in score.breakdown:
        if entry.key in entries:
            raise DomainValidationError(f"The score breakdown repeats '{entry.key}'.")
        if entry.key not in source_map:
            raise DomainValidationError(f"'{entry.key}' isn't valid for this game type.")
        entries[entry.key] = entry.points
    missing = [source.key for source in sources if source.key not in entries]
    if missing:
        raise DomainValidationError("The score breakdown is missing: " + ", ".join(missing) + ".")
    if score.total_points != sum(entries.values()):
        raise DomainValidationError("The total score must equal the sum of the point breakdown.")
    for source in sources:
        value = entries[source.key]
        if value < source.min_points:
            raise DomainValidationError(
                f"{source.label} can't be below {source.min_points} points."
            )
        if source.requires_even and value % 2:
            raise DomainValidationError(f"{source.label} must be an even number of points.")
        if source.fixed_points is not None and value not in (0, source.fixed_points):
            raise DomainValidationError(
                f"{source.label} is either 0 or {source.fixed_points} points."
            )
    metropolis = entries.get("metropolis_bonus", 0)
    cities = entries.get("cities", 0)
    if metropolis > cities:
        raise DomainValidationError("Metropolis points can't exceed the points from cities.")
    if metropolis and not cities:
        raise DomainValidationError("Metropolis points require at least one city.")
    return entries


def validate_player_score(
    score: PlayerScore | GameRules, rules: GameRules | PlayerScore
) -> PlayerScore:
    """Validate one complete player row and return it unchanged."""

    # Accept both ``(score, rules)`` (matching the other domain validators)
    # and ``(rules, score)`` (matching rule-first workflow code).
    if type(score) is GameRules and type(rules) is PlayerScore:
        score, rules = rules, score
    if type(score) is not PlayerScore:
        raise DomainValidationError("A player score is required.")
    if type(rules) is not GameRules:
        raise DomainValidationError("Game rules are required to validate a score.")
    _score_entries(score, rules)
    return score


def validate_game_scores(
    rules: GameRules,
    scores: Iterable[PlayerScore] | None,
    participants: Iterable[int] | None = None,
    *,
    participant_ids: Iterable[int] | None = None,
    winner_id: int | None = None,
    allow_partial: bool = False,
) -> tuple[PlayerScore, ...]:
    """Validate every score row, participant coverage, and shared awards.

    ``allow_partial=False`` (the default) is byte-for-byte the original
    behavior, error messages included: every participant must have exactly
    one score row, or none at all (see the empty-sheet case below).

    ``allow_partial=True`` is for the DM-based score collection flow, where
    each participant submits their own row independently, potentially over
    a 24-hour window (see `game_score_requests`), so a game legitimately
    holds some rows and not others while collection is still in progress.
    An absent row stays SQL NULL -- deliberately distinct from a recorded
    zero -- so nothing here ever invents a score nobody submitted. Under it:
      - any subset of participants may have a row, including none at all;
      - every row that IS present still gets full `validate_player_score`
        treatment;
      - still at most one row per player, and every row's player must be
        one of the game's participants;
      - an exclusive award's claimed total is checked only across the rows
        that exist so far -- there's nothing to check a not-yet-submitted
        row against;
      - "the winner must reach the target score" only applies once the
        winner's own row has arrived; it says nothing about a game whose
        winner hasn't submitted yet.
    """

    if type(rules) is not GameRules:
        raise DomainValidationError("Game rules are required to validate scores.")
    if participants is not None and participant_ids is not None:
        raise DomainValidationError("Provide participants only once.")
    raw_participants = participant_ids if participant_ids is not None else participants
    if raw_participants is None:
        raise DomainValidationError("The game participants are required.")
    participant_list = list(raw_participants)
    if not 2 <= len(participant_list) <= 6:
        raise DomainValidationError("A game needs 2 to 6 players.")
    for user_id in participant_list:
        _validate_user_id(user_id)
    if len(set(participant_list)) != len(participant_list):
        raise DomainValidationError("The same player can't be listed more than once.")
    if len(participant_list) >= 5 and not rules.extension_5_6:
        raise DomainValidationError("Games with 5 or 6 players require the 5–6 Player Extension.")
    score_list = [] if scores is None else list(scores)
    # Legacy reports may have no score sheet at all.  Keep that distinct from
    # a partial sheet: NULL score columns are intentionally not rewritten as
    # zeroes, and there is no winner target to validate in this case.
    if not score_list:
        return ()
    if winner_id is None or type(winner_id) is not int:
        raise DomainValidationError("A winning player is required.")
    _validate_user_id(winner_id)
    if winner_id not in participant_list:
        raise DomainValidationError("The winner must be one of the game participants.")
    if not allow_partial and len(score_list) != len(participant_list):
        raise DomainValidationError("Enter exactly one score for every participant.")
    ids = [score.user_id if type(score) is PlayerScore else None for score in score_list]
    if len(set(ids)) != len(ids):
        raise DomainValidationError("The same player can't have more than one score.")
    if allow_partial:
        if not set(ids) <= set(participant_list):
            # Distinct from the "every participant" coverage message below:
            # under partial collection, *missing* rows are always fine, so
            # the only way this branch triggers is a row for someone who
            # isn't a participant at all -- a different mistake that deserves
            # its own accurate wording rather than reusing the "exactly one"
            # phrasing, which would be actively misleading here.
            raise DomainValidationError(
                "A score can only be recorded for one of this game's participants."
            )
    elif set(ids) != set(participant_list):
        raise DomainValidationError("Enter exactly one score for every participant.")
    for score in score_list:
        validate_player_score(score, rules)
    source_map = {source.key: source for source in score_sources(rules)}
    for source in source_map.values():
        if source.exclusive:
            claimed = sum(
                next(entry.points for entry in score.breakdown if entry.key == source.key)
                for score in score_list
            )
            if claimed > (source.fixed_points or 0):
                raise DomainValidationError(f"Only one player can claim {source.label}.")
    # Non-partial: the coverage check above already guarantees the winner's
    # row is present, so `next(..., None)` always resolves. Partial: it may
    # legitimately be absent, in which case there's no target to check yet.
    winner_score = next((score for score in score_list if score.user_id == winner_id), None)
    if winner_score is not None and winner_score.total_points < (rules.target_points or 0):
        raise DomainValidationError("The winner must reach the target score.")
    return tuple(score_list)
