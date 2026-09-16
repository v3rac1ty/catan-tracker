# Security

## Scope and threat model

Catan Tracker stores guild configuration, player IDs, game and season history, event details, RSVP state, and reminder state. The highest-value secrets are the Discord bot token, the runtime database URL, and the migration database URL. A malicious guild member is assumed to control slash-command text, dates, IDs supplied through Discord, button custom IDs, and stored content that later appears in an embed. A guild administrator is trusted to configure that guild, but must not be able to read or mutate another guild's rows. The deployment host, container runtime, Discord, and PostgreSQL host are separate trust boundaries.

The application flow is interaction or scheduler -> cog/view -> service -> repository -> PostgreSQL. Cogs and views do not execute SQL. The scheduler intentionally performs system-wide claim and sweep queries, while user-triggered service calls remain guild-scoped. Migrations run separately with the schema-owning role. Host compromise, Discord account compromise, and compromise of an upstream dependency are outside the application's control and require operational response.

## Controls in the M6 checkpoint

- **SQL injection:** Repository SQL is module-level text with asyncpg bind parameters; the static guard rejects dynamic SQL, indirect sink aliases, stacked statements, unsafe identifiers, and database calls outside repositories or the migration allowlist (`tests/static/sql_guard.py`, `tests/static/test_no_dynamic_sql.py`). Dynamic payload coverage is in `tests/integration/test_sql_injection.py`.
- **Authorization and tenant isolation:** Services compute permissions from the interaction actor and stored guild configuration (`src/catan_bot/services/context.py:59-104`). Repository predicates include `guild_id` for caller actions, with cross-guild tests in `tests/integration/test_guild_isolation.py`. Admin role assignment itself requires Manage Server (`src/catan_bot/services/config_service.py:24-60`).
- **Discord output:** Free text is cleaned at input and escaped at display (`src/catan_bot/domain/validation.py:242-280`; `src/catan_bot/formatting.py:177-210`). Default mentions are disabled (`src/catan_bot/bot.py:41-49`). Event announcements and reminders may allow exactly one configured player-role ID; unset configuration sends no mention. RSVP member mentions are rendered only in embeds delivered with user mentions disabled.
- **Publish targets:** `/event create` and `/leaderboard` verify that a selected target is in the invoking guild and that both the caller and bot can view it, send messages, and embed links before any event is written. Explicitly targeted posts receive an ephemeral jump-link confirmation.
- **Buttons and IDs:** Dynamic item templates accept only fixed actions and decimal IDs (`src/catan_bot/views/game_confirm.py:24-80`; `src/catan_bot/views/event_rsvp.py:15-83`). Callbacks derive the guild from the interaction and repeat service-side authorization.
- **Database roles and timeouts:** The runtime role is NOSUPERUSER, NOINHERIT, and has SELECT/INSERT/UPDATE privileges only; schema migration uses a separate owner role (`db/roles.sql:11-16,57-71,103-132`). Runtime pools use a five-second server timeout and ten-second client command timeout (`src/catan_bot/db/pool.py:16-24`). The database port is loopback-only in Compose (`docker-compose.yml:31-37`).
- **Credential bootstrap:** Role passwords must be present and contain at least 16 URL-safe characters before role creation; this rejects whitespace and DSN-breaking punctuation (`db/roles.sql:33-57`).
- **Container baseline:** The application image runs as an unprivileged user (`Dockerfile:10-13`); Compose makes bot and migration filesystems read-only, drops capabilities, enables `no-new-privileges`, and limits PostgreSQL to loopback (`docker-compose.yml:1-31`).
- **CI and documentation:** The workflow checks out pinned action revisions, runs dependency auditing, migrations, Ruff, Bandit, and the full test suite (`.github/workflows/ci.yml:1-85`). Deployment, backup, restore, and host-hardening procedures are documented in `README.md:145-150` and `docs/DEPLOYMENT.md:1-9,276-320`.
- **Failure handling:** User-facing errors are fixed or escaped, and unexpected logs contain metadata, exception type, and safe SQLSTATE only (`src/catan_bot/errors.py:96-130,165-196`; `src/catan_bot/services/event_service.py:164-188`; `src/catan_bot/services/season_service.py:185-207`; `src/catan_bot/db/migrate.py:55-94,308-313`).

## Findings and recommended fixes

| Severity | Evidence | Finding and exploit scenario | Recommended fix |
|---|---|---|---|
| Medium | `pyproject.toml:10-15`; `Dockerfile:1`; `docker-compose.yml:18-19` | Dependencies and base images use ranges or moving tags. A compromised or incompatible upstream release can enter a rebuild without a source change. | Commit a lock or constraints file with hashes, pin Python/Postgres image digests, and review dependency and image upgrades separately. |
| Info | `docker-compose.yml:22-26,43-57`; `.env.example:1-38` | Passwords and the Discord token are supplied as container environment variables. A user with Docker host inspection access can read them. | Use a secrets manager or Docker/OCI secrets where available, restrict host and Docker-socket access, and rotate credentials after accidental exposure. Never include `.env` in support bundles. |
| Low | `src/catan_bot/cogs/game_cog.py:59`; event commands in `src/catan_bot/cogs/event_cog.py:30-127`; read commands in `src/catan_bot/cogs/stats_cog.py:15-56`; `src/catan_bot/db/pool.py:16-20` | Only game reporting has a command cooldown. Repeated event, RSVP, and read commands can consume the small pool and Discord/API quota. | Add per-user and per-guild rate limits for expensive commands and metrics for rejected requests. Keep database timeouts and pool limits as defense in depth. |
| Low | `src/catan_bot/services/event_service.py:224-239`; `src/catan_bot/scheduler.py:8-13,210-211` | Reminder delivery can be lost after a committed claim, and a crash before an announcement is marked can duplicate it. These are at-most-once and duplicate-delivery risks in a single-bot deployment. | Add an outbox or durable delivery state with retry/dead-letter handling and idempotency keys; alert operators when delivery is abandoned. |

## SQL injection category review

| Category | Result | Evidence |
|---|---|---|
| Comments, quote breaking, tautologies, stacked statements | PASS | `tests/integration/test_sql_injection.py:50-70`; `tests/integration/test_privileges.py:129-155`; `tests/static/test_no_dynamic_sql.py` |
| UNION, boolean, error-based and time-based blind SQLi | PASS | `tests/integration/test_sql_injection.py:50-70,377-414`; `src/catan_bot/db/pool.py:16-24` |
| Out-of-band, file reads, `COPY ... PROGRAM` | PASS | `tests/integration/test_sql_injection.py:67`; `tests/integration/test_privileges.py:53-57`; role settings in `db/roles.sql:57-71,99-114` |
| CHR/hex, encoded and Unicode quote bypasses | PASS | `tests/integration/test_sql_injection.py:58-70,377-414` |
| ORDER BY, LIMIT and identifier injection | PASS | Fixed scope choices in `src/catan_bot/services/stats_service.py:13-43`; parameterized limits in repository queries; `tests/static/test_no_dynamic_sql.py` |
| Second-order stored payloads | PASS | `tests/integration/test_sql_injection.py:377-414`; `tests/integration/test_service_hygiene.py:136-190`; `src/catan_bot/formatting.py:177-210` |
| Privilege escalation and destructive SQL | PASS | `db/roles.sql:103-132`; `tests/integration/test_privileges.py:28-70,84-176` |

The SQL guard's documented migration-file exception is limited to the trusted, repository-committed migration execution (`src/catan_bot/db/migrate.py:242-247`). No bypass was identified in the reviewed production SQL paths. Migration execution remains an architectural trust boundary: a reviewed release must protect the schema-owning credential and migration artifacts.

## Operational responsibilities

1. Keep the Discord token, app DSN, and migrator DSN in a secret store or tightly permissioned environment; never paste them into issues, logs, images, or shell history. Rotate the token in the Discord Developer Portal and rotate both database passwords after exposure.
2. Run the migrator from a reviewed release artifact, then run the bot with `catan_app`. Do not give the bot the migrator DSN. Verify runtime role flags and grants after provisioning.
3. Keep `SYNC_COMMANDS=false` after the intended command-sync run. Restrict the bot invite to `bot` and `applications.commands` and only the channel permissions it needs.
4. Bind PostgreSQL to loopback for local Compose use. For a remote database, require TLS, firewall rules, private networking, and certificate verification; do not publish PostgreSQL broadly.
5. Back up encrypted database dumps off-host, monitor backup failures, and periodically restore into an isolated database before relying on the backup.
6. Patch Python, discord.py, asyncpg, tzdata, PostgreSQL, Docker, and the host through reviewed, pinned updates. Run the complete test, static guard, Ruff, and Bandit checks before release.

## Vulnerability reporting

Report suspected vulnerabilities privately to the repository owner or deployment maintainer through the private channel associated with the source-control project or Discord application owner. Do not open a public issue containing a token, DSN, exploit payload that reaches production data, or unredacted logs. Include the affected commit or deployment version, impact, minimal reproduction, and proposed mitigation after removing secrets and personal data. If no private maintainer channel is available, use the hosting platform's private security-advisory mechanism once enabled by the owner. Rotate exposed credentials immediately while preserving only sanitized evidence.

## Verification and residual risk

M6 verification passed Ruff, the format check for 95 files, Bandit, and `pip-audit` with no known vulnerabilities (the editable application was skipped). The full live database suite passed with 1390 tests, one upstream `audioop` deprecation warning, and an 81.40-second runtime. Compose configuration validation, an ARM64 image build, a hardened no-login bot-container probe, and an idempotent migration-container run reporting no pending migrations all passed. Backup catalog, isolated restore, and query verification passed; the temporary database and dump were removed. GitHub-hosted CI, live Discord command sync, live button clicks and reminders, and OCI deployment remain unverified. Reminder loss after a committed claim, duplicate announcements after a crash, single-bot deployment assumptions, the trusted migration boundary, host compromise, Discord-side abuse controls, mutable dependency/image inputs, and the operational findings above remain residual risks. M6 includes sanitized migration logging and configuration/password hardening.
