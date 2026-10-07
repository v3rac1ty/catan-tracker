#!/usr/bin/env bash
# One-command production deploy for the catan-tracker bot.
#
# Run on the VM as the normal (non-root) user, from any directory:
#   bash scripts/deploy.sh [--no-sync] [--skip-ci-check] [--force] [--timeout SECONDS]
#   bash scripts/deploy.sh --rollback [REV]
#   bash scripts/deploy.sh --help
#
# Run it inside tmux or screen: a lost SSH session kills it mid-deploy.
# See docs/DEPLOYMENT.md section 7 for what each step does and how to recover.
set -Eeuo pipefail

# --- settings -------------------------------------------------------------
# GitHub check runs that must be completed with conclusion "success" before a
# deploy. Keep in sync with the job `name:` values in .github/workflows/ci.yml
# (tests/static/test_deploy_script.py enforces this).
REQUIRED_CHECKS=(
  "Python 3.12 / PostgreSQL 16"
  "Production container smoke test"
)

TIMEOUT=120                          # seconds to wait for db / migrate / the bot
CI_WAIT_MAX=900                      # seconds to wait for pending CI checks
CI_POLL="${DEPLOY_CI_POLL_SECONDS:-20}"
HEALTH_POLL="${DEPLOY_HEALTH_POLL_SECONDS:-3}"
STABLE_SECONDS="${DEPLOY_STABLE_SECONDS:-10}"   # bot must stay up this long
STABLE_POLL="${DEPLOY_STABLE_POLL_SECONDS:-1}"
SYNC=1
SKIP_CI=0
FORCE=0
ROLLBACK=0
ROLLBACK_REV=""

ACTION=deploy                        # deploy | rollback (history log label)
PREV=""                              # sha the bot was running before this run
TARGET=""                            # sha being deployed / rolled back to
BACKUP=""                            # path of this run's pre-change backup
START=""                             # UTC timestamp used to scope log reads
BOT_ID=""                            # id of the bot container just started
STEP=""                              # current phase, for failure messages
MIGRATED=0                           # 1 once a migration was applied this run
SYNCED="no"
ARMED=0                              # 1 while a failure must trigger rollback
ROLLING_BACK=0
MAIN_PID=$$

STATE=backups/.deployed-sha          # sha the bot is known to be running
HISTORY=backups/deploy-history.log

usage() {
  cat <<'EOF'
Usage:
  bash scripts/deploy.sh [--no-sync] [--skip-ci-check] [--force] [--timeout SECONDS]
  bash scripts/deploy.sh --rollback [REV]
  bash scripts/deploy.sh --help

Deploy: fetch origin, require green CI on the new commit, back up the database,
fast-forward, rebuild, apply migrations, sync slash commands once, and wait for
the bot to connect and stay up. Any failure (or Ctrl-C / SIGTERM) after the
update rolls the code back automatically to the revision that was running. The
database is never restored automatically and the db container is never
recreated. Run it inside tmux or screen.

Options:
  --no-sync         do not sync slash commands this run
  --skip-ci-check   do not require passing GitHub checks on the new commit
  --force           redeploy even if the running revision is already current
  --timeout SECONDS how long to wait for db, migrate and the bot (default 120)
  --rollback [REV]  back up, then return to REV (default: the commit that ran
                    before the last successful deploy) and rebuild
  --help            show this help
EOF
}

# --- output ---------------------------------------------------------------
log() { printf 'deploy: %s\n' "$*"; }
warn() { printf 'deploy: WARNING: %s\n' "$*"; }
die() { printf 'deploy: ERROR: %s\n' "$*" >&2; exit 1; }

# --- docker (sudo unless root; per-run env must go through `sudo env`) ----
dockerx() { if ((EUID == 0)); then docker "$@"; else sudo docker "$@"; fi; }
compose() { dockerx compose "$@"; }
compose_sync() {
  if ((EUID == 0)); then
    env SYNC_COMMANDS=true docker compose "$@"
  else
    sudo env SYNC_COMMANDS=true docker compose "$@"
  fi
}

utc_now() { date -u +%Y-%m-%dT%H:%M:%SZ; }
short() { git rev-parse --short "$1"; }

# --- arguments ------------------------------------------------------------
parse_args() {
  while (($#)); do
    case $1 in
      --no-sync) SYNC=0 ;;
      --skip-ci-check) SKIP_CI=1 ;;
      --force) FORCE=1 ;;
      --timeout)
        [[ ${2:-} =~ ^[1-9][0-9]*$ ]] || die "--timeout needs a positive number of seconds"
        TIMEOUT=$2
        shift
        ;;
      --rollback)
        ROLLBACK=1
        if [[ -n ${2:-} && $2 != --* ]]; then
          ROLLBACK_REV=$2
          shift
        fi
        ;;
      -h | --help)
        usage
        exit 0
        ;;
      *)
        usage >&2
        die "unknown argument: $1"
        ;;
    esac
    shift
  done
}

# --- state and history ----------------------------------------------------
history_add() { # action prev new result
  printf '%s action=%s prev=%s new=%s backup=%s result=%s\n' \
    "$(utc_now)" "$1" "$2" "$3" "${BACKUP:-none}" "$4" >>"$HISTORY"
}

# The sha the bot is known to be running: written only after a deploy or
# rollback was verified healthy. Prints nothing if unknown or unusable.
deployed_sha() {
  local sha=""
  if [[ -f $STATE ]]; then sha=$(tr -d '[:space:]' <"$STATE"); fi
  if [[ $sha =~ ^[0-9a-f]{40}$ ]] && git cat-file -e "$sha^{commit}" 2>/dev/null; then
    printf '%s' "$sha"
  fi
}

record_deployed() { # sha  (atomic: write a temp file, then rename)
  printf '%s\n' "$1" >"$STATE.tmp"
  mv -f "$STATE.tmp" "$STATE"
}

# Previous sha of the most recent successful deploy (empty if none).
last_good_previous() {
  [[ -f $HISTORY ]] || return 0
  grep -E ' action=deploy .* result=ok$' "$HISTORY" | tail -n 1 |
    sed -n 's/.* prev=\([0-9a-f]\{7,40\}\) .*/\1/p' || true
}

# Decide which revision counts as "running": the state file, else HEAD.
resolve_prev() {
  PREV=$(deployed_sha)
  if [[ -z $PREV ]]; then
    PREV=$(git rev-parse HEAD)
    warn "no recorded running revision ($STATE); assuming the checkout, $(short "$PREV"), is what runs"
  fi
}

# --- preflight ------------------------------------------------------------
default_branch() {
  local ref
  ref=$(git symbolic-ref --short -q refs/remotes/origin/HEAD 2>/dev/null || true)
  if [[ -n $ref ]]; then echo "${ref#origin/}"; else echo master; fi
}

preflight() {
  install -d -m 700 backups
  if command -v flock >/dev/null 2>&1; then
    exec 9>backups/.deploy.lock
    flock -n 9 || die "another deploy is already running"
  fi

  local dirty
  dirty=$(git status --porcelain)
  if [[ -n $dirty ]]; then
    printf '%s\n' "$dirty"
    die "working tree is not clean; keep production-only settings in .env"
  fi

  if ((EUID != 0)); then
    sudo -v || die "sudo is required to run docker"
  fi
  compose config --quiet || die "docker compose config is invalid"

  if grep -Eiq "^[[:space:]]*SYNC_COMMANDS=[[:space:]]*[\"']?(true|1|yes)" .env 2>/dev/null; then
    warn ".env sets SYNC_COMMANDS=true; set it to false, because this script syncs commands itself."
  fi
}

ensure_branch() {
  local branch=$1 current
  current=$(git symbolic-ref --short -q HEAD || true)
  if [[ $current != "$branch" ]]; then
    log "HEAD is ${current:-detached} (a previous rollback?); switching to $branch"
    git switch "$branch"
  fi
}

# --- CI gate --------------------------------------------------------------
# Reads one or more concatenated GitHub check-runs pages on stdin; the required
# check names are the arguments. Prints one verdict line:
#   ok N | failed NAMES | pending NAMES | missing NAMES | incomplete | invalid
# A required check must be completed with conclusion "success". Any other run
# that completed with a failing conclusion also fails the gate; other runs that
# are still pending are ignored once the required checks have passed.
read -r -d '' CI_PARSER <<'PY' || true
import json, re, sys

def clean(text):
    return re.sub(r"[^\x20-\x7e]", "?", str(text))

required = sys.argv[1:]
text = sys.stdin.read()
decoder = json.JSONDecoder()
runs, total, pos = [], None, 0
try:
    while True:
        while pos < len(text) and text[pos].isspace():
            pos += 1
        if pos >= len(text):
            break
        doc, pos = decoder.raw_decode(text, pos)
        page = doc["check_runs"]
        if not isinstance(page, list):
            raise ValueError("check_runs is not a list")
        if total is None:
            total = int(doc["total_count"])
        runs.extend(page)
    if total is None:
        raise ValueError("no pages")
except Exception:
    print("invalid")
    sys.exit(0)
if total > len(runs):
    print("incomplete")
    sys.exit(0)

passing = {"success", "skipped", "neutral"}
failed = [
    "%s (%s)" % (clean(r.get("name", "?")), clean(r.get("conclusion")))
    for r in runs
    if r.get("status") == "completed" and r.get("conclusion") not in passing
]
pending, missing = [], []
for name in required:
    mine = [r for r in runs if r.get("name") == name]
    if not mine:
        missing.append(clean(name))
        continue
    for r in mine:
        if r.get("status") != "completed":
            pending.append(clean(name))
            break
        if r.get("conclusion") != "success":
            failed.append("%s (%s; success required)" % (clean(name), clean(r.get("conclusion"))))
            break
anything_pending = any(r.get("status") != "completed" for r in runs)
if failed:
    print("failed " + ", ".join(failed))
elif pending or (missing and anything_pending):
    print("pending " + ", ".join(pending + missing))
elif missing:
    print("missing " + ", ".join(missing))
else:
    print("ok %d" % len(runs))
PY

github_repo() {
  local url slug
  url=$(git remote get-url origin)
  slug=$(printf '%s' "$url" | sed -E 's#^.*github\.com[:/]##; s#\.git$##; s#/+$##')
  [[ $url == *github.com* && $slug =~ ^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$ ]] ||
    die "cannot derive a GitHub owner/repo from origin ($url); rerun with --skip-ci-check"
  printf '%s' "$slug"
}

# Fetch every page of check runs for a commit (100 per page) and print the verdict.
ci_verdict() { # api-url
  local api=$1 page=1 pages="" body verdict
  while :; do
    body=$(curl -fsS --max-time 20 -H 'Accept: application/vnd.github+json' "$api&page=$page") ||
      die "could not query the GitHub checks API (unreachable or rate limited); rerun with --skip-ci-check"
    pages+="$body"$'\n'
    verdict=$(printf '%s' "$pages" | python3 -c "$CI_PARSER" "${REQUIRED_CHECKS[@]}") ||
      die "could not parse the GitHub checks response; rerun with --skip-ci-check"
    if [[ $verdict != incomplete ]]; then
      printf '%s' "$verdict"
      return 0
    fi
    ((page < 10)) || die "too many pages of GitHub check runs; rerun with --skip-ci-check"
    if ! printf '%s' "$body" | grep -q '"name"'; then
      die "GitHub check runs ended before total_count was reached; rerun with --skip-ci-check"
    fi
    page=$((page + 1))
  done
}

ci_gate() { # sha
  local sha=$1 repo api verdict waited=0
  repo=$(github_repo)
  api="https://api.github.com/repos/$repo/commits/$sha/check-runs?per_page=100"
  log "checking CI for $(short "$sha") on $repo (required: ${REQUIRED_CHECKS[*]})"
  while :; do
    verdict=$(ci_verdict "$api")
    case $verdict in
      ok*)
        log "CI passed (${verdict#ok } check runs)"
        return 0
        ;;
      failed*) die "CI failed for $(short "$sha"): ${verdict#failed }" ;;
      missing*) die "required CI check(s) never ran for $(short "$sha"): ${verdict#missing }" ;;
      pending*)
        ((waited < CI_WAIT_MAX)) || die "CI still not green after ${CI_WAIT_MAX}s: ${verdict#pending }"
        log "CI pending (${verdict#pending }); rechecking in ${CI_POLL}s"
        sleep "$CI_POLL"
        waited=$((waited + CI_POLL))
        ;;
      *) die "could not parse the GitHub checks response; rerun with --skip-ci-check" ;;
    esac
  done
}

# --- backup (docs/DEPLOYMENT.md section 8; backups are never deleted) ------
make_backup() { # label
  local stamp
  stamp=$(date -u +%Y%m%dT%H%M%SZ)
  BACKUP="backups/catan-${stamp}-$1.dump"
  log "backing up the database to $BACKUP"
  (
    umask 077
    compose exec -T db pg_dump -U catan_migrator -d catan -Fc >"$BACKUP"
  ) || die "pg_dump failed (partial file, if any, left at $BACKUP); nothing was changed"
  [[ -s $BACKUP ]] || die "backup $BACKUP is empty; nothing was changed"
  compose exec -T db pg_restore -l <"$BACKUP" >/dev/null ||
    die "backup $BACKUP failed the pg_restore -l check; nothing was changed"
}

# --- containers -----------------------------------------------------------
service_id() { # service  (any state)
  compose ps -aq "$1" 2>/dev/null | head -n 1 || true
}

# "<status> <exit code>" for a container id, or "missing -1".
state_of() { # id
  [[ -n $1 ]] || {
    echo "missing -1"
    return 0
  }
  dockerx inspect -f '{{.State.Status}} {{.State.ExitCode}}' "$1" 2>/dev/null || echo "missing -1"
}

restarts_of() { # id
  dockerx inspect -f '{{.RestartCount}}' "$1" 2>/dev/null || echo "-1"
}

health_of() { # id
  dockerx inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$1" 2>/dev/null ||
    echo "missing"
}

# Wait for the (never recreated) database container to report healthy.
wait_db_healthy() {
  local deadline=$((SECONDS + TIMEOUT)) id
  log "waiting for the database to be healthy"
  while :; do
    id=$(service_id db)
    if [[ -n $id && $(state_of "$id") == running* && $(health_of "$id") == healthy ]]; then
      return 0
    fi
    if ((SECONDS >= deadline)); then
      log "database did not become healthy within ${TIMEOUT}s"
      return 1
    fi
    sleep "$HEALTH_POLL"
  done
}

# Wait for the one-shot migrate container to exit; require exit code 0.
wait_migrate() {
  local deadline=$((SECONDS + TIMEOUT)) id state
  log "waiting for migrations to finish"
  while :; do
    id=$(service_id migrate)
    state=$(state_of "$id")
    case $state in
      "exited 0")
        migrate_report "$id"
        return 0
        ;;
      exited* | dead*)
        migrate_report "$id"
        log "migrate container finished with '$state' (expected 'exited 0')"
        return 1
        ;;
    esac
    if ((SECONDS >= deadline)); then
      log "migrate did not finish within ${TIMEOUT}s (state: $state)"
      return 1
    fi
    sleep "$HEALTH_POLL"
  done
}

# Record (and print) whether the migrate container applied a migration.
migrate_report() { # [id]
  local id=${1:-} logs
  [[ -n $id ]] || id=$(service_id migrate)
  [[ -n $id ]] || return 0
  logs=$(dockerx logs --since "${START:-1970-01-01T00:00:00Z}" "$id" 2>&1 || true)
  if grep -Eq 'Applied ([0-9]+ )?migration' <<<"$logs"; then
    MIGRATED=1
    log "migrations applied: $(grep -E 'Applied ([0-9]+ )?migration' <<<"$logs" | tail -n 1 | sed 's/^.*Applied/Applied/')"
  elif grep -q 'No pending migrations' <<<"$logs"; then
    log "migrations: none pending"
  fi
}

# The new bot container must still be this container, running, with no restarts.
bot_alive() { # id restarts-at-start
  local id=$1 restarts0=$2 current state
  current=$(compose ps -q bot 2>/dev/null | head -n 1 || true)
  if [[ $current != "$id" ]]; then
    log "bot container was replaced while waiting"
    return 1
  fi
  state=$(state_of "$id")
  if [[ ${state%% *} != running ]]; then
    log "bot container is ${state%% *}"
    return 1
  fi
  if [[ $(restarts_of "$id") != "$restarts0" ]]; then
    log "bot container restarted"
    return 1
  fi
}

# Wait for the bot container $1 to connect to the Gateway, then require it to
# stay up and un-restarted for STABLE_SECONDS. Reads only this container's logs.
health_wait() { # id since-timestamp
  local id=$1 since=$2 deadline=$((SECONDS + TIMEOUT)) logs state restarts0 end
  restarts0=$(restarts_of "$id")
  log "waiting up to ${TIMEOUT}s for the bot to connect to Discord"
  while :; do
    logs=$(dockerx logs --since "$since" "$id" 2>&1 || true)
    if grep -q 'has connected to Gateway' <<<"$logs"; then
      break
    fi
    if grep -Eq 'Traceback|ExtensionFailed' <<<"$logs"; then
      log "bot logged a startup error"
      return 1
    fi
    state=$(state_of "$id")
    case ${state%% *} in
      restarting | exited | dead | missing)
        log "bot container is ${state%% *}"
        return 1
        ;;
    esac
    if ((SECONDS >= deadline)); then
      log "timed out after ${TIMEOUT}s without a Gateway connection"
      return 1
    fi
    sleep "$HEALTH_POLL"
  done
  log "bot connected; checking it stays up for ${STABLE_SECONDS}s"
  bot_alive "$id" "$restarts0" || return 1
  end=$((SECONDS + STABLE_SECONDS))
  while ((SECONDS < end)); do
    sleep "$STABLE_POLL"
    bot_alive "$id" "$restarts0" || return 1
  done
  log "bot is connected and stable"
}

# Build once, then start db (never recreated), migrate, and the bot, each
# explicitly scoped. $1 = 1 to sync slash commands with the bot start.
deploy_stack() { # sync
  local sync=$1
  STEP="build image"
  compose build bot || return 1 # migrate and bot share this one image
  STEP="start database"
  compose up -d --no-recreate db || return 1
  wait_db_healthy || return 1
  STEP="run migrations"
  START=$(utc_now)
  compose up -d --no-deps --force-recreate migrate || return 1
  wait_migrate || return 1
  STEP="start bot"
  START=$(utc_now)
  if ((sync)); then
    compose_sync up -d --no-deps --force-recreate bot || return 1
  else
    compose up -d --no-deps --force-recreate bot || return 1
  fi
  BOT_ID=$(service_id bot)
  [[ -n $BOT_ID ]] || {
    log "bot container not found after start"
    return 1
  }
  STEP="wait for bot health"
  health_wait "$BOT_ID" "$START" || return 1
}

# --- rollback and failure handling ----------------------------------------
# Return the checkout and containers to $1. Never touches the database.
rollback_to() { # sha
  local sha=$1
  log "returning the code to $(short "$sha")"
  if [[ $(git rev-parse HEAD) != "$sha" ]]; then
    git switch --detach "$sha" || return 1
  fi
  deploy_stack 0 || return 1
  record_deployed "$sha"
}

loud_migration_warning() {
  cat <<EOF
deploy: ################################################################
deploy: WARNING: A DATABASE MIGRATION WAS APPLIED DURING THIS DEPLOY.
deploy: A code rollback does NOT undo migrations. The old code may be
deploy: incompatible with the migrated schema. The database has NOT been
deploy: restored automatically. Backup taken just before this deploy:
deploy:   $BACKUP
deploy: To restore it, follow docs/DEPLOYMENT.md section 8 ("Restore production").
deploy: ################################################################
EOF
}

# Say exactly where things stand, so a failed run never ends silently.
report_state() {
  local branch bot
  branch=$(git symbolic-ref --short -q HEAD || true)
  bot=$(state_of "$(service_id bot)")
  log "checkout: ${branch:-detached HEAD} at $(git rev-parse HEAD)"
  log "bot container: ${bot%% *}"
  log "recorded running revision: $(deployed_sha || true)"
}

arm() {
  ARMED=1
  trap 'on_signal INT' INT
  trap 'on_signal TERM' TERM
  trap 'on_error $LINENO' ERR
}

disarm() {
  ARMED=0
  trap - INT TERM ERR
}

on_signal() { # name
  ((ARMED)) || exit 130
  fail_and_rollback "interrupted by SIG$1"
}

on_error() { # line
  [[ $BASHPID == "$MAIN_PID" ]] || return 0 # ignore failures inside $(...) subshells
  ((ARMED)) || return 0
  fail_and_rollback "unexpected command failure at line $1"
}

# Restore the revision that was running before this run, then exit 1. Runs at
# most once; traps are off (INT/TERM ignored) while it works.
fail_and_rollback() { # reason
  if ((ROLLING_BACK)); then exit 1; fi
  ROLLING_BACK=1
  ARMED=0
  trap '' INT TERM
  trap - ERR
  log "FAILED: $1${STEP:+ (during: $STEP)}"
  migrate_report || true # best effort: did a migration run?
  local result=failed-rolled-back
  if [[ $PREV == "$TARGET" ]]; then
    result=failed-restarted-in-place
    log "the running revision is the target itself; restarting it in place"
  fi
  if ! rollback_to "$PREV"; then
    result=failed-restore-also-failed
    warn "could not bring the previous revision back up; investigate immediately"
  fi
  log "last 40 bot log lines:"
  compose logs --no-color --tail=40 bot 2>&1 || true
  history_add "$ACTION" "$PREV" "$TARGET" "$result"
  if [[ $ACTION == deploy ]] && ((MIGRATED)); then loud_migration_warning; fi
  report_state
  log "result: $result"
  exit 1
}

# --- main flows -----------------------------------------------------------
do_deploy() {
  local branch bot
  branch=$(default_branch)
  preflight
  resolve_prev # before ensure_branch: the checkout as found is the fallback
  ensure_branch "$branch"

  log "fetching origin"
  git fetch --prune origin
  TARGET=$(git rev-parse "origin/$branch")
  if [[ $PREV == "$TARGET" ]] && ((!FORCE)); then
    bot=$(state_of "$(service_id bot)")
    if [[ ${bot%% *} == running ]]; then
      log "Already up to date at $(short "$TARGET")"
      exit 0
    fi
    log "running revision is current but the bot container is ${bot%% *}; redeploying"
  fi
  git merge-base --is-ancestor HEAD "$TARGET" ||
    die "local $branch has commits that are not on origin/$branch; cannot fast-forward"
  log "deploying $(short "$PREV") -> $(short "$TARGET")"

  if ((SKIP_CI)); then warn "skipping the CI check"; else ci_gate "$TARGET"; fi

  make_backup pre-deploy

  # Merge the exact commit CI validated rather than whatever origin has now.
  git merge --ff-only "$TARGET" || {
    history_add deploy "$PREV" "$TARGET" pull-failed
    die "fast-forward failed; nothing was deployed (backup kept at $BACKUP)"
  }

  arm # from here a failure, Ctrl-C or SIGTERM restores $PREV
  START=$(utc_now)
  if ((SYNC)); then
    log "deploying with a one-time slash command sync"
  else
    log "deploying without a command sync"
  fi
  deploy_stack "$SYNC" || fail_and_rollback "deploy step failed"

  if ((SYNC)); then SYNCED=yes; fi
  record_deployed "$TARGET"
  disarm
  history_add deploy "$PREV" "$TARGET" ok
  summary "deployed:"
}

do_rollback() {
  local branch rev
  ACTION=rollback
  branch=$(default_branch)
  preflight
  resolve_prev
  rev=${ROLLBACK_REV:-$(last_good_previous)}
  [[ -n $rev ]] || die "no successful deploy recorded in $HISTORY; pass a revision: --rollback REV"
  TARGET=$(git rev-parse --verify --quiet "$rev^{commit}") || die "unknown revision: $rev"
  log "rolling back $(short "$PREV") -> $(short "$TARGET")"

  make_backup pre-rollback
  arm # a failed or interrupted rollback restores $PREV
  STEP="rollback"
  rollback_to "$TARGET" || fail_and_rollback "rollback did not bring the bot back up"
  disarm
  history_add rollback "$PREV" "$TARGET" ok
  warn "a code rollback does not undo database migrations; see docs/DEPLOYMENT.md section 8"
  log "the checkout is detached; the next 'bash scripts/deploy.sh' switches back to $branch"
  summary "rolled back to:"
}

summary() { # label for the final sha line
  local migrated=no
  if ((MIGRATED)); then migrated=yes; fi
  log "----------------------------------------------------------------"
  log "$(printf '%-20s' "$1")$(git rev-parse HEAD) ($(git log -1 --format=%s))"
  log "$(printf '%-20s' "backup:")$BACKUP"
  log "$(printf '%-20s' "commands synced:")$SYNCED"
  log "$(printf '%-20s' "migrations applied:")$migrated"
  log "----------------------------------------------------------------"
}

main() {
  parse_args "$@"
  cd "$(dirname "${BASH_SOURCE[0]}")/.."
  git rev-parse --is-inside-work-tree >/dev/null 2>&1 || die "not inside the catan-tracker git checkout"
  if ((!ROLLBACK && !SKIP_CI)); then
    command -v python3 >/dev/null 2>&1 || die "python3 is required for the CI check (or use --skip-ci-check)"
  fi
  if ((ROLLBACK)); then do_rollback; else do_deploy; fi
}

main "$@"
