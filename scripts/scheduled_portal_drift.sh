#!/bin/bash
# Scheduled portal-drift check: runs monthly (cron, 1st of the month, 5 AM,
# after scheduled_guidelines.sh).
#
# Runs scripts/check_portal_drift.py against a fresh checkout of origin/main
# (.portal_drift/worktree, detached), so it tests what users get and the
# maintainer's working tree is never touched. The drift tests open each venue's
# real portal headless, reusing the saved sessions in paperpush's config
# directory (or PAPERPUSH_TEST_USERNAME / PAPERPUSH_TEST_PASSWORD, which can be
# put in the gitignored .portal_drift/env). When a portal changed, an issue
# labelled `portal-drift` is opened on GitHub (or commented on, if one is open).
#
# Note: test_first_step_drift reaches the first wizard step, which leaves a
# clearly titled dummy draft on that portal each run.
#
# Logs go to .portal_drift/logs/; one run at a time.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
STATE_DIR="$PROJECT_DIR/.portal_drift"
LOG_DIR="$STATE_DIR/logs"
WORKTREE="$STATE_DIR/worktree"
mkdir -p "$LOG_DIR"

# cron starts with a minimal PATH (/usr/bin:/bin); `gh` and `git` may live in
# /usr/local/bin or /opt/homebrew/bin (macOS), and conda in ~/miniconda3 etc.
export PATH="$HOME/.local/bin:/usr/local/bin:/opt/homebrew/bin:$PATH"
for d in "$HOME/miniconda3" "$HOME/anaconda3" "$HOME/miniforge3" "$HOME/mambaforge" /opt/miniconda3 /opt/anaconda3; do
  [ -x "$d/bin/conda" ] && { export PATH="$PATH:$d/bin"; break; }
done
export PYTHONUNBUFFERED=1

# One run at a time. flock where it exists (Linux); macOS has no flock, so
# fall back to an atomic mkdir lock, cleared if its holder is gone.
if command -v flock >/dev/null; then
  exec 9>"$LOG_DIR/.scheduled_portal_drift.lock"
  flock -n 9 || exit 0
else
  LOCK_DIR="$LOG_DIR/.scheduled_portal_drift.lockdir"
  if ! mkdir "$LOCK_DIR" 2>/dev/null; then
    kill -0 "$(cat "$LOCK_DIR/pid" 2>/dev/null)" 2>/dev/null && exit 0
    rm -rf "$LOCK_DIR"; mkdir "$LOCK_DIR" || exit 0
  fi
  echo $$ > "$LOCK_DIR/pid"
  trap 'rm -rf "$LOCK_DIR"' EXIT
fi

TIMESTAMP=$(date +%Y-%m-%d_%H-%M-%S)
LOG_FILE="$LOG_DIR/portal_drift_${TIMESTAMP}.log"

run() {
  echo "=== Scheduled Portal Drift Check Start: $TIMESTAMP ==="
  echo "Project: $PROJECT_DIR"
  echo ""

  for tool in git gh conda; do
    command -v "$tool" >/dev/null || { echo "ERROR: '$tool' not found on PATH ($PATH)"; return 1; }
  done

  if [ -f "$STATE_DIR/env" ]; then
    set -a; . "$STATE_DIR/env"; set +a
  fi

  eval "$(conda shell.bash hook)"
  conda activate paperpush || return 1
  cd "$PROJECT_DIR" || return 1

  git fetch -q origin main || return 1
  if [ -d "$WORKTREE/.git" ] || [ -f "$WORKTREE/.git" ]; then
    git -C "$WORKTREE" checkout -q --detach origin/main || return 1
    git -C "$WORKTREE" reset -q --hard origin/main || return 1
    git -C "$WORKTREE" clean -qfdx -e tests/portal_snapshots/_failures/ || return 1
  else
    git worktree prune
    git worktree add -q --detach "$WORKTREE" origin/main || return 1
  fi

  python "$WORKTREE/scripts/check_portal_drift.py" --repo-dir "$WORKTREE" --out-dir "$STATE_DIR" "$@"
}

run "$@" >> "$LOG_FILE" 2>&1
STATUS=$?
if [ "$STATUS" -eq 0 ]; then
  echo "=== Scheduled Portal Drift Check Complete: $(date +%Y-%m-%d_%H-%M-%S) ===" >> "$LOG_FILE"
else
  echo "=== Scheduled Portal Drift Check FAILED (exit $STATUS): $(date +%Y-%m-%d_%H-%M-%S) ===" >> "$LOG_FILE"
fi
# Keep the last twelve logs.
ls -1t "$LOG_DIR"/portal_drift_*.log 2>/dev/null | tail -n +13 | xargs -r rm -f
exit "$STATUS"
