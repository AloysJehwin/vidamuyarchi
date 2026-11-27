#!/usr/bin/env bash
#
# linkedin_install_mac.sh — install the daily runner as a macOS launchd job
# =========================================================================
# Schedules scripts/linkedin_daily.sh to run once a day on this Mac (which
# already holds a trusted LinkedIn session, so no re-login is needed — unlike
# the fresh Razer box). Runs only while the Mac is awake; if it was asleep at
# the scheduled time, launchd fires it at the next wake.
#
# !!! NEVER run this alongside the Razer cron: two runners double the LinkedIn
# !!! invites on one account. LINKEDIN_SHEETS_SYNC=0 keeps it off the Google Sheet.
#
# Usage:
#   bash scripts/linkedin_install_mac.sh          # install + load
#   bash scripts/linkedin_install_mac.sh --remove # unload + remove
#
# Logs: ~/linkedin_daily.log   (stdout/stderr of every run)
set -uo pipefail

LABEL="com.jeffi.linkedin.daily"
PLIST="$HOME/Library/LaunchAgents/${LABEL}.plist"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
RUNNER="$SCRIPT_DIR/linkedin_daily.sh"
PYTHON="$(command -v python3)"
LOG="$HOME/linkedin_daily.log"

# Run hour:minute — 09:07 local (inside 08–22 send-hours, off the round mark).
HOUR=9
MINUTE=7

if [ "${1:-}" = "--remove" ]; then
    launchctl unload "$PLIST" 2>/dev/null || true
    rm -f "$PLIST"
    echo "Removed launchd job $LABEL"
    exit 0
fi

mkdir -p "$HOME/Library/LaunchAgents"

cat > "$PLIST" <<PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${LABEL}</string>

    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>${RUNNER}</string>
    </array>

    <!-- Mac already has a trusted session — run HEADED so LinkedIn sees a
         normal browser (headless is more detectable). A visible Chrome window
         will briefly appear during the run. -->
    <key>EnvironmentVariables</key>
    <dict>
        <key>HEADED</key>
        <string>1</string>
        <key>PYTHON</key>
        <string>${PYTHON}</string>
        <key>PATH</key>
        <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
        <key>LINKEDIN_SHEETS_SYNC</key>
        <string>0</string>
    </dict>

    <key>WorkingDirectory</key>
    <string>${REPO_DIR}</string>

    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key>
        <integer>${HOUR}</integer>
        <key>Minute</key>
        <integer>${MINUTE}</integer>
    </dict>

    <key>StandardOutPath</key>
    <string>${LOG}</string>
    <key>StandardErrorPath</key>
    <string>${LOG}</string>

    <!-- Don't fire on load, only at the scheduled time. -->
    <key>RunAtLoad</key>
    <false/>
</dict>
</plist>
PLISTEOF

launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"

echo "Installed launchd job: $LABEL"
echo "  Runner:  $RUNNER"
echo "  Python:  $PYTHON"
echo "  Daily:   ${HOUR}:$(printf '%02d' "$MINUTE") local"
echo "  Log:     $LOG"
echo
echo "Verify:   launchctl list | grep linkedin"
echo "Run now:  launchctl start $LABEL   (then: tail -f $LOG)"
echo "Remove:   bash $SCRIPT_DIR/linkedin_install_mac.sh --remove"

cat >&2 <<'WARNEOF'

!!! ═══════════════════════════════════════════════════════════════════════
!!!  WARNING: the Razer cron runs linkedin_daily.sh every day.
!!!  NEVER keep this Mac job loaded while that cron is active. Each machine
!!!  counts the invite caps in its own ~/linkedin_outreach.db, so two runners
!!!  DOUBLE the LinkedIn invites on one account (restriction risk).
!!!  This job never syncs the Google Sheet (LINKEDIN_SHEETS_SYNC=0).
!!!  If the Razer cron is on, remove this job now (--remove, see above).
!!! ═══════════════════════════════════════════════════════════════════════
WARNEOF
