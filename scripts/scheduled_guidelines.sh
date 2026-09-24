#!/bin/bash
# Scheduled guideline check: runs monthly (cron, 1st of the month, 3 AM).
#
# Runs scripts/update_guidelines.py: every author-guideline page behind
# manuscript_requirements.json is re-read without an agent, and only the venues
# whose pages changed (or moved, or could not be vouched for in six months) get
# a Claude Code agent (local subscription, no API key) to update their entry.
# The result is a pull request on GitHub, never a push to main.
#
# Logs go to .guideline_cache/logs/; one run at a time (a second one that fires
# while the first is still researching exits quietly).

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
LOG_DIR="$PROJECT_DIR/.guideline_cache/logs"
mkdir -p "$LOG_DIR"

# cron starts with a minimal PATH (/usr/bin:/bin), which lacks the `claude` CLI
# (~/.local/bin); `gh` is in /usr/bin. No display either: the checker re-runs
# itself under xvfb-run, since several publishers refuse headless browsers.
export PATH="$HOME/.local/bin:$PATH"
export PYTHONUNBUFFERED=1
unset DISPLAY

exec 9>"$LOG_DIR/.scheduled_guidelines.lock"
flock -n 9 || exit 0

TIMESTAMP=$(date +%Y-%m-%d_%H-%M-%S)
LOG_FILE="$LOG_DIR/guidelines_${TIMESTAMP}.log"

run() {
  echo "=== Scheduled Guideline Check Start: $TIMESTAMP ==="
  echo "Project: $PROJECT_DIR"
  echo ""

  for tool in claude gh xvfb-run; do
    command -v "$tool" >/dev/null || { echo "ERROR: '$tool' not found on PATH ($PATH)"; return 1; }
  done

  eval "$(conda shell.bash hook)"
  conda activate paperpush || return 1
  cd "$PROJECT_DIR" || return 1

  python scripts/update_guidelines.py "$@"
}

run "$@" >> "$LOG_FILE" 2>&1
STATUS=$?
if [ "$STATUS" -eq 0 ]; then
  echo "=== Scheduled Guideline Check Complete: $(date +%Y-%m-%d_%H-%M-%S) ===" >> "$LOG_FILE"
else
  echo "=== Scheduled Guideline Check FAILED (exit $STATUS): $(date +%Y-%m-%d_%H-%M-%S) ===" >> "$LOG_FILE"
fi
# Keep the last twelve logs.
ls -1t "$LOG_DIR"/guidelines_*.log 2>/dev/null | tail -n +13 | xargs -r rm -f
exit "$STATUS"
