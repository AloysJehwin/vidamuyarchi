#!/usr/bin/env bash
#
# linkedin_daily.sh — daily LinkedIn outreach runner (all phases)
# ================================================================
# Sequences the full funnel in src/linkedin/linkedin_outreach.py, each phase in
# its own isolated invocation (fresh browser per phase), with human-like gaps.
# Phase failures are logged and skipped — one broken phase never aborts the rest.
#
# The Python script enforces ALL account-safety caps itself (invites: jittered
# 10–15/day under DAILY_LIMIT 15 and WEEKLY_LIMIT 90 per rolling week; 20 first
# DMs/day, no auto-replies (replies are answered by hand); 25 comments/day;
# 3 search pages/day shared by harvest and connect, spreading the free
# account's monthly search limit over the month; 30 similar-profile visits/day;
# 08–22 send-hours). This runner only orders the phases and paces them; it does not
# re-implement limits.
#
# HARVEST_ONLY=1 runs just the harvest, the sheet sync and a short summary.
#
# INSTALL (cron, on Razer, IST). Google Sheets sync needs LINKEDIN_SHEETS_SYNC=1,
# which only these Razer lines set (never the Mac job or a bare manual run):
#   3 9 * * * cd $HOME/vidamuyarchi && LINKEDIN_SHEETS_SYNC=1 HEADED=1 /usr/bin/xvfb-run -a --server-args='-screen 0 1280x900x24' /bin/bash scripts/linkedin_daily.sh >> $HOME/linkedin_daily.log 2>&1
#   40 15 * * * cd $HOME/vidamuyarchi && HARVEST_ONLY=1 LINKEDIN_SHEETS_SYNC=1 HEADED=1 /usr/bin/xvfb-run -a --server-args='-screen 0 1280x900x24' /bin/bash scripts/linkedin_daily.sh >> $HOME/linkedin_daily.log 2>&1
#
# Headless unless HEADED=1. The cron sets HEADED=1 under xvfb-run so Chrome runs
# headed in a virtual display (the connect flow's pyautogui needs an X display).
set -uo pipefail

# ── Locate repo + python regardless of cron's minimal env ────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
PY_SCRIPT="$REPO_DIR/src/linkedin/linkedin_outreach.py"
PYTHON="${PYTHON:-python3}"
LOG="${LINKEDIN_DAILY_LOG:-$HOME/linkedin_daily.log}"
ENGINE_DB="$HOME/outreach/state.db"

if [ "${HEADED:-0}" != "1" ]; then
    export HEADLESS=1
fi

# Ollama runs on this box (Razer hosts it). Use localhost so comment/reply
# generation doesn't take a Tailscale round-trip and doesn't fall back to
# canned text if Tailscale hiccups. Override by exporting OLLAMA_URL before
# invoking, e.g. when running the daily script from a machine without Ollama.
export OLLAMA_URL="${OLLAMA_URL:-http://localhost:11434}"

cd "$REPO_DIR" || { echo "cannot cd to repo $REPO_DIR"; exit 1; }

# ── WhatsApp notify (Meta Cloud API, direct) ─────────────────────────────────
# Reads config from ~/.linkedin_wa.env (gitignored). Silently skips if absent,
# so the run never breaks when WhatsApp isn't configured.
#   WA_TOKEN=<Meta access token>
#   WA_PHONE_ID=<sender phone_number_id>
#   WA_TO=<your recipient number, e.g. 9198xxxxxxx — country code, no +>
WA_ENV="${LINKEDIN_WA_ENV:-$HOME/.linkedin_wa.env}"

notify_whatsapp() {
    # notify_whatsapp "message text"
    local msg="$1"
    [ -f "$WA_ENV" ] || { log "   (WhatsApp skipped — no $WA_ENV)"; return 0; }
    # shellcheck disable=SC1090
    source "$WA_ENV"
    if [ -z "${WA_TOKEN:-}" ] || [ -z "${WA_PHONE_ID:-}" ] || [ -z "${WA_TO:-}" ]; then
        log "   (WhatsApp skipped — WA_TOKEN/WA_PHONE_ID/WA_TO not all set in $WA_ENV)"
        return 0
    fi
    # Escape the message for JSON (newlines + quotes).
    local esc
    esc=$(printf '%s' "$msg" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))')
    local http
    http=$(curl -s -o /tmp/wa_resp.json -w '%{http_code}' \
        -X POST "https://graph.facebook.com/v21.0/${WA_PHONE_ID}/messages" \
        -H "Authorization: Bearer ${WA_TOKEN}" \
        -H "Content-Type: application/json" \
        -d "{\"messaging_product\":\"whatsapp\",\"to\":\"${WA_TO}\",\"type\":\"text\",\"text\":{\"body\":${esc}}}" \
        2>/dev/null)
    if [ "$http" = "200" ]; then
        log "   ✔ WhatsApp update sent"
    else
        log "   ⚠️  WhatsApp send failed (HTTP $http): $(cat /tmp/wa_resp.json 2>/dev/null | head -c 200)"
    fi
}

# Build a one-line-per-metric summary of TODAY's activity from the SQLite DBs
# (both opened read-only). daily_summary full|harvest
daily_summary() {
    "$PYTHON" - "$1" "$HOME/linkedin_outreach.db" "$ENGINE_DB" << 'PYEOF' 2>/dev/null || echo "summary unavailable"
import sqlite3, sys, datetime
from pathlib import Path
mode, db, engine_db = sys.argv[1:4]
today = datetime.date.today().isoformat()
def ro(path):
    return sqlite3.connect(Path(path).as_uri() + "?mode=ro", uri=True, timeout=30).cursor()
c = ro(db)
def n(q, *a, cur=c, fail=0):
    try: return cur.execute(q, a).fetchone()[0]
    except Exception: return fail
hq = "SELECT COUNT(*) FROM outreach WHERE source='harvest' AND date(created_at,'localtime')=?"
h_all = n(hq, today)
h_india = n(hq + " AND search_geo='india'", today)
h_global = n(hq + " AND search_geo='global'", today)
h_similar = n("SELECT COUNT(*) FROM outreach WHERE source='similar' AND date(created_at,'localtime')=?", today)
s_visits = n("SELECT COUNT(*) FROM similar_seed WHERE visited_at LIKE ?", today + "%")
harvested = (f"{h_all + h_similar} (search: India {h_india} / global {h_global}; "
             f"similar profiles: {h_similar} from {s_visits} visits)")
total     = n("SELECT COUNT(*) FROM outreach")
if mode == "harvest":
    print(f"""🔎 LinkedIn harvest run — {today}
• Harvested today: {harvested}
• Total tracked: {total}""")
    sys.exit(0)
sent      = n("SELECT COUNT(*) FROM outreach WHERE requested_at LIKE ?", today+"%")
accepted  = n("SELECT COUNT(*) FROM outreach WHERE connected_at LIKE ?", today+"%")
messaged  = n("SELECT COUNT(*) FROM outreach WHERE messaged_at LIKE ?", today+"%")
comments  = n("SELECT COUNT(*) FROM post_engagement WHERE commented_at LIKE ? "
              "AND COALESCE(comment_text,'') NOT LIKE '(comment already on post%'", today+"%")
try:
    names = [r[0] for r in c.execute("SELECT name FROM conversations WHERE last_message_from='them' "
                                     "ORDER BY last_message_checked_at DESC LIMIT 8")]
    n_wait = n("SELECT COUNT(*) FROM conversations WHERE last_message_from='them'")
    waiting = str(n_wait) + (" (" + ", ".join(names) + ")" if names else "")
except Exception:
    waiting = "n/a"
week      = n("SELECT COUNT(*) FROM outreach WHERE requested_at >= ?",
              (datetime.date.today()-datetime.timedelta(days=7)).isoformat())
e = None
if Path(engine_db).exists():
    try: e = ro(engine_db)
    except Exception: pass
handed    = n("SELECT COUNT(*) FROM prospects WHERE source LIKE 'linkedin%' AND substr(discovered_at,1,10)=?",
              today, cur=e, fail="n/a")
emailed   = n("SELECT COUNT(*) FROM prospects WHERE substr(sent_at,1,10)=? AND status!='sent_manual'",
              today, cur=e, fail="n/a")
supply_line = "n/a"
try:
    import json
    s = json.loads(e.execute("SELECT v FROM kv WHERE k='supply_forecast'").fetchone()[0])
    gaps = ([str(s["supply_gap"]) + " short on verified people"] if s.get("supply_gap") else []) + \
           ([str(s["cap_gap"]) + " short on company caps"] if s.get("cap_gap") else [])
    supply_line = (str(s["forecast_1d"]) + " of " + str(s["target"]) + " ready for " + s["as_of"].replace("T", " ")
                   + (" (" + "; ".join(gaps) + ")" if gaps else "") + " [as of " + s["recorded_at"][5:16].replace("T", " ") + "]")
except Exception:
    pass
print(f"""🔗 LinkedIn daily run — {today}
• New requests sent: {sent}  (week {week}/90)
• Accepted today: {accepted}
• DMs sent: {messaged}
• Feed comments: {comments}
• Replies waiting for you: {waiting}
• Harvested today: {harvested}
• Handed to email engine today: {handed}
• Emails sent today: {emailed}
• Email supply (next batch): {supply_line}
• Total tracked: {total}""")
PYEOF
}

# ── Logging + log rotation (keep the file from growing unbounded) ────────────
log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"; }

rotate_log() {
    # Rotate if the daily log exceeds ~5 MB.
    if [ -f "$LOG" ] && [ "$(wc -c < "$LOG" 2>/dev/null || echo 0)" -gt 5000000 ]; then
        mv "$LOG" "$LOG.1" 2>/dev/null || true
    fi
}
rotate_log

# ── Send-hours guard (08–22 local, matches within_send_hours() in the script) ─
HOUR=$(date +%-H)
if [ "$HOUR" -lt 8 ] || [ "$HOUR" -ge 22 ]; then
    log "⏸  Outside send-hours (hour=$HOUR, allowed 08–22). Running SAFE phases only (sync + sheets)."
    SAFE_ONLY=1
else
    SAFE_ONLY=0
fi

# ── Human-like jitter between phases ─────────────────────────────────────────
sleep_between() {
    # sleep_between MIN_MIN MAX_MIN  → sleep a random number of seconds in range
    local lo=$1 hi=$2
    local secs=$(( (RANDOM % ((hi - lo) * 60 + 1)) + lo * 60 ))
    log "   …pausing ${secs}s (~$((secs/60))m) before next phase"
    sleep "$secs"
}

# ── Phase runner: isolate failures, log clearly ──────────────────────────────
run_phase() {
    # run_phase "Human label" arg1 arg2 ...
    local label="$1"; shift
    log "▶ PHASE: $label   ($PYTHON linkedin_outreach.py $*)"
    local start=$SECONDS
    if "$PYTHON" "$PY_SCRIPT" "$@"; then
        log "✔ PHASE OK: $label   (${SECONDS}s wait, took $(( SECONDS - start ))s)"
        return 0
    else
        local rc=$?
        log "✖ PHASE FAILED: $label   (exit $rc) — logged and skipped, continuing"
        return 0   # never propagate — one bad phase must not kill the day
    fi
}

# ─────────────────────────────────────────────────────────────────────────────
if [ "${HARVEST_ONLY:-0}" = "1" ]; then
    log "════════════ HARVEST-ONLY RUN START | headless=${HEADLESS:-0} | safe_only=$SAFE_ONLY ════════════"
    if [ "$SAFE_ONLY" -eq 0 ]; then
        run_phase "harvest people" --harvest --pages "${HARVEST_PAGES:-6}"
        sleep_between 2 5
        run_phase "harvest similar profiles" --harvest-similar
    fi
    run_phase "sync Google Sheet" --sync-sheets
    log "▶ Building harvest summary + WhatsApp notify"
    SUMMARY="$(daily_summary harvest)"
    log "$SUMMARY"
    notify_whatsapp "$SUMMARY"
    log "════════════ HARVEST-ONLY RUN COMPLETE ════════════"
    exit 0
fi

log "════════════ DAILY RUN START | headless=${HEADLESS:-0} | safe_only=$SAFE_ONLY ════════════"

# 1. Detect accepted invites first (cheap, zero LinkedIn write-risk).
run_phase "sync accepted connections" --sync-connections

if [ "$SAFE_ONLY" -eq 0 ]; then
    sleep_between 3 8

    # 2. Harvest people from search result pages only (no profile visits, no invites).
    run_phase "harvest people" --harvest --pages "${HARVEST_PAGES:-6}"

    sleep_between 3 8

    # 2b. Harvest people from 'More profiles for you' rails on known profiles
    #     (no search, so it keeps working after the monthly search limit hits).
    run_phase "harvest similar profiles" --harvest-similar

    sleep_between 3 8

    # 3. Send new connection requests (10-15/day, self-limited by daily/weekly caps).
    run_phase "send new connection requests"

    sleep_between 5 12

    # 4. DM newly-connected people: the FIRST message only (AI via Ollama; 20/day).
    run_phase "message new connections" --message-connections

    sleep_between 3 8

    # 5. Read threads and record who replied. Never answers: replies are yours.
    run_phase "sync messages (read-only)" --sync-messages

    sleep_between 3 8

    # 6. Engage the feed — like + AI comment on up to 25 posts (25 comments/day, never twice on a post).
    run_phase "engage feed" --engage-feed --limit 25
fi

# 7. Final Google Sheet sync (every phase already syncs, but make it explicit).
run_phase "sync Google Sheet" --sync-sheets

# 8. WhatsApp summary of today's activity.
log "▶ Building daily summary + WhatsApp notify"
SUMMARY="$(daily_summary full)"
log "$SUMMARY"
notify_whatsapp "$SUMMARY"

log "════════════ DAILY RUN COMPLETE ════════════"
