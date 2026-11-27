#!/usr/bin/env bash
#
# linkedin_setup_razer.sh — one-time bootstrap for the daily LinkedIn runner
# =========================================================================
# Razer starts bare: no Chrome, no Python deps, no LinkedIn session. Run this
# ONCE on the Razer box to install everything, then verify the transferred
# credentials, then (after an interactive first run) enable cron.
#
# Usage (on Razer):
#   bash scripts/linkedin_setup_razer.sh
set -uo pipefail

log() { echo "[setup] $*"; }
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# ── 1. Install Google Chrome (undetected-chromedriver needs real Chrome) ─────
if command -v google-chrome >/dev/null 2>&1; then
    log "google-chrome already installed: $(google-chrome --version)"
else
    log "Installing google-chrome-stable…"
    TMPDEB=$(mktemp --suffix=.deb)
    wget -q -O "$TMPDEB" \
        https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb
    sudo apt-get update -qq
    sudo apt-get install -y "$TMPDEB" || sudo apt-get install -yf
    rm -f "$TMPDEB"
    log "Chrome installed: $(google-chrome --version 2>&1)"
fi

# ── 1b. System libs for headed-in-Xvfb Chrome + pyautogui keyboard input ─────
# The connect flow uses pyautogui (Tab×3+Enter to hit "Send without a note"),
# which on Linux needs an X display plus python-xlib, scrot and tk to import.
# The cron lines printed below run linkedin_daily.sh with HEADED=1 under
# xvfb-run, so Chrome is headed inside a virtual display and DISPLAY is set.
log "Installing Xvfb + pyautogui X backend deps…"
sudo apt-get install -y -qq xvfb scrot python3-tk python3-dev 2>&1 | tail -1 || true

# ── 2. Python dependencies (user site — matches the repo's other tooling) ────
log "Installing Python dependencies…"
pip install --user -q \
    undetected-chromedriver selenium openpyxl \
    google-api-python-client google-auth \
    psycopg2-binary python-dotenv \
    pyautogui python-xlib \
    && log "Python deps installed" \
    || log "⚠️  pip install reported an issue — check output above"

# ── 3. Verify transferred credential / session artifacts ─────────────────────
log "Checking required credential files (copy these from the Mac if missing)…"
MISSING=0
check() {
    if [ -e "$1" ]; then
        log "  ✔ $1"
    else
        log "  ✖ MISSING: $1"
        MISSING=1
    fi
}
check "$HOME/.linkedin_outreach_cookies.json"
check "$HOME/.linkedin_outreach.env"
check "$HOME/.linkedin_gsheets.json"
check "$HOME/.linkedin_outreach_chrome"

if [ "$MISSING" -eq 1 ]; then
    cat <<'EOF'

[setup] ── Copy the missing artifacts from the Mac (run these ON THE MAC) ──
    scp ~/.linkedin_outreach_cookies.json  razer-gpu:~/
    scp ~/.linkedin_outreach.env           razer-gpu:~/
    scp ~/.linkedin_gsheets.json           razer-gpu:~/
    scp -r ~/.linkedin_outreach_chrome     razer-gpu:~/

EOF
fi

# ── 4. Print the interactive-first-run warning + cron lines ──────────────────
cat <<EOF

[setup] ════════════════════════════════════════════════════════════════════
[setup] NEXT STEPS
[setup] ════════════════════════════════════════════════════════════════════
[setup]
[setup] 1. SMOKE TEST (zero LinkedIn risk — just DB → Google Sheet):
[setup]      cd $REPO_DIR
[setup]      LINKEDIN_SHEETS_SYNC=1 HEADLESS=1 python3 src/linkedin/linkedin_outreach.py --sync-sheets
[setup]
[setup] 2. SESSION TEST (confirms the transferred LinkedIn cookies survived):
[setup]      HEADLESS=1 python3 src/linkedin/linkedin_outreach.py --sync-connections
[setup]    ⚠️  Headless Chrome on Linux has a different fingerprint than the Mac.
[setup]        If this hits a LinkedIn checkpoint / re-login, run once WITH a
[setup]        display (X-forward or VNC) to re-establish the session:
[setup]          python3 src/linkedin/linkedin_outreach.py --manual-login
[setup]
[setup] 3. FULL RUNNER (watch it live the first time):
[setup]      bash scripts/linkedin_daily.sh
[setup]
[setup] 4. ENABLE CRON (only after step 3 is clean) — add via 'crontab -e' (IST):
[setup]      3 9 * * * cd $REPO_DIR && LINKEDIN_SHEETS_SYNC=1 HEADED=1 /usr/bin/xvfb-run -a --server-args='-screen 0 1280x900x24' /bin/bash scripts/linkedin_daily.sh >> \$HOME/linkedin_daily.log 2>&1
[setup]      40 15 * * * cd $REPO_DIR && HARVEST_ONLY=1 LINKEDIN_SHEETS_SYNC=1 HEADED=1 /usr/bin/xvfb-run -a --server-args='-screen 0 1280x900x24' /bin/bash scripts/linkedin_daily.sh >> \$HOME/linkedin_daily.log 2>&1
[setup]    Delete any other linkedin_daily.sh line (e.g. an older copy of the runner)
[setup]    and keep the Mac launchd job off: only these two lines may drive the account.
[setup]
[setup] ════════════════════════════════════════════════════════════════════
EOF

log "Bootstrap complete."
