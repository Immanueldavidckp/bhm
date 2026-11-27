#!/usr/bin/env bash
# BHM continuous deployment, server side (Oracle VM forex-trader).
#
# Run every minute by the user timer bhm-deploy.timer (from the installed copy
# ~/bhm/bin/bhm-deploy.sh, see install.sh). It:
#   1. fetches main from GitHub into the clone ~/bhm/repo
#   2. if main has a new commit, waits until the GitHub Actions CI for it is green
#   3. copies repo/server-dev/ onto the live folder ~/bhm/server-dev/
#      (never touches .env, secrets/, certs/, mosquitto/passwd, backups)
#   4. rebuilds / restarts only the docker services whose files changed
#
# Anybody who pushes to main (web edit on GitHub or git push) gets deployed this way.
#
#   bhm-deploy.sh            normal run (what the timer does)
#   bhm-deploy.sh --force    deploy origin/main now, skip the CI check
#   bhm-deploy.sh --status   show deployed commit vs. main
set -euo pipefail

REPO_URL=${BHM_REPO_URL:-https://github.com/Immanueldavidckp/bhm.git}
GH_API=${BHM_GH_API:-https://api.github.com/repos/Immanueldavidckp/bhm}
BRANCH=${BHM_BRANCH:-main}
BASE=${BHM_BASE:-$HOME/bhm}
REPO=$BASE/repo
LIVE=$BASE/server-dev
STATE=$BASE/deploy-state
PROJECT=bhm
# a commit with no CI result at all after this many seconds is deployed anyway
NO_CI_GRACE=${BHM_NO_CI_GRACE:-900}

log() { echo "[bhm-deploy] $*"; }
mkdir -p "$STATE"

exec 9>"$STATE/lock"
flock -n 9 || { log "another deploy is running"; exit 0; }

if [ ! -d "$REPO/.git" ]; then
    log "cloning $REPO_URL -> $REPO"
    git clone --quiet --branch "$BRANCH" "$REPO_URL" "$REPO"
fi

git -C "$REPO" fetch --quiet origin "$BRANCH"
NEW=$(git -C "$REPO" rev-parse "origin/$BRANCH")
OLD=$(cat "$STATE/deployed" 2>/dev/null || true)

if [ "${1:-}" = "--status" ]; then
    echo "deployed: ${OLD:-none}"
    echo "main:     $NEW  $(git -C "$REPO" log -1 --format='%an, %ar: %s' "$NEW")"
    exit 0
fi

if [ -z "$OLD" ]; then
    # first run: take the current main as the baseline, deploy nothing
    echo "$NEW" > "$STATE/deployed"
    log "baseline set to $NEW (nothing deployed); next new commit on $BRANCH will deploy"
    exit 0
fi

[ "$NEW" = "$OLD" ] && [ "${1:-}" != "--force" ] && exit 0

# ---- wait for CI ----------------------------------------------------------
if [ "${1:-}" != "--force" ]; then
    CI=$(curl -fsS -H 'Accept: application/vnd.github+json' \
            "$GH_API/commits/$NEW/check-runs?per_page=50" 2>/dev/null \
        | python3 -c '
import json, sys
runs = json.load(sys.stdin).get("check_runs", [])
if not runs: print("none")
elif any(r["status"] != "completed" for r in runs): print("pending")
elif all(r["conclusion"] in ("success", "skipped", "neutral") for r in runs): print("success")
else: print("failure")
' 2>/dev/null || echo "unknown")
    case "$CI" in
        success) ;;
        pending|unknown) log "CI for ${NEW:0:7} is $CI, waiting"; exit 0 ;;
        failure)
            if [ "$(cat "$STATE/failed" 2>/dev/null)" != "$NEW" ]; then
                log "CI FAILED for ${NEW:0:7}, not deploying (server stays on ${OLD:0:7})"
                echo "$NEW" > "$STATE/failed"
            fi
            exit 0 ;;
        none)
            AGE=$(( $(date +%s) - $(git -C "$REPO" log -1 --format=%ct "$NEW") ))
            if [ "$AGE" -lt "$NO_CI_GRACE" ]; then log "no CI result yet for ${NEW:0:7}, waiting"; exit 0; fi
            log "no CI ran for ${NEW:0:7} after ${AGE}s, deploying anyway" ;;
    esac
fi

# ---- copy files -----------------------------------------------------------
git -C "$REPO" checkout --quiet --force "$NEW"
log "deploying ${OLD:0:7} -> ${NEW:0:7}: $(git -C "$REPO" log -1 --format='%an: %s' "$NEW")"

EXCLUDES=(--exclude '.env' --exclude 'secrets/' --exclude 'certs/' --exclude 'mosquitto/passwd'
          --exclude 'backup-*/' --exclude '__pycache__/' --exclude 'deploy/')
BACKUP=$LIVE/backup-auto-$(date +%Y%m%d-%H%M%S)-${OLD:0:7}

CHANGED=$(rsync -rlpc --itemize-changes --dry-run "${EXCLUDES[@]}" "$REPO/server-dev/" "$LIVE/" \
          | awk '$1 ~ /^>f/ {print $2}')
if [ -z "$CHANGED" ]; then
    log "no server-dev file changed, nothing to restart"
    echo "$NEW" > "$STATE/deployed"
    exit 0
fi
log "changed: $(echo $CHANGED)"

rsync -rlpc --backup --backup-dir="$BACKUP" "${EXCLUDES[@]}" "$REPO/server-dev/" "$LIVE/"
# keep the 10 newest automatic backups
ls -dt "$LIVE"/backup-auto-* 2>/dev/null | tail -n +11 | xargs -r rm -rf

# ---- restart what changed -------------------------------------------------
BUILD=() RESTART=()
ALL=0
for f in $CHANGED; do
    case "$f" in
        docker-compose.yml)          ALL=1 ;;
        dashboard.html|api/*)        BUILD+=(dashboard) ;;
        ingester/*)                  BUILD+=(ingester) ;;
        mosquitto/*)                 RESTART+=(mosquitto) ;;
        *) ;;   # db/ init scripts, shell helpers: no restart needed
    esac
done

cd "$LIVE"
read -ra DC <<< "${BHM_DOCKER:-sudo -n docker} compose -p $PROJECT"
if [ "$ALL" = 1 ]; then
    "${DC[@]}" up -d --build
else
    if [ ${#BUILD[@]} -gt 0 ]; then
        mapfile -t BUILD < <(printf '%s\n' "${BUILD[@]}" | sort -u)
        "${DC[@]}" up -d --build "${BUILD[@]}"
    fi
    if [ ${#RESTART[@]} -gt 0 ]; then "${DC[@]}" restart mosquitto; fi
fi

# ---- health check ---------------------------------------------------------
sleep 5
CODE=$(curl -s -o /dev/null -w '%{http_code}' "${BHM_HEALTH_URL:-http://127.0.0.1:8090/}" || true)
case "$CODE" in
    200|301|302|401) log "dashboard answers HTTP $CODE - deployed ${NEW:0:7} OK (backup: $BACKUP)" ;;
    *) log "WARNING dashboard answers HTTP $CODE after deploy; old files are in $BACKUP" ;;
esac
echo "$NEW" > "$STATE/deployed"
rm -f "$STATE/failed"
if ! cmp -s "$0" "$REPO/server-dev/deploy/bhm-deploy.sh"; then
    log "NOTE deploy/bhm-deploy.sh changed in the repo; review it and run ~/bhm/repo/server-dev/deploy/install.sh"
fi
