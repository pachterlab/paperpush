#!/bin/bash
# Scheduled submit walkthrough: meant to run every three months.
#
# Runs scripts/check_submit_walkthrough.py against a fresh checkout of
# origin/main (.submit_walkthrough/worktree, detached), so it tests what users
# get and the maintainer's working tree is never touched. The walkthrough runs
# the real `paperpush submit` for every venue with a runner, through every page,
# and stops before the final submit button, reusing the saved logins in
# paperpush's config directory. Optional PAPERPUSH_TEST_USERNAME /
# PAPERPUSH_TEST_PASSWORD can go in the gitignored .submit_walkthrough/env.
# When a venue fails all three attempts, an issue labelled `submit-walkthrough`
# is opened on GitHub (or commented on, if one is open).
#
# Browsers run headed (fewer portals bot-wall them): in the desktop session on
# macOS, under xvfb-run on Linux when there is no display, and headless only
# when neither is available. Each run leaves an unsent draft on every portal.
#
# Logs go to .submit_walkthrough/logs/; one run at a time.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
STATE_DIR="$PROJECT_DIR/.submit_walkthrough"
LOG_DIR="$STATE_DIR/logs"
WORKTREE="$STATE_DIR/worktree"
mkdir -p "$LOG_DIR"

# A scheduler starts with a minimal PATH (/usr/bin:/bin); `gh` and `git` may live
# in /usr/local/bin or /opt/homebrew/bin (macOS), and conda in ~/miniconda3 etc.
export PATH="$HOME/.local/bin:/usr/local/bin:/opt/homebrew/bin:$PATH"
for d in "$HOME/miniconda3" "$HOME/anaconda3" "$HOME/miniforge3" "$HOME/mambaforge" /opt/miniconda3 /opt/anaconda3; do
  [ -x "$d/bin/conda" ] && { export PATH="$PATH:$d/bin"; break; }
done
export PYTHONUNBUFFERED=1

# One run at a time. flock where it exists (Linux); macOS has no flock, so
# fall back to an atomic mkdir lock, cleared if its holder is gone.
if command -v flock >/dev/null; then
  exec 9>"$LOG_DIR/.scheduled_submit_walkthrough.lock"
  flock -n 9 || exit 0
else
  LOCK_DIR="$LOG_DIR/.scheduled_submit_walkthrough.lockdir"
  if ! mkdir "$LOCK_DIR" 2>/dev/null; then
    kill -0 "$(cat "$LOCK_DIR/pid" 2>/dev/null)" 2>/dev/null && exit 0
    rm -rf "$LOCK_DIR"; mkdir "$LOCK_DIR" || exit 0
  fi
  echo $$ > "$LOCK_DIR/pid"
  trap 'rm -rf "$LOCK_DIR"' EXIT
fi

TIMESTAMP=$(date +%Y-%m-%d_%H-%M-%S)
LOG_FILE="$LOG_DIR/submit_walkthrough_${TIMESTAMP}.log"

run() {
  echo "=== Scheduled Submit Walkthrough Start: $TIMESTAMP ==="
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
  if [ -e "$WORKTREE/.git" ]; then
    git -C "$WORKTREE" checkout -q --detach origin/main || return 1
    git -C "$WORKTREE" reset -q --hard origin/main || return 1
    git -C "$WORKTREE" clean -qfdx || return 1
  else
    git worktree prune
    git worktree add -q --detach "$WORKTREE" origin/main || return 1
  fi

  local cmd=(python "$WORKTREE/scripts/check_submit_walkthrough.py" --repo-dir "$WORKTREE" --out-dir "$STATE_DIR")
  if [ "$(uname)" != "Darwin" ] && [ -z "$DISPLAY" ]; then
    if command -v xvfb-run >/dev/null; then
      cmd=(xvfb-run -a "${cmd[@]}")
    else
      echo "no display and no xvfb-run: running headless"
      cmd+=(--headless)
    fi
  fi
  "${cmd[@]}" "$@"
}

run "$@" >> "$LOG_FILE" 2>&1
STATUS=$?
if [ "$STATUS" -eq 0 ]; then
  echo "=== Scheduled Submit Walkthrough Complete: $(date +%Y-%m-%d_%H-%M-%S) ===" >> "$LOG_FILE"
else
  echo "=== Scheduled Submit Walkthrough FAILED (exit $STATUS): $(date +%Y-%m-%d_%H-%M-%S) ===" >> "$LOG_FILE"
fi
# Keep the last twelve logs.
ls -1t "$LOG_DIR"/submit_walkthrough_*.log 2>/dev/null | tail -n +13 | xargs -r rm -f
exit "$STATUS"
