#!/usr/bin/env python3
"""
LinkedIn Outreach Automation
- Finds professionals at target companies
- Sends connection requests (10-15/day)
- Tracks everything in SQLite (local) + CSV
- Sends follow-up messages after acceptance
"""

import time
import json
import random
import sqlite3
import csv
import os
import logging
from logging.handlers import RotatingFileHandler
import re
import hashlib
import collections
import unicodedata
import urllib.parse
import urllib.request
import urllib.error
from datetime import datetime, timedelta
from pathlib import Path

import undetected_chromedriver as uc
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, NoSuchElementException

try:
    import linkedin_private as _PRIV  # personal values, gitignored
except ImportError:
    _PRIV = None

# ─── CONFIG ───────────────────────────────────────────────────────────────────
LINKEDIN_EMAIL = ""       # Set via env or prompt
LINKEDIN_PASSWORD = ""    # Set via env or prompt

# ─── ACCOUNT-SAFETY LIMITS ──────────────────────────────────────────────────
# The account is the asset. These conservative caps exist to keep it healthy —
# LinkedIn restricts accounts that exceed the WEEKLY invitation cap, so that's
# the hard ceiling we never cross.
DAILY_LIMIT = 15          # Max invites in one run/day (hard cap)
WEEKLY_LIMIT = 90         # Trailing-7-day hard cap (LinkedIn's real trigger ~100; stay under)
PENDING_WARN = 200        # Warn if outstanding un-accepted invites exceed this
SEND_HOURS = (8, 22)      # Only send during these local hours (automation at 3am = obvious bot)

# ─── FEED ENGAGEMENT ────────────────────────────────────────────────────────
FEED_POST_COUNT = int(os.environ.get("FEED_POST_COUNT", "15"))  # posts to engage per run
COMMENT_DAILY_LIMIT = int(os.environ.get("COMMENT_DAILY_LIMIT", "25"))  # higher-risk than likes; env-overridable for testing
MESSAGE_DAILY_LIMIT = int(os.environ.get("MESSAGE_DAILY_LIMIT", "20"))  # first DMs to new 1st-degree connections; env-overridable
# The automation sends only the FIRST message to a new connection. Replies are
# handled by hand: --sync-messages reads threads and records who replied, but
# never answers unless LINKEDIN_AUTO_REPLY=1 is set explicitly.
AUTO_REPLY = os.environ.get("LINKEDIN_AUTO_REPLY", "0") == "1"
# Ollama generates comments locally (no API key/cost). Runs on the Razer,
# reached over Tailscale. Override with env vars if the address/model changes.
#   models on the Razer: qwen2.5:7b, gemma3:4b, gemma4:12b
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5:7b")

# A canned line under the wrong post reads worse than silence, so a post we
# can't write a real comment for is liked only. COMMENT_ALLOW_FALLBACK=1 posts
# these generic, name-free lines instead.
COMMENT_ALLOW_FALLBACK = os.environ.get("COMMENT_ALLOW_FALLBACK", "0") == "1"
COMMENT_MIN_POST_CHARS = int(os.environ.get("COMMENT_MIN_POST_CHARS", "80"))
COMMENT_MAX_ATTEMPTS = int(os.environ.get("COMMENT_MAX_ATTEMPTS", "3"))
# Recorded against a post that already carries a comment of mine (found in the
# DOM, e.g. commented from the phone or before the DB existed). Marks the post
# as done without spending today's comment budget on it.
PRE_EXISTING_COMMENT = "(comment already on post — not posted by this run)"
FALLBACK_COMMENTS = [
    "Useful perspective, thanks for sharing this.",
    "Good read, appreciate the write-up.",
    "Solid point, thanks for putting it out there.",
    "Enjoyed this one, thanks for sharing.",
]

# Safe fallback DMs if Ollama is unreachable — never send an empty message.
FALLBACK_MESSAGES = [
    "Hi {name}, thanks for connecting! Would love to hear what you're working on these days.",
    "Hey {name}, glad we connected. What's keeping you busy lately?",
    "Hi {name}, great to be connected! What are you focused on at the moment?",
]

# Daily target: steady-state volume (10–15/day, jittered) from day one, per the
# user's decision to skip the warm-up ramp. The WEEKLY_LIMIT (90) and DAILY_LIMIT
# (15) hard caps remain the real safety net — daily_target never exceeds them,
# and the weekly cap still hard-stops sends once the trailing-7-day count hits 90.
# (Prior behaviour ramped 3→5→8→12 over the first ~60 lifetime sends; kept here in
# comment for reference in case a gentler ramp is wanted again.)
def daily_target(total_sent_ever):
    return random.randint(10, DAILY_LIMIT)                     # steady state, jittered

DB_PATH = Path.home() / "linkedin_outreach.db"
CSV_PATH = Path.home() / "linkedin_outreach.csv"
XLSX_PATH = Path.home() / "linkedin_outreach.xlsx"
LOG_PATH = Path.home() / "linkedin_outreach.log"

# ─── GOOGLE SHEETS SYNC ──────────────────────────────────────────────────────
# The same three tables (outreach / post_engagement / conversations) are mirrored
# to a Google Sheet on every run when LINKEDIN_SHEETS_SYNC=1, exactly like the
# local .xlsx, plus a read-only 'Email Outreach' tab from the email engine's
# state DB. Auth is a Google service account — headless, no token expiry.
#
# ONE-TIME SETUP:
#   1. https://console.cloud.google.com  → any project
#   2. APIs & Services → Enable "Google Sheets API"
#   3. Credentials → Create Service Account → Keys → Add Key (JSON) → download
#   4. Save the JSON to ~/.linkedin_gsheets.json  (or set GSHEETS_CREDS env)
#   5. Open the target sheet → Share → paste the service account's client_email
#      (…@….iam.gserviceaccount.com) as Editor
#
# If the creds file is missing the sync is silently skipped — a run never breaks
# because Sheets is unavailable. Force a standalone push with `--sync-sheets`.
GSHEETS_SPREADSHEET_ID = os.environ.get(
    "GSHEETS_SPREADSHEET_ID", getattr(_PRIV, "GSHEETS_SPREADSHEET_ID", ""))
GSHEETS_CREDS_PATH = Path(os.environ.get(
    "GSHEETS_CREDS", str(Path.home() / ".linkedin_gsheets.json")))
# Table → target tab name (created if absent). Order controls left-to-right tabs.
GSHEETS_TABS = [
    ("outreach",        "Connections",     "created_at DESC"),
    ("post_engagement", "Feed Engagement", "created_at DESC"),
    ("conversations",   "Conversations",   "updated_at DESC"),
]

INDIA_GEO_URN = "102713980"  # LinkedIn people-search geoUrn for India

COMPANIES_CSV = Path(os.environ.get("LINKEDIN_COMPANIES_CSV")
                     or Path(__file__).resolve().parents[2] / "config" / "companies.csv")
TIER_WEIGHTS = {"A": 3, "B": 2, "C": 1}
SEARCH_ROLES = {"Talent Acquisition": 3, "Technical Recruiter": 3,
                "Engineering Manager": 2, "Senior Software Engineer": 1}

# Search-page budget. Every people-search page (harvest or connect) counts
# toward the daily ceiling recorded in harvest_log.
HARVEST_PAGES_PER_RUN = int(os.environ.get("HARVEST_PAGES_PER_RUN", "6"))
# A free account gets roughly 60-80 people-search result pages a month (the
# commercial-use limit); 15/day burned it in four days (Oct 2026) and left 26 days
# with no new people. 3/day spreads it over the month. Shared by harvest, connect
# searches and company-id resolution.
HARVEST_MAX_PAGES_PER_DAY = int(os.environ.get("HARVEST_MAX_PAGES_PER_DAY", "3"))
HARVEST_MAX_PAGE = 10
HARVEST_PAGE_CARDS = 8  # a people-search page below this many parsed cards is the last page
HARVEST_CURSOR_TTL_DAYS = 45  # re-open an exhausted (company, role, geo) cursor after this
CONNECT_MAX_SEARCHES = int(os.environ.get("CONNECT_MAX_SEARCHES", "5"))
COMPANY_ID_RETRY_DAYS = 3
SEARCH_LIMIT_PHRASES = ("monthly limit", "commercial use limit", "reached the limit for")

# Similar-profiles harvest: the "More profiles for you" / "People also viewed" rail
# on a profile page lists related people, usually colleagues at the same employer.
# Opening a profile by URL is not a people search, so it never touches the monthly
# commercial-use limit. Visits are capped per run and per day and paced like a human.
SIMILAR_PER_RUN = int(os.environ.get("SIMILAR_PER_RUN", "30"))
SIMILAR_MAX_PER_DAY = int(os.environ.get("SIMILAR_MAX_PER_DAY", "60"))
SIMILAR_RAIL_HEADINGS = ("more profiles for you", "people also viewed", "people you may know")
SIMILAR_SKIP_HEADINGS = ("others named",)  # namesakes, not colleagues
RECRUITER_TITLE_RE = re.compile(r"recruit|talent|sourc|hiring|\bhr\b|people partner", re.I)

CONNECTION_NOTE_TEMPLATES = [
    "Hi {name}, I'm a software engineer and came across your profile. Would be great to connect!",
    "Hi {name}, fellow software engineer here. I'd like to keep up with the engineering work at {company} — happy to connect.",
    "Hi {name}, I'm a software engineer keen to learn more about engineering at {company}. Would love to connect!",
]

FOLLOW_UP_TEMPLATES = [
    "Hi {name}, thanks for connecting! Would love to hear how you find working at {company}.",
    "Hey {name}, appreciate the connection! Curious what your work as {title} at {company} looks like day to day — any advice for an engineer interested in the team?",
    "Hi {name}, glad to be connected! I've been following {company}'s engineering work. Would you be open to a quick chat sometime?",
]

# Personal copy and settings override the neutral defaults above (src/linkedin/linkedin_private.py, gitignored).
CONNECTION_NOTE_TEMPLATES = getattr(_PRIV, "CONNECTION_NOTE_TEMPLATES", CONNECTION_NOTE_TEMPLATES)
FOLLOW_UP_TEMPLATES = getattr(_PRIV, "FOLLOW_UP_TEMPLATES", FOLLOW_UP_TEMPLATES)
HOME_CITY = getattr(_PRIV, "HOME_CITY", "")

def load_companies(path=None):
    """Target companies from the shared registry (config/companies.csv), minus
    exclude=1 rows and harvest=0 rows (email-only companies the engine knows but
    LinkedIn does not search). Exits when the file is missing or has no usable rows."""
    path = Path(path or COMPANIES_CSV)
    if not path.is_file():
        log(f"❌ Company registry not found: {path} (set LINKEDIN_COMPANIES_CSV)")
        raise SystemExit(1)
    companies = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        for raw in csv.DictReader(f):
            row = {k.strip(): (v or "").strip() for k, v in raw.items() if isinstance(k, str)}
            key = row.get("key", "").lower()
            if row.get("exclude") == "1" or row.get("harvest") == "0":
                continue
            if not re.fullmatch(r"[a-z0-9-]+", key):
                log(f"⚠️  Registry row skipped — bad key {key!r}")
                continue
            try:
                share = min(1.0, max(0.0, float(row.get("india_share") or 0.8)))
            except ValueError:
                share = 0.8
            name = row.get("name") or key
            companies.append({
                "key": key,
                "name": name,
                "tier": (row.get("tier") or "C").upper(),
                "linkedin_name": row.get("linkedin_name") or name,
                "aliases": [a.strip() for a in row.get("aliases", "").split("|") if a.strip()],
                "india_share": share,
                "linkedin_company_id": re.sub(r"\D", "", row.get("linkedin_company_id", "")),
                "email_domains": [d.strip().lower() for d in row.get("email_domains", "").split("|")
                                  if d.strip()],
            })
    if not companies:
        log(f"❌ Company registry {path} has no usable (exclude=0) rows")
        raise SystemExit(1)
    return companies

# ─── DATABASE ─────────────────────────────────────────────────────────────────
def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS outreach (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            linkedin_id TEXT UNIQUE,
            name TEXT,
            title TEXT,
            company TEXT,
            location TEXT,
            profile_url TEXT,
            status TEXT DEFAULT 'found',
            requested_at TEXT,
            connected_at TEXT,
            messaged_at TEXT,
            message_sent TEXT,
            notes TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    # Feed engagement log. post_urn (urn:li:activity:...) is the stable, unique
    # key — the UNIQUE constraint plus has_commented() guarantees we NEVER
    # comment on the same post twice, even across runs.
    cur.execute("""
        CREATE TABLE IF NOT EXISTS post_engagement (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            post_urn TEXT UNIQUE,
            author TEXT,
            preview TEXT,
            liked_at TEXT,
            commented_at TEXT,
            comment_text TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    # Conversation tracking — separate entity from the funnel row, keyed by
    # linkedin_id. Evolves independently as messages/replies come in.
    cur.execute("""
        CREATE TABLE IF NOT EXISTS conversations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            linkedin_id TEXT UNIQUE,
            name TEXT,
            profile_url TEXT,
            last_message_text TEXT,
            last_message_from TEXT,       -- 'me' | 'them'
            last_message_at TEXT,         -- ISO or raw relative timestamp
            reply_count INTEGER DEFAULT 0,-- count of 'them' messages observed
            thread_url TEXT,
            updated_at TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    # Idempotent column additions to outreach (CREATE IF NOT EXISTS can't add
    # columns; bare ALTER errors on the 2nd run — so guard with table_info).
    _ensure_columns(cur, "outreach", {"replied_at": "TEXT", "last_activity_at": "TEXT",
                                      "first_name": "TEXT", "last_name": "TEXT",
                                      "city": "TEXT", "country": "TEXT",
                                      "company_key": "TEXT",
                                      "company_verified": "INTEGER DEFAULT 0",
                                      "source": "TEXT DEFAULT 'connect'",
                                      "search_geo": "TEXT"})
    # comment_attempts caps blind retries on a post whose submit never verifies.
    # direction_source records WHICH signal decided last_message_from, so a
    # wrong direction in the sheet is debuggable rather than a mystery.
    _ensure_columns(cur, "post_engagement", {"comment_attempts": "INTEGER DEFAULT 0"})
    _ensure_columns(cur, "conversations", {"direction_source": "TEXT",
                                           "last_message_checked_at": "TEXT"})
    cur.execute("""
        CREATE TABLE IF NOT EXISTS company_ids (
            company_key TEXT PRIMARY KEY,
            linkedin_company_id TEXT,
            matched_name TEXT,
            resolved_at TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS harvest_cursor (
            company_key TEXT,
            role TEXT,
            geo TEXT,
            next_page INTEGER,
            exhausted INTEGER DEFAULT 0,
            updated_at TEXT,
            PRIMARY KEY (company_key, role, geo)
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS harvest_log (
            day TEXT PRIMARY KEY,
            pages INTEGER,
            blocked_until TEXT,
            note TEXT
        )
    """)
    # Profiles opened by --harvest-similar, so a seed is visited once (an empty
    # rail is retried after 14 days) and the daily visit cap can be counted.
    cur.execute("""
        CREATE TABLE IF NOT EXISTS similar_seed (
            linkedin_id TEXT PRIMARY KEY,
            visited_at TEXT,
            rail_cards INTEGER,
            saved INTEGER
        )
    """)
    # The email engine only imports once the columns/tables above exist.
    if cur.execute("PRAGMA user_version").fetchone()[0] < 1:
        cur.execute("PRAGMA user_version = 1")
    conn.commit()
    return conn

def _ensure_columns(cur, table, columns):
    """Add any missing columns to an existing table (idempotent migration).
    `columns` maps name -> SQL type decl."""
    existing = {row[1] for row in cur.execute(f"PRAGMA table_info({table})")}
    for col, decl in columns.items():
        if col not in existing:
            cur.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")

def upsert_conversation(conn, linkedin_id, **fields):
    """Insert or update a conversations row keyed by linkedin_id. Only the
    provided fields are written; always stamps updated_at."""
    fields["updated_at"] = datetime.now().isoformat()
    cur = conn.cursor()
    cur.execute("INSERT OR IGNORE INTO conversations (linkedin_id) VALUES (?)", (linkedin_id,))
    sets = ", ".join(f"{k}=?" for k in fields)
    cur.execute(f"UPDATE conversations SET {sets} WHERE linkedin_id=?",
                list(fields.values()) + [linkedin_id])
    conn.commit()

def record_engagement(conn, post_urn, author="", preview=""):
    """Ensure a row exists for this post; returns nothing. Idempotent."""
    cur = conn.cursor()
    cur.execute("INSERT OR IGNORE INTO post_engagement (post_urn, author, preview) "
                "VALUES (?, ?, ?)", (post_urn, author, preview[:280]))
    conn.commit()

def has_commented(conn, post_urn):
    """True if we've ALREADY commented on this post — the never-twice guard."""
    cur = conn.cursor()
    cur.execute("SELECT commented_at FROM post_engagement WHERE post_urn=?", (post_urn,))
    row = cur.fetchone()
    return bool(row and row[0])

def has_liked(conn, post_urn):
    cur = conn.cursor()
    cur.execute("SELECT liked_at FROM post_engagement WHERE post_urn=?", (post_urn,))
    row = cur.fetchone()
    return bool(row and row[0])

def mark_liked(conn, post_urn):
    cur = conn.cursor()
    cur.execute("UPDATE post_engagement SET liked_at=? WHERE post_urn=?",
                (datetime.now().isoformat(), post_urn))
    conn.commit()

def mark_commented(conn, post_urn, comment_text):
    cur = conn.cursor()
    cur.execute("UPDATE post_engagement SET commented_at=?, comment_text=? WHERE post_urn=?",
                (datetime.now().isoformat(), comment_text, post_urn))
    conn.commit()

def bump_comment_attempt(conn, post_urn):
    cur = conn.cursor()
    cur.execute("UPDATE post_engagement SET comment_attempts=COALESCE(comment_attempts,0)+1 "
                "WHERE post_urn=?", (post_urn,))
    conn.commit()

def comment_attempts(conn, post_urn):
    cur = conn.cursor()
    cur.execute("SELECT COALESCE(comment_attempts,0) FROM post_engagement WHERE post_urn=?",
                (post_urn,))
    row = cur.fetchone()
    return row[0] if row else 0

def comments_today(conn):
    """Comments this tool actually posted today — pre-existing ones we merely
    detected are marked done but must not spend the daily budget."""
    cur = conn.cursor()
    today = datetime.now().date().isoformat()
    cur.execute("SELECT COUNT(*) FROM post_engagement "
                "WHERE commented_at LIKE ? AND COALESCE(comment_text,'') != ?",
                (f"{today}%", PRE_EXISTING_COMMENT))
    return cur.fetchone()[0]

def messages_today(conn):
    cur = conn.cursor()
    today = datetime.now().date().isoformat()
    cur.execute("SELECT COUNT(*) FROM outreach WHERE messaged_at LIKE ?", (f"{today}%",))
    return cur.fetchone()[0]

def save_person(conn, person):
    """INSERT OR IGNORE on linkedin_id. On re-sighting, a VERIFIED sighting is
    authoritative and overwrites the stored employer (company_key, company,
    company_verified) and upgrades search_geo to 'india' when seen in an India
    search; an unverified sighting only fills empty fields and never downgrades a
    verified row. Returns True when a new row was added."""
    first, last = person.get("first_name"), person.get("last_name")
    if first is None and last is None:
        first, last = _split_name(person.get("name", ""))
    first, last = first or "", last or ""
    city, country = person.get("city") or "", person.get("country") or ""
    key = person.get("company_key") or None
    verified = 1 if key and person.get("company_verified") else 0
    company = person.get("company") or ""
    geo = person.get("search_geo")
    cur = conn.cursor()
    cur.execute("""
        INSERT OR IGNORE INTO outreach
        (linkedin_id, name, title, company, location, profile_url, status, requested_at,
         first_name, last_name, city, country, company_key, company_verified, source, search_geo,
         notes)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (person['id'], person['name'], person['title'], company,
          person.get('location',''), person['url'],
          person.get('status','found'), person.get('requested_at'),
          first, last, city, country, key, verified,
          person.get('source') or 'connect', geo, person.get('notes')))
    inserted = cur.rowcount == 1
    if not inserted:
        # SET expressions all read the pre-update row. A verified sighting (new
        # verified=1 with a key) overwrites the employer even when a different
        # one was pinned earlier; otherwise fields are only filled when empty.
        cur.execute("""
            UPDATE outreach SET
                company_verified = CASE WHEN ? = 1 THEN 1 ELSE company_verified END,
                company_key = CASE WHEN ? = 1 THEN ?
                                   ELSE COALESCE(NULLIF(company_key, ''), ?) END,
                company     = CASE WHEN ? = 1 AND ? != '' THEN ?
                                   ELSE COALESCE(NULLIF(company, ''), ?) END,
                search_geo  = CASE WHEN ? = 'india' THEN 'india'
                                   ELSE COALESCE(NULLIF(search_geo, ''), ?) END,
                first_name  = COALESCE(NULLIF(first_name, ''), ?),
                last_name   = COALESCE(NULLIF(last_name, ''), ?),
                city        = COALESCE(NULLIF(city, ''), ?),
                country     = CASE WHEN ? = 'India' THEN 'India'
                                   ELSE COALESCE(NULLIF(country, ''), ?) END
            WHERE linkedin_id = ?
        """, (verified,
              verified, key, key,
              verified, company, company, company,
              geo, geo,
              first, last, city,
              country, country,
              person['id']))
    conn.commit()
    return inserted

def update_status(conn, linkedin_id, status, extra=None):
    cur = conn.cursor()
    updates = {"status": status}
    if status == "requested":
        updates["requested_at"] = datetime.now().isoformat()
    elif status == "connected":
        updates["connected_at"] = datetime.now().isoformat()
    elif status == "messaged":
        updates["messaged_at"] = datetime.now().isoformat()
        if extra:
            updates["message_sent"] = extra
    elif status == "replied":
        updates["replied_at"] = datetime.now().isoformat()
    # 'developing' just advances status; no dedicated timestamp.
    updates["last_activity_at"] = datetime.now().isoformat()

    set_clause = ", ".join(f"{k}=?" for k in updates)
    cur.execute(f"UPDATE outreach SET {set_clause} WHERE linkedin_id=?",
                list(updates.values()) + [linkedin_id])
    conn.commit()

def get_pending_messages(conn):
    """Get connected people who haven't been messaged yet."""
    cur = conn.cursor()
    cur.execute("""
        SELECT linkedin_id, name, title, company, profile_url
        FROM outreach
        WHERE status='connected' AND messaged_at IS NULL
    """)
    return cur.fetchall()

def _dump_table_to_sheet(cur, ws, table, order_by):
    """Write an entire SQLite table into a worksheet (header + all rows)."""
    cur.execute(f"SELECT * FROM {table} ORDER BY {order_by}")
    rows = cur.fetchall()
    cols = [d[0] for d in cur.description]
    ws.append(cols)
    for r in rows:
        ws.append([("" if v is None else v) for v in r])
    # Bold header + freeze it + reasonable column widths.
    from openpyxl.styles import Font
    for c in ws[1]:
        c.font = Font(bold=True)
    ws.freeze_panes = "A2"
    for i, col in enumerate(cols, start=1):
        letter = ws.cell(row=1, column=i).column_letter
        width = max(len(str(col)) + 2, 14)
        ws.column_dimensions[letter].width = min(width, 60)
    return len(rows)

def export_workbook(conn):
    """Export the FULL history into one Excel workbook with two tabs:
    'Connections' (outreach) and 'Feed Engagement' (post_engagement). Reads the
    whole DB each run, so the workbook always reflects complete accumulated
    history — every run targets the same file. Falls back to CSV if openpyxl
    or the file (e.g. open in Excel) is unavailable."""
    try:
        from openpyxl import Workbook
        cur = conn.cursor()
        wb = Workbook()
        ws1 = wb.active
        ws1.title = "Connections"
        n1 = _dump_table_to_sheet(cur, ws1, "outreach", "created_at DESC")
        ws2 = wb.create_sheet("Feed Engagement")
        n2 = _dump_table_to_sheet(cur, ws2, "post_engagement", "created_at DESC")
        ws3 = wb.create_sheet("Conversations")
        n3 = _dump_table_to_sheet(cur, ws3, "conversations", "updated_at DESC")
        wb.save(XLSX_PATH)
        log(f"  Workbook updated → {XLSX_PATH} "
            f"({n1} connections, {n2} feed posts, {n3} conversations)")
    except PermissionError:
        log(f"  ⚠️  {XLSX_PATH} is open (close it in Excel) — falling back to CSV this run")
        _export_csv_fallback(conn)
    except Exception as e:
        log(f"  ⚠️  Excel export failed ({e}) — falling back to CSV")
        _export_csv_fallback(conn)

def _export_csv_fallback(conn):
    cur = conn.cursor()
    cur.execute("SELECT * FROM outreach ORDER BY created_at DESC")
    rows = cur.fetchall()
    cols = [d[0] for d in cur.description]
    with open(CSV_PATH, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(cols)
        writer.writerows(rows)
    log(f"  Exported to {CSV_PATH}")

# Backwards-compatible name — all call sites now produce the single workbook.
def export_csv(conn):
    export_workbook(conn)
    export_google_sheets(conn)

def _read_table(cur, table, order_by):
    """Return (header_row, data_rows) for a table, all values stringified for
    the Sheets API (which rejects None and wants JSON-safe scalars)."""
    cur.execute(f"SELECT * FROM {table} ORDER BY {order_by}")
    rows = cur.fetchall()
    cols = [d[0] for d in cur.description]
    values = [cols]
    for r in rows:
        values.append(["" if v is None else str(v) for v in r])
    return values, len(rows)

OUTREACH_STATE_DB = Path(os.environ.get("OUTREACH_STATE_DB")
                         or Path.home() / "outreach" / "state.db")
EMAIL_TAB = "Email Outreach"
EMAIL_TAB_COLUMNS = ("prospect_id", "full_name", "company", "tier", "job_title", "city",
                     "country", "email", "method", "confidence", "status", "sent_at", "source")

def _read_email_outreach():
    """(values, n) of the email engine's prospects, newest first, read-only.
    None when the state DB or its prospects table is absent."""
    if not OUTREACH_STATE_DB.is_file():
        return None
    src = sqlite3.connect(OUTREACH_STATE_DB.resolve().as_uri() + "?mode=ro", uri=True, timeout=10)
    try:
        have = {r[1] for r in src.execute("PRAGMA table_info(prospects)")}
        cols = [c for c in EMAIL_TAB_COLUMNS
                if c in have or (c == "method" and "resolve_method" in have)]
        if not cols:
            return None
        select = ", ".join(c if c in have else "resolve_method AS method" for c in cols)
        rows = src.execute(f"SELECT {select} FROM prospects ORDER BY rowid DESC").fetchall()
    finally:
        src.close()
    return [cols] + [["" if v is None else str(v) for v in r] for r in rows], len(rows)

def export_google_sheets(conn):
    """Mirror the three tables into the Google Sheet, one tab each, plus the
    read-only 'Email Outreach' tab. Runs only when LINKEDIN_SHEETS_SYNC=1.

    Reads the whole DB each run so the sheet always reflects complete history —
    the same full-overwrite model as the local .xlsx. Creates any missing tabs,
    clears the old contents, writes fresh values, and bold-freezes the header.

    Silently no-ops (with a one-line log) if the service-account JSON is absent
    or the Google libraries aren't installed, so a run never fails because
    Sheets is unreachable."""
    if os.environ.get("LINKEDIN_SHEETS_SYNC") != "1":
        log("  Google Sheets sync skipped (set LINKEDIN_SHEETS_SYNC=1 to enable)")
        return
    if not GSHEETS_CREDS_PATH.exists():
        log_debug(f"  Google Sheets sync skipped — no creds at {GSHEETS_CREDS_PATH}")
        return
    try:
        from google.oauth2.service_account import Credentials
        from googleapiclient.discovery import build
    except ImportError as e:
        log(f"  ⚠️  Google Sheets sync skipped — missing library ({e}). "
            f"Install: pip install google-api-python-client google-auth")
        return

    try:
        creds = Credentials.from_service_account_file(
            str(GSHEETS_CREDS_PATH),
            scopes=["https://www.googleapis.com/auth/spreadsheets"])
        svc = build("sheets", "v4", credentials=creds, cache_discovery=False)
        sheets_api = svc.spreadsheets()

        # Map existing tab titles → sheetId so we can create/clear correctly.
        meta = sheets_api.get(spreadsheetId=GSHEETS_SPREADSHEET_ID).execute()
        existing = {s["properties"]["title"]: s["properties"]["sheetId"]
                    for s in meta.get("sheets", [])}

        cur = conn.cursor()
        counts = {}
        header_formats = []

        def _write_tab(tab, values):
            # Create the tab if it doesn't exist yet.
            if tab not in existing:
                resp = sheets_api.batchUpdate(
                    spreadsheetId=GSHEETS_SPREADSHEET_ID,
                    body={"requests": [{"addSheet": {"properties": {"title": tab}}}]},
                ).execute()
                existing[tab] = resp["replies"][0]["addSheet"]["properties"]["sheetId"]

            sheet_id = existing[tab]
            # Clear then write — full overwrite matches the .xlsx behaviour.
            sheets_api.values().clear(
                spreadsheetId=GSHEETS_SPREADSHEET_ID, range=f"'{tab}'").execute()
            sheets_api.values().update(
                spreadsheetId=GSHEETS_SPREADSHEET_ID,
                range=f"'{tab}'!A1",
                valueInputOption="RAW",
                body={"values": values},
            ).execute()

            # Bold + freeze the header row for this tab.
            header_formats.extend([
                {"repeatCell": {
                    "range": {"sheetId": sheet_id, "startRowIndex": 0, "endRowIndex": 1},
                    "cell": {"userEnteredFormat": {"textFormat": {"bold": True}}},
                    "fields": "userEnteredFormat.textFormat.bold"}},
                {"updateSheetProperties": {
                    "properties": {"sheetId": sheet_id,
                                   "gridProperties": {"frozenRowCount": 1}},
                    "fields": "gridProperties.frozenRowCount"}},
            ])

        for table, tab, order_by in GSHEETS_TABS:
            values, n = _read_table(cur, table, order_by)
            counts[tab] = n
            _write_tab(tab, values)

        try:
            email = _read_email_outreach()
            if email:
                _write_tab(EMAIL_TAB, email[0])
                counts[EMAIL_TAB] = email[1]
        except Exception as e:
            log(f"  ⚠️  '{EMAIL_TAB}' tab skipped ({type(e).__name__}: {e})")

        if header_formats:
            sheets_api.batchUpdate(
                spreadsheetId=GSHEETS_SPREADSHEET_ID,
                body={"requests": header_formats}).execute()

        summary = ", ".join(f"{n} {tab.lower()}" for tab, n in counts.items())
        log(f"  Google Sheet synced → {GSHEETS_SPREADSHEET_ID} ({summary})")
    except Exception as e:
        log(f"  ⚠️  Google Sheets sync failed ({type(e).__name__}: {e}) — "
            f"local .xlsx still written")

def requests_today(conn):
    cur = conn.cursor()
    today = datetime.now().date().isoformat()
    cur.execute("SELECT COUNT(*) FROM outreach WHERE requested_at LIKE ?", (f"{today}%",))
    return cur.fetchone()[0]

def requests_this_week(conn):
    """Count invites sent in the trailing 7 days.

    LinkedIn's real enforcement trigger is the *weekly* invitation cap (~100),
    not the daily number — exceeding it is what gets accounts restricted. This
    is the most important guardrail for protecting the account.
    """
    cur = conn.cursor()
    week_ago = (datetime.now() - timedelta(days=7)).isoformat()
    cur.execute("SELECT COUNT(*) FROM outreach WHERE requested_at >= ?", (week_ago,))
    return cur.fetchone()[0]

def total_pending(conn):
    """Outstanding requests that haven't been accepted yet. A large pending
    pile is itself a risk signal, so we surface it."""
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM outreach WHERE status='requested'")
    return cur.fetchone()[0]

def total_requested_ever(conn):
    """How many invites this tool has ever sent — drives the warm-up ramp."""
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM outreach WHERE requested_at IS NOT NULL")
    return cur.fetchone()[0]

def acceptance_rate(conn):
    """Fraction of sent invites that were accepted (status advanced past
    'requested'). Acceptance rate <~20% is a primary LinkedIn restriction
    trigger, so we surface it. Returns (rate_pct, sent, accepted) or None if
    too few data points to be meaningful."""
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM outreach WHERE requested_at IS NOT NULL")
    sent = cur.fetchone()[0]
    if sent < 10:
        return None
    cur.execute("SELECT COUNT(*) FROM outreach WHERE connected_at IS NOT NULL "
                "OR status IN ('connected','messaged')")
    accepted = cur.fetchone()[0]
    return (100.0 * accepted / sent, sent, accepted)

def stale_pending(conn, days=21):
    """Count invites pending longer than `days` — candidates to withdraw."""
    cur = conn.cursor()
    cutoff = (datetime.now() - timedelta(days=days)).isoformat()
    cur.execute("SELECT COUNT(*) FROM outreach WHERE status='requested' "
                "AND requested_at IS NOT NULL AND requested_at < ?", (cutoff,))
    return cur.fetchone()[0]

def within_send_hours():
    """True if the local hour is inside the allowed sending window. Sending
    invites at 3am is one of the clearest automation tells."""
    return SEND_HOURS[0] <= datetime.now().hour < SEND_HOURS[1]

# ─── SELENIUM HELPERS ─────────────────────────────────────────────────────────
def human_delay(min_s=1.5, max_s=3.5):
    time.sleep(random.uniform(min_s, max_s))

def make_interactable(driver, element):
    """Scroll an element into view and try to focus it so send_keys works.

    LinkedIn's login page has multiple email/password inputs in the DOM, some
    hidden. An element can be *present* but not *interactable*. We scroll it
    into the viewport and JS-focus it as a best-effort before typing.
    """
    try:
        driver.execute_script(
            "arguments[0].scrollIntoView({block: 'center', behavior: 'instant'});",
            element,
        )
    except Exception:
        pass
    try:
        driver.execute_script("arguments[0].focus();", element)
    except Exception:
        pass

def robust_click(driver, element):
    """Click an element reliably on LinkedIn's SDUI (React) feed.

    The feed buttons are React components (componentkey=...) that listen for a
    full POINTER/MOUSE event sequence, not a bare .click(). A native Selenium
    click hits an overlay center and a synthetic element.click() is ignored by
    React. So we: scroll into view, resolve the nearest <button>, and dispatch
    the real pointerdown→mousedown→mouseup→click sequence at the element's own
    coordinates. Returns True if dispatched."""
    make_interactable(driver, element)
    # Try a native click first (works for non-React controls).
    try:
        element.click()
    except Exception:
        pass
    # React-friendly synthetic pointer/mouse sequence on the button itself.
    try:
        driver.execute_script("""
            var el = arguments[0];
            var btn = (el.closest && el.closest('button')) ? el.closest('button') : el;
            var r = btn.getBoundingClientRect();
            var cx = r.left + r.width/2, cy = r.top + r.height/2;
            var opts = {bubbles:true, cancelable:true, view:window, clientX:cx, clientY:cy, button:0};
            ['pointerover','pointerenter','pointerdown','mousedown',
             'pointerup','mouseup','click'].forEach(function(type){
                var Ev = type.indexOf('pointer')===0 ? PointerEvent : MouseEvent;
                try { btn.dispatchEvent(new Ev(type, opts)); } catch(e){ btn.dispatchEvent(new MouseEvent('click', opts)); }
            });
        """, element)
        return True
    except Exception:
        pass
    return False

def slow_type(driver, element, text):
    """Type text human-like. Falls back to JS if the element is not directly
    interactable. Handles both <input>/<textarea> (.value) and contenteditable
    (Quill/React comment editors, which use textContent), firing input/change
    so React's onChange enables the submit button."""
    make_interactable(driver, element)
    is_ce = (element.get_attribute("contenteditable") or "").lower() == "true"
    try:
        if not is_ce:
            element.clear()
    except Exception:
        pass
    for char in text:
        try:
            element.send_keys(char)
        except Exception:
            # send_keys failed — set the content via JS the right way for the
            # element type, then fire input/change so React/Quill registers it.
            driver.execute_script(
                "var el=arguments[0], t=arguments[1];"
                "if ((el.getAttribute('contenteditable')||'').toLowerCase()==='true'){"
                "  el.focus(); el.textContent=t;"
                "} else { el.value=t; }"
                "el.dispatchEvent(new Event('input', {bubbles:true}));"
                "el.dispatchEvent(new Event('change', {bubbles:true}));",
                element, text,
            )
            return
        time.sleep(random.uniform(0.05, 0.15))
    # After char-by-char typing, fire an explicit input event too — Quill's
    # React binding sometimes needs it to enable the Post button.
    try:
        driver.execute_script(
            "arguments[0].dispatchEvent(new Event('input', {bubbles:true}));", element)
    except Exception:
        pass

def scroll_down(driver, pixels=300):
    driver.execute_script(f"window.scrollBy(0, {pixels})")
    human_delay(0.5, 1.5)

def wait_for(driver, selector, by=By.CSS_SELECTOR, timeout=15):
    return WebDriverWait(driver, timeout).until(
        EC.presence_of_element_located((by, selector))
    )

def find_interactable_field(driver, selectors, timeout=8):
    """Return the first *visible & enabled* element matching any selector.

    Unlike presence_of_element_located, this only returns a field the user
    could actually type into. LinkedIn keeps hidden duplicate inputs in the
    DOM, so presence alone is not enough (that was the ElementNotInteractable
    bug). We poll each selector for a displayed+enabled match.
    """
    end = time.time() + timeout
    last_selector = None
    while time.time() < end:
        for selector in selectors:
            try:
                for el in driver.find_elements(By.CSS_SELECTOR, selector):
                    if el.is_displayed() and el.is_enabled():
                        return el, selector
            except Exception:
                continue
            last_selector = selector
        time.sleep(0.3)
    # Nothing interactable — fall back to first merely-present element so the
    # JS-typing path in slow_type still has a shot.
    for selector in selectors:
        els = driver.find_elements(By.CSS_SELECTOR, selector)
        if els:
            return els[0], selector
    return None, last_selector

def _setup_logger():
    """Console (clean, human) + rotating file (full audit trail with levels).
    The file at ~/linkedin_outreach.log keeps a persistent record across runs
    (5 files × 1MB), so you can review exactly what happened after the fact."""
    lg = logging.getLogger("linkedin_outreach")
    if lg.handlers:  # already configured (e.g. re-import)
        return lg
    lg.setLevel(logging.DEBUG)

    # Console: just the time + message, as before.
    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(logging.Formatter("[%(asctime)s] %(message)s", datefmt="%H:%M:%S"))
    lg.addHandler(ch)

    # File: full date, level, message — with rotation so it never grows unbounded.
    try:
        fh = RotatingFileHandler(LOG_PATH, maxBytes=1_000_000, backupCount=5, encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(logging.Formatter("%(asctime)s  %(levelname)-7s  %(message)s"))
        lg.addHandler(fh)
    except Exception:
        pass  # console-only if the log file can't be opened
    return lg

_LOGGER = _setup_logger()

def log(msg):
    """Info-level log (console + file). Auto-routes ⚠️/❌ lines to WARNING/ERROR
    so the file's levels are meaningful for later review."""
    m = str(msg)
    stripped = m.strip()
    if stripped.startswith("❌") or "🛑" in stripped:
        _LOGGER.error(m)
    elif stripped.startswith("⚠️") or "⚠️" in stripped:
        _LOGGER.warning(m)
    else:
        _LOGGER.info(m)

def log_debug(msg):
    """Verbose detail — goes to the file only, not the console."""
    _LOGGER.debug(str(msg))

# ─── SESSION PERSISTENCE ──────────────────────────────────────────────────────
CREDS_PATH = Path.home() / ".linkedin_outreach.env"
COOKIE_PATH = Path.home() / ".linkedin_outreach_cookies.json"

def _load_saved_creds():
    """Load email/password from ~/.linkedin_outreach.env if present."""
    if not CREDS_PATH.exists():
        return None, None
    try:
        data = {}
        for line in CREDS_PATH.read_text().splitlines():
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                data[k.strip()] = v.strip()
        return data.get("LINKEDIN_EMAIL"), data.get("LINKEDIN_PASSWORD")
    except Exception:
        return None, None

def _save_creds(email, password):
    """Persist credentials to ~/.linkedin_outreach.env (chmod 600)."""
    try:
        CREDS_PATH.write_text(f"LINKEDIN_EMAIL={email}\nLINKEDIN_PASSWORD={password}\n")
        CREDS_PATH.chmod(0o600)
        log(f"  Credentials saved to {CREDS_PATH}")
    except Exception as e:
        log(f"  ⚠️  Could not save credentials: {e}")

def save_cookies(driver):
    """Persist LinkedIn session cookies so future runs skip login entirely."""
    try:
        cookies = driver.get_cookies()
        COOKIE_PATH.write_text(json.dumps(cookies))
        log(f"  Saved {len(cookies)} session cookies → {COOKIE_PATH}")
    except Exception as e:
        log(f"  ⚠️  Could not save cookies: {e}")

def _get_with_retry(driver, url, attempts=3, wait=4):
    """driver.get with retry on transient network errors (DNS blips, WiFi/VPN
    reconnects). Returns True on success, False if all attempts fail — callers
    handle that gracefully instead of crashing with a stack trace."""
    for i in range(attempts):
        try:
            driver.get(url)
            return True
        except Exception as e:
            msg = str(e).split("\n")[0]
            transient = any(s in msg for s in (
                "ERR_NAME_NOT_RESOLVED", "ERR_INTERNET_DISCONNECTED",
                "ERR_CONNECTION", "ERR_TIMED_OUT", "ERR_NETWORK", "timeout"))
            if transient and i < attempts - 1:
                log(f"  ⚠️  Network hiccup reaching {url} ({msg}) — retry {i+1}/{attempts-1} in {wait}s")
                time.sleep(wait)
                continue
            log(f"  ❌ Could not load {url}: {msg}")
            return False
    return False

def load_cookies(driver):
    """Restore saved cookies. Returns True if a session was loaded and is live."""
    if not COOKIE_PATH.exists():
        return False
    try:
        cookies = json.loads(COOKIE_PATH.read_text())
    except Exception as e:
        log(f"  ⚠️  Could not read cookies ({e}) — will log in fresh")
        return False

    # Cookies can only be set on the matching domain — visit it first.
    if not _get_with_retry(driver, "https://www.linkedin.com"):
        log("  ⚠️  Network unavailable — skipping cookie restore")
        return False
    human_delay(1, 2)
    added = 0
    for c in cookies:
        c.pop("sameSite", None)  # Selenium rejects some sameSite values
        try:
            driver.add_cookie(c)
            added += 1
        except Exception:
            continue
    log(f"  Restored {added}/{len(cookies)} cookies — verifying session...")
    if not _get_with_retry(driver, "https://www.linkedin.com/feed/"):
        return False
    human_delay(2, 4)
    # Verify a REAL logged-in session, not just a URL substring: the re-auth
    # checkpoint (d_checkpoint_rm_rememberMeOrganicLogin) redirects to a URL that
    # still contains "/feed/" in its return_to param, so a substring check gives
    # false positives. Trust the page only if it's the feed URL AND the login
    # nav is absent AND a logged-in marker (global nav) is present.
    url = driver.current_url
    on_checkpoint = "checkpoint" in url or "login" in url or "authwall" in url
    has_nav = False
    try:
        has_nav = bool(driver.find_elements(By.CSS_SELECTOR,
                       "header.global-nav, #global-nav, [data-test-global-nav], "
                       "main#workspace, [data-testid='mainFeed'], "
                       "header a[href*='/mynetwork'], header button[aria-label^='Me']"))
    except Exception:
        pass
    if (("feed" in url or "mynetwork" in url) and not on_checkpoint and has_nav):
        log("✅ Logged in via saved cookies (no password needed)")
        return True
    log("  Saved session expired / checkpoint — falling back to credential login")
    return False

# ─── LINKEDIN ACTIONS ─────────────────────────────────────────────────────────
def _confirm_logged_in(driver):
    url = driver.current_url
    return "feed" in url or "mynetwork" in url or "linkedin.com/in/" in url

def login(driver, email, password, interactive=True, checkpoint_wait=120):
    # 1) Try a saved session first — fully automatic, no password, no CAPTCHA.
    if load_cookies(driver):
        return True

    log("→ Navigating to LinkedIn login page...")
    driver.get("https://www.linkedin.com/login")
    human_delay(3, 5)

    log(f"  Current URL: {driver.current_url}")
    log(f"  Page title:  {driver.title}")

    # Already logged in? (persistent Chrome profile / existing session)
    if "feed" in driver.current_url or "mynetwork" in driver.current_url:
        log("✅ Already logged in (existing session)")
        save_cookies(driver)
        return True

    if not email or not password:
        log("❌ No credentials available and no saved session — cannot log in")
        return False

    # Find a *visible, interactable* email field. presence alone is not enough:
    # LinkedIn keeps hidden duplicate inputs in the DOM (the old bug).
    email_field, sel = find_interactable_field(
        driver, ["#username", "input[name='session_key']", "input[type='email']"]
    )
    if not email_field:
        log("❌ Could not find email field — page may not have loaded")
        log(f"  Page source snippet: {driver.page_source[:500]}")
        return False
    log(f"  Found email field via '{sel}' (interactable)")

    slow_type(driver, email_field, email)
    human_delay()

    pwd_field, sel = find_interactable_field(
        driver, ["#password", "input[name='session_password']", "input[type='password']"]
    )
    if not pwd_field:
        log("❌ Could not find password field")
        return False
    log(f"  Found password field via '{sel}' (interactable)")

    slow_type(driver, pwd_field, password)
    human_delay()

    log("  Submitting login form...")
    try:
        pwd_field.send_keys(Keys.RETURN)
    except Exception:
        # Field wasn't interactable — click the submit button instead.
        try:
            btn = driver.find_element(By.CSS_SELECTOR, "button[type='submit']")
            btn.click()
        except Exception:
            log("❌ Could not submit login form")
            return False
    human_delay(4, 6)

    log(f"  Post-login URL: {driver.current_url}")

    if _confirm_logged_in(driver):
        log("✅ Logged in successfully")
        save_cookies(driver)
        return True
    elif "checkpoint" in driver.current_url or "challenge" in driver.current_url:
        return _handle_checkpoint(driver, interactive, checkpoint_wait)
    elif "login" in driver.current_url or "authwall" in driver.current_url:
        log("❌ Still on login page — wrong credentials or CAPTCHA")
        log(f"  Page title: {driver.title}")
        return False
    else:
        log(f"⚠️  Unexpected URL after login: {driver.current_url} — waiting 5s then checking...")
        human_delay(5, 6)
        if _confirm_logged_in(driver):
            log("✅ Logged in (delayed redirect)")
            save_cookies(driver)
            return True
        log("❌ Login failed")
        return False

def _handle_checkpoint(driver, interactive, checkpoint_wait):
    """LinkedIn threw a verification checkpoint (2FA / CAPTCHA / email code).

    We poll for it to auto-clear (e.g. app-approved 2FA) up to checkpoint_wait
    seconds. If it hasn't cleared and we're interactive, we hand control to the
    user. Non-interactive runs give up after the timeout.
    """
    log(f"⚠️  LinkedIn verification checkpoint — polling up to {checkpoint_wait}s for it to clear...")
    end = time.time() + checkpoint_wait
    while time.time() < end:
        if _confirm_logged_in(driver):
            log("✅ Checkpoint cleared automatically")
            save_cookies(driver)
            return True
        time.sleep(3)

    if interactive:
        log("→ Checkpoint still up. Complete the verification in the browser window.")
        input("Press Enter once you're on your LinkedIn feed...")
        if _confirm_logged_in(driver):
            save_cookies(driver)
            return True
        log("❌ Still not logged in after manual verification")
        return False

    log("❌ Checkpoint did not clear within timeout (non-interactive run)")
    return False

def _facet(values):
    return json.dumps([str(v) for v in values], separators=(",", ":"))

def _at_page_bottom(driver):
    try:
        return bool(driver.execute_script(
            "return window.innerHeight + window.scrollY >= document.body.scrollHeight - 200;"))
    except Exception:
        return True

def _page_text(driver):
    # innerText, not page_source: only text actually shown on the page counts.
    try:
        return (driver.execute_script(
            "return document.body ? document.body.innerText : '';") or "").lower()
    except Exception:
        return ""

def _search_block_reason(driver):
    """('auth', detail) on an authwall/checkpoint/login redirect, ('limit', detail)
    when LinkedIn shows its monthly / commercial-use search limit, else (None, '')."""
    url = (driver.current_url or "").lower()
    if any(m in url for m in ("/authwall", "/login", "/checkpoint", "/uas/")):
        return "auth", f"redirected to {driver.current_url.split('?')[0]}"
    text = _page_text(driver)
    for phrase in SEARCH_LIMIT_PHRASES:
        if phrase in text:
            return "limit", f"LinkedIn search limit ('{phrase}')"
    return None, ""

def _company_names(company):
    names = {company.get("name", ""), company.get("linkedin_name", ""), *company.get("aliases", [])}
    return {n.strip().lower() for n in names if n and n.strip() and len(n.strip()) >= 2}

# A company name in these role/skill contexts is a product or skill, not the
# person's employer ("Salesforce Developer", "AWS Cloud Engineer").
_NONEMPLOYER_AFTER = re.compile(
    r"\W*(?:developer|consultant|engineer|architect|admin|administrator|certified|"
    r"certification|cloud|dynamics|partner|specialist|expert|professional|skills?|"
    r"apis?|top voice)\b", re.I)
_EX_EMPLOYER = re.compile(
    r"\b(?:ex|former|formerly|prev|previously|alumni|alumnus|alumna)\b\W*$")

def _names_company(headline, company):
    """True only when the headline names the company (or an alias) as the person's
    current employer: the name follows an 'at'/'@' marker, is not a skill/product
    usage ('Salesforce Developer'), is not an 'ex-/former' employer, is not a
    'hiring for X' recruiting target, and no other current employer is named."""
    low = (headline or "").lower()
    names = _company_names(company)
    if not names:
        return False
    ours = other = False
    for m in re.finditer(r"(?P<mk>\bat\b|@|\bhiring for\b)\s*(?P<seg>[^|•·,;/]+)", low):
        marker, seg = m.group("mk"), m.group("seg").strip()
        if not seg:
            continue
        hit = next((n for n in sorted(names, key=len, reverse=True)
                    if re.match(rf"{re.escape(n)}(?![a-z0-9])", seg)), None)
        if hit:
            if _NONEMPLOYER_AFTER.match(seg[len(hit):]):
                continue
            if _EX_EMPLOYER.search(low[:m.start("seg")]):
                continue
            if marker == "hiring for":
                continue
            ours = True
        elif marker != "hiring for":
            other = True
    return ours and not other

def search_people(driver, company, role, geo="india", page=1, network=None, company_id=None,
                  source="connect", dump_on_empty=True, stats=None):
    """Load one people-search results page and return EVERY parsed person on it
    (saving and dedupe are the caller's job). With a LinkedIn company id the
    currentCompany facet filters by current employer, so every result counts as
    verified; without one it searches '<role> <linkedin_name>' as keywords and
    verifies a person only when their headline names the company. geo 'india'
    adds the India geoUrn facet; 'global' has no geo filter. `stats` (a dict),
    when given, receives 'loaded' and the raw 'cards' count."""
    results = []
    params = {"keywords": role if company_id
              else " ".join(x for x in (role, company.get("linkedin_name")) if x)}
    if company_id:
        params["currentCompany"] = _facet([company_id])
    if geo == "india":
        params["geoUrn"] = _facet([INDIA_GEO_URN])
    if network:
        params["network"] = _facet(network)
    params["origin"] = "FACETED_SEARCH"
    params["page"] = str(page)
    url = ("https://www.linkedin.com/search/results/people/?"
           + urllib.parse.urlencode(params, quote_via=urllib.parse.quote))

    log(f"→ Searching people [{geo} · {'company facet' if company_id else 'keywords'} · "
        f"page {page}]: {role} @ {company.get('name')}")
    log(f"  URL: {url}")
    if stats is not None:
        stats.update(loaded=False, cards=0)
    if not _get_with_retry(driver, url):
        return results
    if stats is not None:
        stats["loaded"] = True
    human_delay(3, 5)

    # Search results are lazy-loaded / virtualized — scroll to force render.
    for i in range(8):
        scroll_down(driver, 700)
        if i >= 3 and _at_page_bottom(driver):
            break
    driver.execute_script("window.scrollTo(0, 0);")
    human_delay(1, 2)

    try:
        # ── Resilient card discovery ────────────────────────────────────────
        # LinkedIn's result-card CSS classes are obfuscated and change often
        # (the old .reusable-search__result-container / .entity-result__* are
        # dead). The ONE thing that can't change without breaking LinkedIn's
        # own product: every person result links to a "/in/<id>" profile. So
        # we anchor on those links and walk up to the enclosing card.
        cards = _find_result_cards(driver)
        log(f"  Raw result cards: {len(cards)}")
        if stats is not None:
            stats["cards"] = len(cards)

        if not cards and dump_on_empty:
            # self-diagnose selector breakage
            _dump_search_dom(driver, company.get("key") or "search")

        seen_ids = set()
        for i, card in enumerate(cards):
            try:
                person = _parse_result_card(card, role)
                if not person or person["id"] in seen_ids:
                    continue
                seen_ids.add(person["id"])
                # The India geo facet guarantees the person is in India, so trust
                # it over a location line we could not fully parse.
                if geo == "india" and person.get("country") != "India":
                    person["country"] = "India"
                person.update({
                    "company": company.get("name", ""),
                    "company_key": company.get("key") or None,
                    "company_verified": 1 if company_id else int(_names_company(person["title"], company)),
                    "source": source,
                    "search_geo": geo,
                    "status": "found",
                })
                results.append(person)
            except Exception as e:
                log(f"  Card {i+1}: parse error — {e}")
                continue

    except Exception as e:
        log(f"  Search error: {e}")

    log(f"  → {len(results)} people parsed for {company.get('name')}")
    return results

def _outside_page_chrome(driver, elements):
    """Drop elements in the global nav or a side rail: their profile links are
    not search results and must never be attributed to the searched company."""
    if not elements:
        return []
    try:
        return driver.execute_script(
            "return arguments[0].filter(function (e) {"
            " return !e.closest('#global-nav, .global-nav, nav, aside'); });", elements) or []
    except Exception:
        return elements

def _find_result_cards(driver):
    """Locate person-result cards resiliently.

    Strategy: find every anchor pointing at a "/in/" profile inside the search
    results, then climb to the nearest <li> (result cards are list items). If
    the <li> ancestor can't be found, fall back to the link's grandparent. De-
    duplicates by the resolved card element. Also tries a few known container
    selectors first as a fast path.
    """
    # Fast path: known container selectors (kept as best-effort, not required).
    # Ordering reflects the current (2025-2026) LinkedIn DOM, corroborated
    # across several maintained scrapers: the result cards carry the stable
    # attribute data-view-name="search-entity-result-universal-template".
    # Legacy class containers kept as fallbacks.
    for sel in [
        "[data-view-name='search-entity-result-universal-template']",
        "div.search-results-container li",
        "ul[role='list'] > li",
        "li.reusable-search__result-container",
        ".reusable-search__result-container",
    ]:
        try:
            found = driver.find_elements(By.CSS_SELECTOR, sel)
            # Only accept if these actually contain profile links.
            good = _outside_page_chrome(
                driver, [c for c in found if c.find_elements(By.CSS_SELECTOR, "a[href*='/in/']")])
            if len(good) >= 2:
                return good
        except Exception:
            continue

    # Resilient path: anchor on profile links, climb to the card.
    cards, seen = [], set()
    links = _outside_page_chrome(driver, driver.find_elements(By.CSS_SELECTOR, "a[href*='/in/']"))
    for link in links:
        try:
            # Climb to the nearest <li> ancestor (result cards are list items).
            card = None
            try:
                card = link.find_element(By.XPATH, "./ancestor::li[1]")
            except Exception:
                try:
                    card = link.find_element(By.XPATH, "./ancestor::div[3]")
                except Exception:
                    card = link
            key = card.id
            if key not in seen:
                seen.add(key)
                cards.append(card)
        except Exception:
            continue
    return cards

FIRST_DEGREE_NOTE = "1st-degree connection"  # already connected: never an invite target

_INDIA_CITY_ALIASES = {
    "bengaluru": "Bengaluru", "bangalore": "Bengaluru", "hyderabad": "Hyderabad",
    "pune": "Pune", "chennai": "Chennai", "mumbai": "Mumbai",
    "gurugram": "Gurugram", "gurgaon": "Gurugram", "noida": "Noida",
    "new delhi": "New Delhi", "delhi": "Delhi", "kolkata": "Kolkata",
    "ahmedabad": "Ahmedabad", "kochi": "Kochi",
    "thiruvananthapuram": "Thiruvananthapuram", "trivandrum": "Thiruvananthapuram",
    "coimbatore": "Coimbatore", "jaipur": "Jaipur", "chandigarh": "Chandigarh",
    "indore": "Indore", "mysuru": "Mysuru", "mysore": "Mysuru",
    "bhubaneswar": "Bhubaneswar", "nagpur": "Nagpur", "visakhapatnam": "Visakhapatnam",
    "vadodara": "Vadodara", "surat": "Surat", "lucknow": "Lucknow",
}
_INDIA_CITY_RE = re.compile(r"\b(" + "|".join(
    re.escape(a) for a in sorted(_INDIA_CITY_ALIASES, key=len, reverse=True)) + r")\b", re.I)

def _parse_location(location):
    """(city, country) from a LinkedIn location line. Anything naming India or
    an Indian city on its own is India; otherwise the last comma part is the
    country (best effort)."""
    loc = (location or "").strip()
    if not loc:
        return "", ""
    parts = [p.strip() for p in loc.split(",") if p.strip()]
    m = _INDIA_CITY_RE.search(loc)
    city = _INDIA_CITY_ALIASES[m.group(1).lower()] if m else (parts[0] if len(parts) > 1 else "")
    if re.search(r"\bindia\b", loc, re.I) or (m and len(parts) == 1):
        return city, "India"
    if len(parts) > 1:
        return city, parts[-1]
    if re.search(r"\b(area|region|metropolitan|metro|remote)\b", loc, re.I):
        return city, ""
    return city, loc

_HONORIFICS = {"dr", "mr", "mrs", "ms", "miss", "prof", "er", "adv"}
_VARIATION_SELECTORS = {chr(0xFE0E), chr(0xFE0F)}
_NAME_SUFFIXES = re.compile(
    r"^(?:phd|mba|pmp|csm|cspo|shrm-?s?cp|sphr|phr|cpa|cfa|cissp|msc|mtech|btech|"
    r"jr|sr|ii|iii|iv)$", re.I)

def _split_name(name):
    """(first, last) from a display name, minus emojis, pronoun tags, hashtags,
    handles, credentials, honorifics and anything after '|'."""
    s = re.split(r"\||\s[-–—]\s", name or "", maxsplit=1)[0].split(",")[0]
    s = re.sub(r"[\(\[\{][^\)\]\}]*[\)\]\}]", " ", s)
    s = re.sub(r"\b(?:she|he|they)\s*/\s*(?:her|hers|him|his|them|theirs)\b", " ", s, flags=re.I)
    # An emoji or other symbol is a hard separator (e.g. a name followed by a
    # 👋 and a headline); keep only what precedes the first one.
    cut = next((i for i, ch in enumerate(s)
                if ch not in _VARIATION_SELECTORS and unicodedata.category(ch).startswith("S")),
               len(s))
    s = s[:cut]
    # Drop hashtags ('#OpenToWork'), handles ('@acme') and slash tokens (leftover
    # pronoun pairs) before the character filter strips their markers.
    s = " ".join(t for t in s.split()
                 if not t.startswith(("#", "@")) and "/" not in t)
    s = "".join(ch if unicodedata.category(ch)[0] in "LM" or ch in " .'-" else " "
                for ch in s if ch not in _VARIATION_SELECTORS)
    tokens = [t for t in s.split() if t.strip(".-'")]
    while len(tokens) > 1 and tokens[0].rstrip(".").lower() in _HONORIFICS:
        tokens.pop(0)
    while len(tokens) > 1 and _NAME_SUFFIXES.match(tokens[-1].replace(".", "")):
        tokens.pop()
    if not tokens:
        return "", ""
    first = tokens[0].strip(".")
    last = tokens[-1].strip(".") if len(tokens) > 1 else ""
    return first, last

def _is_card_noise(line, name):
    low = line.lower()
    if line == name or line in ("•", "·"):
        return True
    if low in ("connect", "message", "follow", "pending", "following"):
        return True
    if low.startswith(("view ", "message ", "status is", "current:", "past:", "summary:",
                       "skills:", "provides services")):
        return True
    if name and low.startswith(name.lower()) and re.search(r"[•·]|\b(?:1st|2nd|3rd)\b", low):
        return True
    # Connection-degree markers: "• 1st", "2nd", "· 3rd+", "3rd+ degree connection".
    if re.match(r"^[•·]?\s*(1st|2nd|3rd)\+?(\s+degree(\s+connection)?)?$", low):
        return True
    return ("mutual connection" in low or "degree connection" in low
            or bool(re.search(r"\d[\d,.]*\s*[km]?\s+followers\b", low)))

def _parse_result_card(card, role_default):
    """Extract one person from a result card: id, name, url, title (headline),
    location, first/last name, city/country, and a note for 1st-degree ones.

    Returns None if the card isn't a usable person result (no /in/ link, hidden
    member, etc.). Uses the profile link as the anchor and derives the name from
    the link text, then reads remaining text lines for title/location.
    """
    link_el = None
    for a in card.find_elements(By.CSS_SELECTOR, "a[href*='/in/']"):
        href = a.get_attribute("href") or ""
        if "/in/" in href:
            link_el = a
            break
    if not link_el:
        return None

    profile_url = link_el.get_attribute("href").split("?")[0]
    linkedin_id = profile_url.rstrip("/").split("/")[-1]

    # Name: prefer the aria-hidden span (LinkedIn renders the visible name
    # there), else the link's own text, cleaned of "View X's profile" noise.
    name = ""
    try:
        name = link_el.find_element(By.CSS_SELECTOR, "span[aria-hidden='true']").text.strip()
    except Exception:
        pass
    if not name:
        name = (link_el.text or "").strip()
    name = name.split("\n")[0].strip()
    # Strip a trailing connection-degree marker that sometimes rides along in
    # the fallback link text, e.g. "Wessel Strengholt • 3rd+".
    name = re.sub(r"\s*[•·]\s*(1st|2nd|3rd)\+?\s*$", "", name).strip()
    if not name or name.lower() == "linkedin member" or name.lower().startswith("view "):
        return None

    # Title / location: read the card's non-empty text lines after the name,
    # dropping LinkedIn's noise lines (connection degree '• 3rd+', action
    # buttons, "View X's profile", the duplicated name, mutual connections).
    raw = [l.strip() for l in (card.text or "").split("\n") if l.strip()]
    lines = [l for l in raw if not _is_card_noise(l, name)]
    degree = next((m.group(1) for m in (
        re.search(r"(?:^|[•·]\s*)(1st|2nd|3rd)\+?(?:\s+degree(?:\s+connection)?)?$", l.lower())
        for l in raw) if m), "")

    title = lines[0] if lines else role_default
    # Location: prefer a line that looks like a place (a comma, India or an
    # Indian city, or a metro 'Area'). Else the 2nd line. Else unknown.
    location = ""
    for l in lines[1:]:
        if ("," in l or _INDIA_CITY_RE.search(l) or re.search(r"\bindia\b", l, re.I)
                or re.search(r"\b(area|region)\b", l, re.I)):
            location = l
            break
    if not location and len(lines) > 1:
        location = lines[1]

    first, last = _split_name(name)
    city, country = _parse_location(location)
    return {"id": linkedin_id, "name": name, "url": profile_url, "title": title,
            "location": location, "first_name": first, "last_name": last,
            "city": city, "country": country,
            "notes": FIRST_DEGREE_NOTE if degree == "1st" else None}

def _dump_search_dom(driver, tag):
    """When 0 cards are found, dump a DOM sample so selector breakage is
    diagnosable instead of silently returning nothing."""
    try:
        path = Path.home() / f"linkedin_search_dump_{tag}.html"
        html = driver.page_source
        path.write_text(html)  # full page — feed content is often past 200KB
        n_in = html.count('/in/')
        log(f"  ⚠️  0 cards — dumped DOM to {path} (page has {n_in} '/in/' refs, {len(html)} bytes)")
        if "authwall" in driver.current_url or "login" in driver.current_url:
            log("  ⚠️  Bounced to auth wall — session not actually logged in for search")
    except Exception as e:
        log(f"  (could not dump DOM: {e})")

# ─── COMPANY IDS ────────────────────────────────────────────────────────────
_COMPANY_URN_RE = re.compile(r"urn:li:(?:fsd_|fs_mini)?[Cc]ompany:(\d+)")

def _is_recent(ts, days):
    try:
        return datetime.now() - datetime.fromisoformat(ts) < timedelta(days=days)
    except (TypeError, ValueError):
        return False

def _pick_company_hit(data, names):
    """(id, name) of the typeahead hit whose name matches, else (None, '').
    When several matching hits carry DIFFERENT company ids the name is ambiguous
    (e.g. a generic 'Sierra' or 'Runway'), so refuse rather than pick the first —
    a wrong id would mark every facet result verified at that company."""
    elements = []
    if isinstance(data, dict):
        inner = data.get("data") if isinstance(data.get("data"), dict) else {}
        for src in (data.get("elements"), inner.get("elements"), data.get("included")):
            if isinstance(src, list):
                elements += [e for e in src if isinstance(e, dict)]
    hits = {}
    for el in elements:
        label = el.get("text")
        if isinstance(label, dict):
            label = label.get("text")
        label = label or el.get("name")
        if not isinstance(label, str) or label.strip().lower() not in names:
            continue
        m = _COMPANY_URN_RE.search(json.dumps(el))
        if m:
            hits.setdefault(m.group(1), label.strip())
    if len(hits) > 1:
        log(f"  ⚠️  Ambiguous company typeahead ({len(hits)} ids for a matching name) — "
            "refusing to guess; keyword fallback")
        return None, ""
    if hits:
        cid, label = next(iter(hits.items()))
        return cid, label
    return None, ""

def _typeahead_company_id(driver, query, names):
    """(id, matched, clean) from LinkedIn's company typeahead, fetched in-page.
    `clean` is True only when the lookup ran and simply found no match (a genuine
    miss worth caching); a missing token, a non-200 response or any error returns
    clean=False so the caller does not cache a transient failure as a miss."""
    try:
        if "linkedin.com" not in (driver.current_url or ""):
            _get_with_retry(driver, "https://www.linkedin.com/feed/")
            human_delay(2, 3)
        # Voyager rejects calls whose csrf-token header isn't the JSESSIONID value.
        token = ((driver.get_cookie("JSESSIONID") or {}).get("value") or "").strip('"')
        if not token:
            return None, "", False
        path = ("/voyager/api/typeahead/hitsV2?keywords=" + urllib.parse.quote(query, safe="")
                + "&origin=OTHER&q=type&type=COMPANY")
        driver.set_script_timeout(30)
        status, body = driver.execute_async_script("""
            var done = arguments[arguments.length - 1];
            fetch(arguments[0], {credentials: 'include', headers: {
                    'csrf-token': arguments[1], 'x-restli-protocol-version': '2.0.0'}})
                .then(function (r) { return r.text().then(function (t) { done([r.status, t]); }); })
                .catch(function (e) { done([0, String(e)]); });
        """, path, token)
        if status != 200:
            log_debug(f"typeahead {query!r}: HTTP {status}")
            return None, "", False
        cid, matched = _pick_company_hit(json.loads(body), names)
        return cid, matched, True
    except Exception as e:
        log_debug(f"typeahead {query!r} failed: {e}")
        return None, "", False

def _company_id_from_pages(driver, query, names):
    """(id, matched, clean) from the companies search page, then the company page
    source. `clean` is True only when both loads succeeded and no match was found
    (a genuine miss); a failed page load returns clean=False."""
    url = ("https://www.linkedin.com/search/results/companies/?"
           + urllib.parse.urlencode({"keywords": query}, quote_via=urllib.parse.quote))
    if not _get_with_retry(driver, url):
        return None, "", False
    human_delay(3, 5)
    slug, matched = "", ""
    try:
        for a in driver.find_elements(By.CSS_SELECTOR, "a[href*='/company/']"):
            label = (a.text or "").strip().split("\n")[0].strip()
            m = re.search(r"/company/([^/?#]+)", a.get_attribute("href") or "")
            if m and label.lower() in names:
                slug, matched = m.group(1), label
                break
    except Exception:
        pass
    if not slug:
        return None, "", True
    if slug.isdigit():
        return slug, matched, True
    if not _get_with_retry(driver, f"https://www.linkedin.com/company/{slug}/"):
        return None, "", False
    human_delay(3, 5)
    ids = _COMPANY_URN_RE.findall(driver.page_source or "")
    if ids:
        return collections.Counter(ids).most_common(1)[0][0], matched, True
    return None, "", True

def _resolve_company(driver, conn, company, persist=True, retry_failed=False):
    """(company id or None, matched name, how it was found).

    Only a lookup that ran cleanly and found no match is cached as a miss;
    transient failures (missing token, non-200, page-load failure) are not, so a
    429 or a dropped session does not silently disable a company for 3 days. The
    companies-search fallback loads real search pages, so it is skipped when
    search is blocked or the daily page ceiling is spent, and every page it does
    load is counted toward that ceiling."""
    if company.get("linkedin_company_id"):
        return company["linkedin_company_id"], company.get("linkedin_name", ""), "registry"
    key = company.get("key")
    row = conn.execute("SELECT linkedin_company_id, matched_name, resolved_at FROM company_ids "
                       "WHERE company_key=?", (key,)).fetchone() if key else None
    if row and row[0]:
        return row[0], row[1] or "", "cache"
    if row and not retry_failed and _is_recent(row[2], COMPANY_ID_RETRY_DAYS):
        return None, "", "cached miss"
    names = _company_names(company)
    cid, matched, clean = _typeahead_company_id(driver, company["linkedin_name"], names)
    how = "typeahead"
    if not cid:
        # The companies-search + company-page path loads two real search pages.
        # Skip it once the daily ceiling is spent or search is blocked.
        budget_left = HARVEST_MAX_PAGES_PER_DAY - harvest_pages_today(conn)
        if harvest_blocked_until(conn) or budget_left <= 0:
            how = "unresolved (page budget spent)"
        else:
            cid, matched, page_clean = _company_id_from_pages(driver, company["linkedin_name"], names)
            clean = clean and page_clean
            how = "company pages" if cid else "unresolved"
            if persist:
                _log_harvest(conn, pages=1)
    log(f"  Company id for {company['name']}: "
        + (f"{cid} ({matched}, via {how})" if cid else "unresolved — keyword search fallback"))
    # Cache a hit always; cache a miss only when the lookup was clean (genuine
    # no-match), so a transient failure is retried next run instead of in 3 days.
    if persist and key and (cid or clean):
        conn.execute("INSERT OR REPLACE INTO company_ids "
                     "(company_key, linkedin_company_id, matched_name, resolved_at) "
                     "VALUES (?, ?, ?, ?)",
                     (key, cid, matched or None, datetime.now().isoformat(timespec="seconds")))
        conn.commit()
    return cid, matched, how

def resolve_company_id(driver, conn, company, persist=True):
    """LinkedIn numeric company id for a registry company, or None (search then
    falls back to keywords). The registry's linkedin_company_id wins; lookups
    are cached in company_ids, and a miss is retried after COMPANY_ID_RETRY_DAYS."""
    return _resolve_company(driver, conn, company, persist=persist)[0]

def resolve_companies(driver, conn, companies, dry_run=False):
    """--resolve-companies: resolve every registry company and print key -> id."""
    log(f"\n🏢 RESOLVE COMPANY IDS — {len(companies)} companies")
    resolved = 0
    for company in companies:
        cid, matched, how = _resolve_company(driver, conn, company, persist=not dry_run,
                                             retry_failed=True)
        resolved += bool(cid)
        print(f"{company['key']} -> {cid or 'UNRESOLVED'}"
              + (f"  ({matched}; {how})" if cid else ""), flush=True)
        if how in ("company pages", "unresolved"):
            human_delay(20, 60)   # loaded real search pages — pace like harvest
        elif how == "typeahead":
            human_delay(2, 4)     # lightweight in-page API call
    log(f"  → {resolved}/{len(companies)} companies resolved")

def _find_connect_button_element(driver):
    """Find the Connect button on a profile page.

    LinkedIn shows Connect directly on some profiles, but hides it behind a
    '…More' (three-dot) dropdown on others — especially when the primary CTA
    is Follow or Message. We try both paths and use aria-label as the primary
    signal (more reliable than button text on SDUI)."""
    from selenium.webdriver.common.action_chains import ActionChains

    def _is_connect(btn):
        label = (btn.get_attribute("aria-label") or "").lower()
        text  = (btn.text or "").strip().lower()
        return "connect" in label or text in ("connect",)

    # Path 1: Find the profile-owner's Connect button by aria-label containing their name,
    # or any Connect button in the top 400px (profile action bar area).
    all_connect = []
    for btn in driver.find_elements(By.CSS_SELECTOR, "button"):
        label = (btn.get_attribute("aria-label") or "").lower()
        text  = (btn.text or "").strip().lower()
        if "connect" not in text and "connect" not in label:
            continue
        try:
            if not btn.is_displayed():
                continue
            y = btn.location.get("y", 9999)
            all_connect.append((y, btn, text, label))
        except Exception:
            pass

    log(f"  All Connect buttons: {[(y, t, l[:50]) for y,_,t,l in sorted(all_connect)[:8]]}")

    # Pick the profile-owner's Connect button — identified by their name in aria-label
    # (e.g. 'invite yusuf keten to connect') which is always in the action bar.
    # Exclude "People you may know" buttons which say 'invite <other person> to connect'.
    # Strategy: pick the Connect button with the LOWEST y (topmost = profile action bar).
    # "People you may know" buttons are always at y > 1000px (below the fold).
    for y, btn, text, label in sorted(all_connect):
        # Skip obviously wrong buttons — "People you may know" section is always > 800px
        if y > 800:
            continue
        log(f"  Using Connect button at y={y} label={label!r}")
        return btn

    # Nothing in top 800px — fall through to More dropdown.
    if all_connect:
        log(f"  No Connect button above 800px (lowest is y={sorted(all_connect)[0][0]}) — trying More dropdown")

    # Path 2: hidden behind the '…More' / three-dot dropdown.
    log("  'Connect' not visible — trying '…More' dropdown...")
    more_btn = None
    for btn in driver.find_elements(By.CSS_SELECTOR, "button"):
        label = (btn.get_attribute("aria-label") or "").lower()
        text  = (btn.text or "").strip().lower()
        if ("more" in label and "action" in label) or text == "more":
            more_btn = btn
            break

    if not more_btn:
        # Fallback: button whose aria-label contains "more actions"
        try:
            more_btn = driver.find_element(
                By.CSS_SELECTOR, "button[aria-label*='More actions']")
        except Exception:
            pass

    if more_btn and more_btn.is_displayed():
        log(f"  Found More button: aria={more_btn.get_attribute('aria-label')!r} text={more_btn.text.strip()!r}")
        from selenium.webdriver.common.action_chains import ActionChains
        import pyautogui

        # Step 1: Open More dropdown via ActionChains.
        ActionChains(driver).move_to_element(more_btn).pause(0.4).click().perform()
        time.sleep(1.0)

        # Step 2: Find Connect item in dropdown.
        connect_item = None
        for sel in ['.artdeco-dropdown__content [role="menuitem"]', '[role="menu"] [role="menuitem"]']:
            for el in driver.find_elements(By.CSS_SELECTOR, sel):
                t = (el.text or el.get_attribute("innerText") or "").strip().lower()
                if t == "connect" or t.startswith("connect"):
                    connect_item = el
                    break
            if connect_item:
                break

        if not connect_item:
            log("  ⚠️  Connect item not found in dropdown after opening")
            return None

        log(f"  Found Connect item: {connect_item.text.strip()!r}")

        # Step 3: Click Connect via ActionChains (keeps focus in automation window).
        ActionChains(driver).move_to_element(connect_item).pause(0.3).click().perform()
        log("  Clicked Connect — waiting 2s for modal...")
        time.sleep(2.0)

        # Step 4: Press Tab+Enter to click 'Send without a note' in modal.
        # Tab order in modal: X (close) → Add a note → Send without a note
        # So we need 3 Tabs to reach 'Send without a note'.
        from selenium.webdriver.common.keys import Keys
        try:
            active = driver.switch_to.active_element
            active.send_keys(Keys.TAB)   # → X (close)
            time.sleep(0.2)
            active = driver.switch_to.active_element
            active.send_keys(Keys.TAB)   # → Add a note
            time.sleep(0.2)
            active = driver.switch_to.active_element
            active.send_keys(Keys.TAB)   # → Send without a note
            time.sleep(0.2)
            active = driver.switch_to.active_element
            active.send_keys(Keys.RETURN)
            log("  ✅ Sent Tab×3+Enter → 'Send without a note'")
        except Exception as e:
            log(f"  Tab+Enter via Selenium failed ({e}) — trying pyautogui keyboard")
            import pyautogui
            pyautogui.press('tab')
            time.sleep(0.2)
            pyautogui.press('tab')
            time.sleep(0.2)
            pyautogui.press('tab')
            time.sleep(0.2)
            pyautogui.press('enter')
            log("  ✅ Sent Tab×3+Enter via pyautogui")
        time.sleep(1.5)
        return True

    return None

def _pyautogui_click(driver, element):
    """Click an element using real OS-level mouse via pyautogui."""
    import pyautogui
    try:
        info = driver.execute_script("""
            var r = arguments[0].getBoundingClientRect();
            return {
                x: Math.round(r.left + r.width/2),
                y: Math.round(r.top  + r.height/2),
                screenX: window.screenX,
                screenY: window.screenY,
                chromeH: window.outerHeight - window.innerHeight
            };
        """, element)
        sx = info['screenX'] + info['x']
        sy = info['screenY'] + info['chromeH'] + info['y']
        log(f"  pyautogui: viewport=({info['x']},{info['y']}) win=({info['screenX']},{info['screenY']}) chromeH={info['chromeH']} → screen=({sx},{sy})")
        # Ensure Chrome window is in front before clicking.
        driver.execute_script("window.focus();")
        time.sleep(0.1)
        pyautogui.moveTo(sx, sy, duration=0.2)
        time.sleep(0.1)
        pyautogui.click()
        return True
    except Exception as e:
        log(f"  pyautogui click failed: {e}")
        return False

def send_connection(driver, person, conn):
    log(f"→ Visiting profile: {person['name']} ({person['url']})")
    try:
        driver.get(person["url"])
        human_delay(2, 4)
        scroll_down(driver, 200)
        human_delay()

        btn_labels = [b.text.strip() for b in driver.find_elements(By.CSS_SELECTOR, "button")]
        log(f"  Buttons on page: {[b for b in btn_labels if b][:12]}")

        connect_btn = _find_connect_button_element(driver)

        if not connect_btn:
            log(f"  No Connect button found for {person['name']} — skipping")
            return False

        if connect_btn is not True:
            # Direct button in action bar — click via pyautogui.
            log("  Clicking Connect via pyautogui...")
            _pyautogui_click(driver, connect_btn)
            time.sleep(2.0)
            # Use Tab×3+Enter to click 'Send without a note' in modal.
            from selenium.webdriver.common.keys import Keys
            try:
                active = driver.switch_to.active_element
                for _ in range(3):
                    active.send_keys(Keys.TAB)
                    time.sleep(0.2)
                    active = driver.switch_to.active_element
                active.send_keys(Keys.RETURN)
                log("  ✅ Tab×3+Enter → 'Send without a note'")
            except Exception as e:
                import pyautogui as pg
                for _ in range(3):
                    pg.press('tab'); time.sleep(0.2)
                pg.press('enter')
                log("  ✅ Tab×3+Enter via pyautogui")
            time.sleep(1.5)
            update_status(conn, person["id"], "requested")
            log(f"✅ Connection sent to {person['name']} at {person['company']}")
            return True
        else:
            # Dropdown path already handled More → Connect → Send without a note.
            update_status(conn, person["id"], "requested")
            log(f"✅ Connection sent to {person['name']} at {person['company']}")
            return True

        log("  Polling for modal...")
        sent_directly = False
        for _ in range(20):
            found = driver.execute_script("""
                var all = Array.from(document.querySelectorAll('button, a, div[role="button"], span[role="button"]'));
                for (var i = 0; i < all.length; i++) {
                    var b = all[i];
                    var t = (b.innerText || b.textContent || '').trim().toLowerCase();
                    var al = (b.getAttribute('aria-label') || '').toLowerCase();
                    var r = b.getBoundingClientRect();
                    if (r.width === 0 || r.height === 0) continue;
                    if (t.indexOf('send without') !== -1 || al.indexOf('send without') !== -1)
                        return {type: 'send_without', el: b};
                    if (t.indexOf('add a note') !== -1 || al.indexOf('add a note') !== -1)
                        return {type: 'add_note', el: b};
                }
                return null;
            """)
            if found:
                btn_type = found['type']
                btn_el   = found['el']
                log(f"  Modal button found: '{btn_type}' — clicking via pyautogui")
                _pyautogui_click(driver, btn_el)
                human_delay(1.5, 2.5)
                sent_directly = True
                break
            time.sleep(0.3)

        if sent_directly:
            page_btns = [(b.get_attribute("aria-label") or b.text or "").lower()
                         for b in driver.find_elements(By.CSS_SELECTOR, "button")]
            log(f"  Post-send buttons: {[b for b in page_btns if b][:8]}")
            if any("pending" in b or "withdraw" in b for b in page_btns):
                update_status(conn, person["id"], "requested")
                log(f"✅ Connection sent to {person['name']} at {person['company']}")
                return True
            update_status(conn, person["id"], "requested")
            log(f"✅ Connection sent to {person['name']} (clicked Send — verify on LinkedIn)")
            return True

        log("  ⚠️  Modal did not appear after 6s")
        page_btns = [(b.get_attribute("aria-label") or b.text or "").lower()
                     for b in driver.find_elements(By.CSS_SELECTOR, "button") if b.is_displayed()]
        log(f"  Current page buttons: {[b for b in page_btns if b][:10]}")
        if any("pending" in b or "withdraw" in b for b in page_btns):
            log("  ✅ Already pending (invite sent earlier)")
            update_status(conn, person["id"], "requested")
            return True
        return False

    except Exception as e:
        log(f"❌ Failed to connect with {person['name']}: {e}")
        return False

def send_followup_messages(driver, conn):
    pending = get_pending_messages(conn)
    if not pending:
        log("No pending follow-up messages")
        return

    log(f"\n→ Sending follow-up messages to {len(pending)} connected people...")

    for linkedin_id, name, title, company, profile_url in pending:
        log(f"  → Messaging {name} at {company}")
        try:
            driver.get(profile_url)
            human_delay(2, 3)

            msg_btn = None
            for btn in driver.find_elements(By.CSS_SELECTOR, "button"):
                if btn.text.strip() == "Message":
                    msg_btn = btn
                    break

            if not msg_btn:
                log(f"  No Message button for {name} — skipping")
                continue

            msg_btn.click()
            human_delay()

            msg = random.choice(FOLLOW_UP_TEMPLATES).format(
                name=name.split()[0],
                company=company,
                title=title or "engineer"
            )

            msg_box = WebDriverWait(driver, 5).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, ".msg-form__contenteditable"))
            )
            slow_type(driver, msg_box, msg)
            human_delay()

            send_btn = driver.find_element(By.CSS_SELECTOR, ".msg-form__send-button")
            send_btn.click()
            human_delay(1, 2)

            update_status(conn, linkedin_id, "messaged", msg)
            log(f"✅ Message sent to {name} at {company}")
            human_delay(3, 6)

        except Exception as e:
            log(f"❌ Failed to message {name}: {e}")

# ─── OLLAMA COMMENT GENERATION ──────────────────────────────────────────────

def _ollama_generate(prompt, num_predict=64, temperature=0.7, timeout=30):
    """One Ollama completion. Returns the raw text, or None when Ollama itself
    is unreachable — distinct from "" (reached, but produced nothing usable),
    because only the latter is worth retrying."""
    payload = json.dumps({
        "model": OLLAMA_MODEL,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": temperature, "num_predict": num_predict},
    }).encode()
    try:
        req = urllib.request.Request(
            f"{OLLAMA_URL}/api/generate", data=payload,
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode())
        return (data.get("response") or "").strip()
    except urllib.error.URLError as e:
        log(f"  ⚠️  Ollama unreachable at {OLLAMA_URL} ({e})")
    except Exception as e:
        log(f"  ⚠️  Ollama error ({e})")
    return None

def _strip_decoration(text, single_line=True):
    """Shared cosmetic clean-up: unwrapped, no emoji, no hashtags or @mentions,
    no doubled whitespace. A comment must be one line; a DM may legitimately
    wrap, so its lines are joined rather than dropped."""
    text = (text or "").strip()
    text = text.split("\n")[0].strip() if single_line else " ".join(text.split())
    text = re.sub(r"^(?:comment|reply|response|message)\s*:\s*", "", text, flags=re.I)
    text = text.strip().strip('"').strip("'").strip("“”‘’").strip()
    text = re.sub(r"[\U0001F000-\U0001FAFF←-➿️]", "", text)
    text = re.sub(r"[@#]\w+", "", text)
    return re.sub(r"\s{2,}", " ", text).strip(" ,;:-—").strip()

_COMMENT_BANNED = ("as an ai", "language model", "here is", "here's a", "sure,",
                   "certainly,", "i cannot", "i can't", "http://", "https://",
                   "www.", "```", "[", "]", "{", "}")

def _sanitize_comment(text, author=""):
    """Return a clean, name-free comment, or "" if the model's output isn't fit
    to post. These are reject rules, not repair rules — a comment we'd have to
    rewrite is a comment we shouldn't post."""
    text = _strip_decoration(text)
    if not text:
        return ""
    lowered = text.lower()
    if any(bad in lowered for bad in _COMMENT_BANNED):
        return ""
    # The name-free rule: any token of the poster's name appearing in the
    # comment means the model addressed them personally.
    for token in re.findall(r"[A-Za-z]{3,}", author or ""):
        if re.search(rf"\b{re.escape(token)}\b", text, re.I):
            return ""
    if re.match(r"^(hi|hey|hello|dear|congrats|congratulations)\b", text, re.I):
        return ""
    sentences = [s for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]
    if len(sentences) > 2:
        text = sentences[0].strip()
    words = text.split()
    if not (4 <= len(words) <= 30) or len(text) > 220:
        return ""
    if not re.search(r"[A-Za-z]", text):
        return ""
    # Local models occasionally leak CJK/Cyrillic/Indic fragments mid-sentence.
    if re.search(r"[\u0400-\u04ff\u0590-\u06ff\u0900-\u0dff\u0e00-\u0e7f\u1100-\u11ff\u3000-\u9fff\uac00-\ud7af\uf900-\ufaff]", text):
        return ""
    if text[-1] not in ".!?":
        text += "."
    return text

_LAST_FALLBACK = {"text": ""}

def _fallback_comment():
    if not COMMENT_ALLOW_FALLBACK:
        return ""
    pool = [c for c in FALLBACK_COMMENTS if c != _LAST_FALLBACK["text"]] or FALLBACK_COMMENTS
    _LAST_FALLBACK["text"] = random.choice(pool)
    return _LAST_FALLBACK["text"]

def generate_comment(post_text, author=""):
    """Generate a short, human, name-free comment about `post_text`.

    Returns "" when nothing safe could be produced — the caller then likes the
    post without commenting. `author` is deliberately NEVER put in the prompt;
    it is passed only so the sanitizer can reject output that names the poster.
    """
    post_text = re.sub(r"\s+", " ", (post_text or "")).strip()
    if len(post_text) < COMMENT_MIN_POST_CHARS:
        return _fallback_comment()

    prompt = (
        "Write a very short LinkedIn comment as a peer software engineer "
        "reacting to the post below. STRICT rules: ONE sentence, 6 to 20 "
        "words, never mention or address anyone by name, no greeting, no "
        "hashtags, no emojis, no quotation marks, no links, be specific to the "
        "subject of the post, never salesy. Return ONLY the comment text.\n\n"
        f"Post:\n\"\"\"\n{post_text[:1200]}\n\"\"\"\n\nComment:"
    )
    for attempt in range(COMMENT_MAX_ATTEMPTS):
        raw = _ollama_generate(prompt, num_predict=48, temperature=0.6 + 0.1 * attempt)
        if raw is None:
            break
        clean = _sanitize_comment(raw, author)
        if clean:
            return clean
        log(f"  ⚠️  comment attempt {attempt + 1}/{COMMENT_MAX_ATTEMPTS} rejected: {raw[:80]!r}")
    return _fallback_comment()

def ollama_available():
    """Quick health check so we can warn early rather than fall back 10x."""
    try:
        with urllib.request.urlopen(f"{OLLAMA_URL}/api/tags", timeout=5) as resp:
            models = [m.get("name", "") for m in json.loads(resp.read()).get("models", [])]
        return True, models
    except Exception as e:
        return False, str(e)

def _location_rule(location="", country=""):
    """Prompt sentence on place: your home city (HOME_CITY) for people in India, the
    Netherlands only for people located there, nothing about location otherwise."""
    where = f"{location or ''} {country or ''}"
    if re.search(r"\bindia\b", where, re.I) or _INDIA_CITY_RE.search(where):
        city = f"you may mention that you are based in {HOME_CITY}. " if HOME_CITY else ""
        return f"They are based in India; {city}Never mention the Netherlands or anything Dutch."
    if re.search(r"\bnetherlands\b", where, re.I):
        return "They are based in the Netherlands; a light mention of the tech scene there is fine."
    return "Do not mention any country, city or relocation, and never anything Dutch."

def generate_message(context, name="", location="", country=""):
    """Generate a short, warm intro DM to a new connection via local Ollama.
    Falls back to a canned message if Ollama is unreachable or returns junk."""
    first = (name or "there").split()[0] if name else "there"
    ctx = (context or "").strip()[:400]
    prompt = (
        "Write a short, warm, professional LinkedIn direct message to a new "
        "1st-degree connection, as a peer software engineer. "
        f"{_location_rule(location, country)} STRICT rules: 1-2 "
        "sentences, under 35 words, address them by first name, no hashtags, no "
        "emojis, no quotation marks, sound like a real person, not salesy, end "
        "with a light friendly question about their work or team. "
        "Return ONLY the message text.\n\n"
        f"Their name: {first}\n"
        f"Their headline/context: {ctx or 'a software professional'}\n\nMessage:"
    )
    raw = _ollama_generate(prompt, num_predict=80)
    if raw is not None:
        text = _strip_decoration(raw, single_line=False)
        if 5 <= len(text) <= 400:
            return text
        log(f"  ⚠️  Ollama returned unusable message ({len(text)} chars) — using fallback")
    return random.choice(FALLBACK_MESSAGES).format(name=first)

def generate_reply(their_message, name="", location="", country=""):
    """Generate a short, natural reply to what a connection just said, via local
    Ollama. Falls back to a safe canned reply if Ollama is unreachable/junk."""
    first = (name or "there").split()[0] if name else "there"
    their = (their_message or "").strip()[:500]
    prompt = (
        "You are a friendly software engineer chatting on LinkedIn with a new "
        f"connection. {_location_rule(location, country)} "
        "Write a SHORT, natural reply to their last message. STRICT rules: 1-2 "
        "sentences, under 40 words, warm and human, no hashtags, no emojis, no "
        "quotation marks, not salesy, keep the conversation going with a light "
        "question when it fits. Return ONLY the reply text.\n\n"
        f"Their name: {first}\n"
        f"Their last message: {their}\n\nReply:"
    )
    raw = _ollama_generate(prompt, num_predict=90)
    if raw is not None:
        text = _strip_decoration(raw, single_line=False)
        if 3 <= len(text) <= 400:
            return text
        log(f"  ⚠️  Ollama returned unusable reply ({len(text)} chars) — using fallback")
    return f"Thanks for the reply, {first}! Appreciate you taking the time — how are things going on your side?"

# ─── FEED ENGAGEMENT ────────────────────────────────────────────────────────

def _dedup_seed(author, text):
    """Normalised (author|text) seed for the post-id fallback hash.

    Normalising is what makes the fallback safe to dedup on: the same post
    hashes differently before and after "…see more" expands, and a different
    hash is a second comment on a post we already commented on. Returns "" when
    the post is too thin to identify reliably — the caller then skips it rather
    than risk a bad id."""
    text = re.sub(r"(?:…|\.\.\.)?\s*see more\s*$|(?:…|\.\.\.)\s*more\s*$", " ", (text or ""), flags=re.I)
    text = re.sub(r"[^a-z0-9 ]+", " ", text.lower())
    text = re.sub(r"\s+", " ", text).strip()
    author = re.sub(r"\s+", " ", (author or "").lower()).strip()
    if not author or len(text) < 40:
        return ""
    return f"{author}|{text[:200]}"

def _post_urn(post_el):
    """Stable per-post id for dedup.

    LinkedIn's SDUI feed carries NO data-urn on posts and hashes all CSS
    classes, but the URN usually still appears somewhere in the post's
    outerHTML (nested attrs / JSON / permalink href). We regex for it; failing
    that we hash a normalised author+text seed. Returns None when neither is
    trustworthy."""
    try:
        html = post_el.get_attribute("outerHTML") or ""
    except Exception:
        html = ""
    for pattern in (r"urn:li:(?:activity|ugcPost|share):(\d+)",
                    r"/feed/update/urn:li:[A-Za-z]+:(\d+)"):
        m = re.search(pattern, html)
        if m:
            return f"urn:li:activity:{m.group(1)}"
    try:
        author, text = _post_author_and_text(post_el)
        seed = _dedup_seed(author, text)
        if seed:
            return "hash:" + hashlib.sha1(seed.encode()).hexdigest()[:16]
    except Exception:
        pass
    return None

_FEED_TRIGGER = "button[aria-label^='Open control menu for post']"

def _find_feed_posts(driver):
    """Locate feed post containers.

    SDUI feed (Sep 2026): no data-urn, hashed classes, and the action-bar
    Comment control carries no aria-label, so the old "ancestor that holds a
    button[aria-label='Comment']" climb failed for every post and the
    ancestor::div[10] fallback resolved all of them to the one shared feed
    container ("Found 1 feed posts"). Each post has exactly one
    'Open control menu for post by …' button; the card is the highest ancestor
    that still holds only that trigger (its parent is the role=list feed or
    holds several)."""
    try:
        cards = driver.execute_script(r"""
            var TRIG = arguments[0], out = [];
            document.querySelectorAll(TRIG).forEach(function (t) {
                var node = t;
                while (node.parentElement && node.parentElement !== document.body
                       && node.parentElement.getAttribute('role') !== 'list'
                       && node.parentElement.querySelectorAll(TRIG).length <= 1) {
                    node = node.parentElement;
                }
                if (out.indexOf(node) === -1) out.push(node);
            });
            return out;
        """, _FEED_TRIGGER) or []
        if cards:
            return cards
    except Exception:
        pass

    # Legacy fallback (older feed DOM).
    for sel in ["[data-view-name='feed-full-update']", "div[data-id^='urn:li:activity']",
                "div.feed-shared-update-v2", "div[role='article']"]:
        try:
            found = driver.find_elements(By.CSS_SELECTOR, sel)
            if found:
                return found
        except Exception:
            continue
    return []

def _scroll_feed(driver, pixels=900):
    """Scroll whatever actually scrolls the feed. The SDUI feed lives in an
    inner overflow container and the document itself is viewport-height, so
    window.scrollBy is a silent no-op there and no further posts ever load.
    Returns which strategy moved."""
    try:
        return driver.execute_script(r"""
            var TRIG = arguments[0], px = arguments[1];
            var trig = document.querySelectorAll(TRIG);
            var last = trig.length ? trig[trig.length - 1] : null;
            var node = last, sc = null;
            while (node && node !== document.body) {
                var cs = getComputedStyle(node);
                if (/(auto|scroll)/.test(cs.overflowY) && node.scrollHeight > node.clientHeight + 50) { sc = node; break; }
                node = node.parentElement;
            }
            // Everything rendered has already been handled: land just past the
            // last post so the lazy loader's sentinel comes into view at once.
            if (last) { try { last.scrollIntoView({block: 'end', behavior: 'instant'}); } catch (e) { last.scrollIntoView(false); } }
            if (sc) { var t0 = sc.scrollTop; sc.scrollBy(0, px); if (sc.scrollTop !== t0 || last) return 'container'; }
            var y0 = window.scrollY; window.scrollBy(0, px);
            if (window.scrollY !== y0) return 'window';
            return last ? 'scrollIntoView' : 'none';
        """, _FEED_TRIGGER, pixels)
    except Exception:
        scroll_down(driver, pixels)
        return "fallback"

def _post_author_and_text(post_el):
    author, text = "", ""
    # SDUI: author name is in the control-menu aria-label.
    try:
        t = post_el.find_element(
            By.CSS_SELECTOR, "button[aria-label^='Open control menu for post']")
        lbl = t.get_attribute("aria-label") or ""
        m = re.search(r"post by (.+?)\s*$", lbl)
        if m:
            author = m.group(1).strip()
    except Exception:
        pass
    if not author:
        for sel in ["span.update-components-actor__title span[aria-hidden='true']",
                    "span.update-components-actor__name"]:
            try:
                author = post_el.find_element(By.CSS_SELECTOR, sel).text.strip().split("\n")[0]
                if author:
                    break
            except Exception:
                continue
    # Post text: SDUI uses data-testid='expandable-text-box'; legacy classes too.
    for sel in ["[data-testid='expandable-text-box']", "[data-view-name='feed-commentary']",
                ".update-components-text", ".feed-shared-update-v2__description",
                ".update-components-update-v2__commentary"]:
        try:
            text = post_el.find_element(By.CSS_SELECTOR, sel).text.strip()
            if text:
                break
        except Exception:
            continue
    return author, text

def _click_like(driver, post_el):
    """Like a post on the SDUI feed. Returns True only if VERIFIED (the button
    stops reading 'no reaction').

    Key fix (per research): the reaction control is HOVER-activated. A click
    without first hovering (mouseenter) silently no-ops. So we: hover the
    button with ActionChains, dwell briefly, then click. The button's
    aria-label is a PREFIX 'Reaction button state: ...' so we prefix-match."""
    from selenium.webdriver.common.action_chains import ActionChains

    def _find_unliked():
        btns = []
        for sel in ["button[aria-label^='Reaction button state']",
                    "button[aria-label*='React Like']", "button.react-button__trigger",
                    "button[aria-label='Like']", "button[aria-label^='React']"]:
            try:
                for b in post_el.find_elements(By.CSS_SELECTOR, sel):
                    if not b.is_displayed():
                        continue
                    lbl = (b.get_attribute("aria-label") or "").lower()
                    if "follow" in lbl:
                        continue
                    # already-liked signals
                    if (b.get_attribute("aria-pressed") or "").lower() == "true" \
                            or "unlike" in lbl \
                            or ("reaction button state" in lbl and "no reaction" not in lbl):
                        continue
                    btns.append(b)
            except Exception:
                continue
        return btns

    targets = _find_unliked()
    if not targets:
        return False
    btn = targets[0]

    # HOVER first (this is what activates the SDUI reaction control), then click.
    try:
        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", btn)
        ActionChains(driver).move_to_element(btn).pause(random.uniform(0.6, 1.2)).perform()
    except Exception:
        pass
    # Click via ActionChains (real mouse at the hovered element) + JS fallback.
    try:
        ActionChains(driver).move_to_element(btn).pause(0.2).click().perform()
    except Exception:
        pass
    human_delay(0.8, 1.5)

    # Verify; if not registered yet, try the synthetic pointer sequence + JS click.
    def _liked_now():
        try:
            lbl = (btn.get_attribute("aria-label") or "").lower()
            if (btn.get_attribute("aria-pressed") or "").lower() == "true" \
                    or "unlike" in lbl \
                    or ("reaction button state" in lbl and "no reaction" not in lbl):
                return True
        except Exception:
            return not _find_unliked()
        return False

    if _liked_now():
        return True
    robust_click(driver, btn)
    human_delay(0.8, 1.5)
    if _liked_now():
        return True
    return False

def _find_comment_editor(scope_el, driver, allow_document=False):
    """Return the comment editor that belongs to `scope_el`, else None.

    Strictly scoped by default. A document-wide search returns the LAST editor
    on the page, which after the first comment of a run belongs to a different
    post — that is how a comment lands under the wrong post. allow_document is
    for diagnostics only."""
    editor_selectors = [".comments-comment-box-comment__text-editor .ql-editor",
                        ".comments-comment-texteditor .ql-editor",
                        "[aria-label='Text editor for creating comment']",
                        "[aria-label='Text editor for commenting']",
                        "div.ProseMirror[contenteditable='true']",
                        "div.tiptap[contenteditable='true']",
                        "div[role='textbox'][contenteditable='true']",
                        "div.ql-editor[contenteditable='true']",
                        "[aria-label^='Add a comment']",
                        "[data-placeholder*='Add a comment']",
                        "div.comments-comment-box__editor div[contenteditable='true']"]
    scopes = [scope_el] + ([driver] if allow_document else [])
    for scope in scopes:
        for sel in editor_selectors:
            try:
                els = [e for e in scope.find_elements(By.CSS_SELECTOR, sel) if e.is_displayed()]
            except Exception:
                continue
            if els:
                return els[-1]  # newly-opened box is usually last
    return None

def _scoped_comment_editor(driver, post_el):
    """The comment editor for THIS post.

    Searches the post, then widens up the ancestor chain — but only through
    ancestors that still contain exactly one post (one control-menu trigger),
    so a neighbouring post's editor can never be picked up."""
    editor = _find_comment_editor(post_el, driver)
    if editor is not None:
        return editor
    node = post_el
    for _ in range(3):
        try:
            parent = driver.execute_script("return arguments[0].parentElement;", node)
            if parent is None:
                return None
            n_posts = driver.execute_script(
                "return arguments[0].querySelectorAll("
                "\"button[aria-label^='Open control menu for post']\").length;", parent)
        except Exception:
            return None
        if n_posts and n_posts > 1:
            return None
        editor = _find_comment_editor(parent, driver)
        if editor is not None:
            return editor
        node = parent
    return None

def _wait_until(predicate, timeout=8.0, interval=0.5):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if predicate():
                return True
        except Exception:
            pass
        time.sleep(interval)
    return False

def _comment_landed(driver, post_el, editor, text):
    """True when `text` is rendered in the post but no longer in the editor —
    ground truth that a submit registered, independent of editor state."""
    probe = re.sub(r"\s+", " ", text or "").strip()[:60].lower()
    if len(probe) < 12:
        return False
    try:
        return bool(driver.execute_script(r"""
            var root=arguments[0], ed=arguments[1], probe=arguments[2];
            var t=(root.innerText||'').replace(/\s+/g,' ').toLowerCase();
            var e='';
            try { e=((ed&&ed.innerText)||'').replace(/\s+/g,' ').toLowerCase(); } catch(err){}
            if (e && e.indexOf(probe)!==-1) return false;
            return t.indexOf(probe)!==-1;
        """, post_el, editor, probe))
    except Exception:
        return False

def _already_commented_by_me(driver, post_el, my_name):
    """True if this post's rendered comment list already carries a comment from
    me. A best-effort second line of defence behind the DB: LinkedIn's own DOM
    can't be reset, copied to another box, or hold a stale hash id."""
    if not my_name:
        return False
    try:
        return bool(driver.execute_script(r"""
            var root=arguments[0], me=(arguments[1]||'').replace(/\s+/g,' ').trim().toLowerCase();
            if(!me) return false;
            var opts=root.querySelectorAll("button[aria-label^='View more options for ']");
            for(var k=0;k<opts.length;k++){
                var l=(opts[k].getAttribute('aria-label')||'').replace(/\s+/g,' ').trim().toLowerCase();
                var mm=l.match(/^view more options for (.+?)[\u2019']s comment/);
                if(mm && (mm[1].indexOf(me)!==-1 || me.indexOf(mm[1])!==-1)) return true;
            }
            var items=root.querySelectorAll(
                "article.comments-comment-entity, .comments-comment-item, "
              + "[class*='comments-comment-entity'], [class*='comments-comment-item']");
            for(var i=0;i<items.length;i++){
                var n=items[i].querySelector(
                    "[class*='commenter__name'], [class*='comment-item__name'], "
                  + ".comments-post-meta__name-text, a[href*='/in/']");
                var t=((n&&n.innerText)||'').replace(/\s+/g,' ').trim().toLowerCase();
                if(t && (t.indexOf(me)!==-1 || me.indexOf(t)!==-1)) return true;
            }
            return false;
        """, post_el, my_name))
    except Exception:
        return False

def _find_comment_toggle(driver, post_el):
    """The action-bar Comment control of THIS post. On the SDUI feed it has no
    aria-label and no stable class, only its visible text."""
    try:
        return driver.execute_script(r"""
            var root = arguments[0], btns = root.querySelectorAll('button');
            for (var i = 0; i < btns.length; i++) {
                var b = btns[i], t = (b.innerText || '').trim(), al = b.getAttribute('aria-label') || '';
                var r = b.getBoundingClientRect();
                if (r.width === 0 || r.height === 0) continue;
                if (t === 'Comment' || /^comment$/i.test(al) || /^comment on /i.test(al)) return b;
            }
            return null;
        """, post_el)
    except Exception:
        return None

def _post_comment(driver, post_el, comment_text):
    """Open THIS post's comment box, type the comment, and submit exactly once.
    Returns True only when the post verifiably registered.

    Two properties matter more than success rate here:
      - the editor is always scoped to `post_el`, so we cannot type into a
        neighbouring post's box;
      - each submit strategy is confirmed with a polled wait before the next is
        tried, so a slow-but-successful click is never re-clicked into a
        duplicate comment.
    """
    from selenium.webdriver.common.action_chains import ActionChains

    editor = _scoped_comment_editor(driver, post_el)
    if editor is None:
        cands = []
        toggle = _find_comment_toggle(driver, post_el)
        if toggle is not None:
            cands.append(toggle)
        for sel in ["button[aria-label='Comment']",
                    "button[id^='feed-shared-social-action-bar-comment-']",
                    "button.comment-button",
                    "button[aria-label*='Comment']"]:
            try:
                cands += [b for b in post_el.find_elements(By.CSS_SELECTOR, sel)
                          if b.is_displayed()
                          and "add a comment" not in (b.get_attribute("aria-label") or "").lower()]
            except Exception:
                continue
        for btn in cands:
            try:
                driver.execute_script("arguments[0].scrollIntoView({block:'center'});", btn)
                ActionChains(driver).move_to_element(btn).pause(random.uniform(0.5, 1.0)).perform()
                ActionChains(driver).move_to_element(btn).pause(0.2).click().perform()
            except Exception:
                pass
            # The composer mounts after a network fetch: measured 0.6-11.5 s on
            # the SDUI feed. A second click too early toggles it shut again.
            _wait_until(lambda: _scoped_comment_editor(driver, post_el) is not None, timeout=15)
            editor = _scoped_comment_editor(driver, post_el)
            if editor is None:
                robust_click(driver, btn)
                _wait_until(lambda: _scoped_comment_editor(driver, post_el) is not None, timeout=10)
                editor = _scoped_comment_editor(driver, post_el)
            if editor is not None:
                break

    if editor is None:
        log("  ⚠️  Comment composer for this post did not open — skipping comment")
        return False

    make_interactable(driver, editor)
    try:
        ActionChains(driver).move_to_element(editor).pause(0.2).click().perform()
    except Exception:
        robust_click(driver, editor)

    # Leftover text from an aborted attempt would be prepended to ours.
    try:
        if (editor.text or editor.get_attribute("innerText") or "").strip():
            editor.send_keys(Keys.CONTROL, "a")
            editor.send_keys(Keys.DELETE)
            human_delay(0.3, 0.6)
    except Exception:
        pass

    slow_type(driver, editor, comment_text)
    human_delay(1, 2)

    try:
        typed = (editor.text or editor.get_attribute("innerText") or "").strip()
        if not typed:
            log("  ⚠️  Comment text didn't register in editor — skipping submit")
            return False
    except Exception:
        pass

    human_delay(0.8, 1.5)

    def _editor_empty():
        try:
            return (editor.text or editor.get_attribute("innerText") or "").strip() == ""
        except Exception:
            return True  # editor torn down = posted

    def _posted():
        return _editor_empty() or _comment_landed(driver, post_el, editor, comment_text)

    def _find_submit():
        # 1) Legacy class-based selectors (older DOM).
        for scope in (post_el, driver):
            for sel in ["button.comments-comment-box__submit-button--cr",
                        "button.comments-comment-box__submit-button",
                        "button[class*='comments-comment-box__submit']",
                        "button[aria-label='Post comment']", "button[aria-label='Post']"]:
                try:
                    for b in scope.find_elements(By.CSS_SELECTOR, sel):
                        if b.is_displayed() and b.is_enabled():
                            return b
                except Exception:
                    continue
        # 2) SDUI DOM: the submit button has NO aria-label/stable class — its
        #    only identifier is its visible text ("Comment"/"Post"), several
        #    levels UP from the editor in a sibling branch. The empty
        #    aria-label check keeps us off the action-bar toggle.
        try:
            btn = driver.execute_script(r"""
                var ed = arguments[0];
                function pick(root){
                    var cands = Array.from(root.querySelectorAll('button'));
                    for (var i=0;i<cands.length;i++){
                        var b = cands[i];
                        var t = (b.innerText||'').trim();
                        var al = b.getAttribute('aria-label')||'';
                        var r = b.getBoundingClientRect();
                        if ((t==='Comment'||t==='Post'||t==='Reply') && !al &&
                            !b.disabled && b.getAttribute('aria-disabled')!=='true' &&
                            r.width>0 && r.height>0){
                            return b;
                        }
                    }
                    return null;
                }
                var node = ed, depth = 0;
                while (node && depth < 10){
                    var b = pick(node);
                    if (b) return b;
                    node = node.parentElement; depth++;
                }
                return null;
            """, editor)
            if btn is not None:
                return btn
        except Exception:
            pass
        return None

    # Each strategy gets a polled confirmation window before the next one runs.
    # Without it a slow-but-successful click is re-clicked into a duplicate.
    btn = _find_submit()
    if btn is not None:
        try:
            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", btn)
            ActionChains(driver).move_to_element(btn).pause(random.uniform(0.4, 0.8)).click().perform()
        except Exception:
            pass
        if _wait_until(_posted, timeout=8):
            _confirm_delay(driver)
            return True

    btn = _find_submit()
    if btn is not None:
        robust_click(driver, btn)
        if _wait_until(_posted, timeout=8):
            _confirm_delay(driver)
            return True

    try:
        editor.send_keys(Keys.CONTROL, Keys.RETURN)
        if _wait_until(_posted, timeout=8):
            _confirm_delay(driver)
            return True
    except Exception:
        pass

    # Leave nothing typed behind — a stale draft is what the next run would
    # find in the box and submit as a comment on the wrong post.
    try:
        editor.send_keys(Keys.CONTROL, "a")
        editor.send_keys(Keys.DELETE)
    except Exception:
        pass
    log("  ⚠️  Submit didn't register (editor still has text) — comment NOT posted")
    return False

def _confirm_delay(driver, seconds=5):
    """Pause after posting so you can visually confirm the comment landed in
    the open browser window."""
    log(f"  ⏸  Holding {seconds}s so you can confirm the action in the browser...")
    time.sleep(seconds)

def inspect_like(driver, conn):
    """Deep diagnostic: find the first feed post's like button and report
    EVERYTHING about it — so we can see why clicks don't register instead of
    guessing. Tries each click strategy and reports the before/after state."""
    log("\n🔬 LIKE BUTTON INSPECTOR")
    driver.get("https://www.linkedin.com/feed/")
    human_delay(4, 6)
    posts = []
    for _ in range(5):
        posts = _find_feed_posts(driver)
        if posts:
            break
        _scroll_feed(driver); human_delay(1, 2)
    if not posts:
        log("  no posts found"); return
    post = posts[0]
    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", post)
    human_delay(1, 2)

    # Locate the like button.
    btn = None
    for sel in ["button[aria-label='Reaction button state: no reaction']",
                "button[aria-label*='React Like']", "button[aria-label*='Like']"]:
        els = post.find_elements(By.CSS_SELECTOR, sel)
        if els:
            btn = els[0]; break
    if not btn:
        log("  ⚠️  no like button found in first post"); return

    # Report the button + what sits at its click point (overlay detection).
    info = driver.execute_script("""
        var b = arguments[0], r = b.getBoundingClientRect();
        var cx = r.left+r.width/2, cy = r.top+r.height/2;
        var top = document.elementFromPoint(cx, cy);
        function desc(e){ if(!e) return null; return {
            tag:e.tagName, cls:(e.className||'').toString().slice(0,80),
            aria:e.getAttribute&&e.getAttribute('aria-label'),
            role:e.getAttribute&&e.getAttribute('role') }; }
        return {
            rect:{x:Math.round(r.left),y:Math.round(r.top),w:Math.round(r.width),h:Math.round(r.height)},
            visible: r.width>0 && r.height>0,
            btn: desc(b),
            elementAtCenter: desc(top),
            centerIsButtonOrChild: (top===b || b.contains(top)),
            disabled: b.disabled, ariaPressed: b.getAttribute('aria-pressed')
        };
    """, btn)
    log(f"  button rect: {info['rect']} visible={info['visible']} disabled={info['disabled']}")
    log(f"  button: {info['btn']}")
    log(f"  element at click-center: {info['elementAtCenter']}")
    log(f"  center hits button (not an overlay)? {info['centerIsButtonOrChild']}")
    label_before = btn.get_attribute("aria-label")
    log(f"  aria-label BEFORE: {label_before!r}")

    # Strategy A: native selenium click.
    try:
        btn.click(); log("  [A] native .click() dispatched")
    except Exception as e:
        log(f"  [A] native click raised: {e}")
    human_delay(1.5, 2.5)
    log(f"      aria-label after A: {btn.get_attribute('aria-label')!r}")

    # Strategy B: ActionChains move + click (real mouse at real coords).
    try:
        from selenium.webdriver.common.action_chains import ActionChains
        ActionChains(driver).move_to_element(btn).pause(0.3).click().perform()
        log("  [B] ActionChains move+click dispatched")
    except Exception as e:
        log(f"  [B] ActionChains raised: {e}")
    human_delay(1.5, 2.5)
    log(f"      aria-label after B: {btn.get_attribute('aria-label')!r}")

    # Strategy C: full synthetic pointer sequence.
    robust_click(driver, btn)
    log("  [C] synthetic pointer sequence dispatched")
    human_delay(1.5, 2.5)
    log(f"      aria-label after C: {btn.get_attribute('aria-label')!r}")
    log("  → If any strategy flipped the label to a reacted state, that's the winner.")

def inspect_comment(driver, conn):
    """Deep diagnostic: open the first post's comment composer, type test text,
    then report EVERYTHING about the submit button — so we can see why the
    Post click doesn't register instead of guessing."""
    from selenium.webdriver.common.action_chains import ActionChains
    log("\n🔬 COMMENT SUBMIT INSPECTOR")
    driver.get("https://www.linkedin.com/feed/")
    human_delay(4, 6)
    posts = []
    for _ in range(5):
        posts = _find_feed_posts(driver)
        if posts:
            break
        _scroll_feed(driver); human_delay(1, 2)
    if not posts:
        log("  no posts found"); return

    # Try posts until a composer actually opens (opening is intermittent).
    editor = None
    opened_post = None
    for pi, post in enumerate(posts[:5]):
        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", post)
        human_delay(1, 2)
        for sel in ["button[aria-label='Comment']", "button.comment-button",
                    "button[aria-label*='Comment']"]:
            btns = [b for b in post.find_elements(By.CSS_SELECTOR, sel)
                    if b.is_displayed() and "add a comment" not in (b.get_attribute("aria-label") or "").lower()]
            if btns:
                try:
                    ActionChains(driver).move_to_element(btns[0]).pause(0.6).click().perform()
                except Exception as e:
                    log(f"  comment-open click raised: {e}")
                break
        human_delay(2, 3)
        editor = _scoped_comment_editor(driver, post)
        if editor:
            opened_post = post
            log(f"  ✅ composer opened on post {pi+1}")
            break
        log(f"  post {pi+1}: composer didn't open — trying next")
    if not editor:
        log("  ⚠️  no composer opened on any post — can't inspect submit"); return

    # Generate a REAL comment (so if it submits during the test, it's a proper
    # comment, not a placeholder).
    author, text = _post_author_and_text(opened_post)
    comment = generate_comment(text, author)
    if not comment:
        log("  ⚠️  no safe comment could be generated for this post — nothing to inspect")
        return
    log(f"  will type comment: {comment!r}")
    try:
        ActionChains(driver).move_to_element(editor).pause(0.2).click().perform()
    except Exception:
        pass
    slow_type(driver, editor, comment)
    human_delay(1, 2)
    typed = (editor.text or editor.get_attribute("innerText") or "").strip()
    log(f"  editor text after typing: {typed!r}")

    # Find the submit button by walking UP from the editor level by level,
    # reporting at which ancestor depth a 'Comment'/'Post' submit button
    # appears — the editor and submit are often in sibling branches.
    report = driver.execute_script("""
        var ed = arguments[0];
        function scan(root){
            var found=[];
            Array.from(root.querySelectorAll('button')).forEach(function(b){
                var t=(b.innerText||'').trim();
                if (t==='Comment'||t==='Post'||t==='Reply'){
                    var r=b.getBoundingClientRect();
                    var top=(r.width>0&&r.height>0)?document.elementFromPoint(r.left+r.width/2,r.top+r.height/2):null;
                    found.push({text:t, aria:b.getAttribute('aria-label'),
                        disabled:b.disabled, ariaDisabled:b.getAttribute('aria-disabled'),
                        visible:(r.width>0&&r.height>0),
                        rect:{x:Math.round(r.left),y:Math.round(r.top),w:Math.round(r.width),h:Math.round(r.height)},
                        centerIsButton:(top===b||(b.contains&&b.contains(top))),
                        atCenter: top?{tag:top.tagName, cls:(top.className||'').toString().slice(0,50)}:null});
                }
            });
            return found;
        }
        var node = ed, depth=0, result=[];
        while (node && depth<12){
            var f = scan(node);
            result.push({depth:depth, tag:node.tagName, cls:(node.className||'').toString().slice(0,40),
                         btnCount: node.querySelectorAll('button').length, submitMatches:f});
            if (f.length) break;   // stop at the first ancestor that contains a submit
            node = node.parentElement; depth++;
        }
        return result;
    """, editor)
    for lvl in report:
        log(f"  depth {lvl['depth']} <{lvl['tag']} .{lvl['cls']}> buttons={lvl['btnCount']} "
            f"submitMatches={len(lvl['submitMatches'])}")
        for b in lvl['submitMatches']:
            log(f"      → text={b['text']!r} aria={b['aria']!r} disabled={b['disabled']} "
                f"ariaDisabled={b['ariaDisabled']} vis={b['visible']} centerIsButton={b['centerIsButton']}")
            log(f"        rect={b['rect']} atCenter={b['atCenter']}")
    log("  → Note the depth where the submit appears; that's how far up to scope the finder.")

    # Now actually TRY each click method on the found button and report which
    # (if any) empties the editor — the definitive test.
    def _empty():
        try:
            return (editor.text or editor.get_attribute("innerText") or "").strip() == ""
        except Exception:
            return True
    def _find():
        return driver.execute_script("""
            var ed=arguments[0], node=ed, depth=0;
            function pick(root){var c=Array.from(root.querySelectorAll('button'));
              for(var i=0;i<c.length;i++){var b=c[i],t=(b.innerText||'').trim(),al=b.getAttribute('aria-label')||'',r=b.getBoundingClientRect();
                if((t==='Comment'||t==='Post'||t==='Reply')&&!al&&!b.disabled&&r.width>0)return b;}return null;}
            while(node&&depth<10){var b=pick(node); if(b)return b; node=node.parentElement; depth++;} return null;
        """, editor)
    from selenium.webdriver.common.action_chains import ActionChains
    b = _find()
    if not b:
        log("  ⚠️  finder returned nothing (won't happen if depth report found it)"); return
    log("  [1] ActionChains hover+click...")
    try:
        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", b)
        ActionChains(driver).move_to_element(b).pause(0.5).click().perform()
    except Exception as e:
        log(f"      raised: {e}")
    human_delay(2, 3); log(f"      editor empty now? {_empty()}")
    if _empty(): log("  ✅ [1] WORKED"); return
    b = _find() or b
    log("  [2] JS .click() on button...")
    try: driver.execute_script("arguments[0].click();", b)
    except Exception as e: log(f"      raised: {e}")
    human_delay(2, 3); log(f"      editor empty now? {_empty()}")
    if _empty(): log("  ✅ [2] WORKED"); return
    log("  [3] Ctrl+Enter in editor...")
    try: editor.send_keys(Keys.CONTROL, Keys.RETURN)
    except Exception as e: log(f"      raised: {e}")
    human_delay(2, 3); log(f"      editor empty now? {_empty()}")
    if _empty(): log("  ✅ [3] Ctrl+Enter WORKED"); return
    log("  ❌ none of [1][2][3] emptied the editor — comment truly not submitting")

# ─── MESSAGING ──────────────────────────────────────────────────────────────
def _find_message_editor(driver):
    """Find the messaging composer contenteditable. Layered selectors — some
    are initial guesses to be corrected by --inspect-message on the live DOM."""
    for sel in ["div.msg-form__contenteditable[contenteditable='true']",
                "[aria-label='Write a message']",
                "[aria-label^='Write a message']",
                "div.msg-form__contenteditable",
                "div[role='textbox'][contenteditable='true']",
                "div.ProseMirror[contenteditable='true']",
                "div.ql-editor[contenteditable='true']"]:
        try:
            els = [e for e in driver.find_elements(By.CSS_SELECTOR, sel) if e.is_displayed()]
            if els:
                return els[-1]
        except Exception:
            continue
    return None

def _click_message_trigger(driver):
    """Open the message composer for the PROFILE OWNER (not a sidebar person).

    On the profile the Message control is an <a href='/messaging/compose/?...
    &screenContext=NON_SELF_PROFILE_VIEW...&interop=msgOverlay'>. The sidebar
    'People you may know' cards also render 'Message <name>' links — we must
    NOT click those. We pick the owner's link by its screenContext and click
    it (hover+click); it opens the message overlay for the right person."""
    from selenium.webdriver.common.action_chains import ActionChains
    owner = None
    try:
        links = driver.find_elements(By.CSS_SELECTOR, "a[href*='/messaging/compose/']")
        # Prefer the profile-owner link (NON_SELF_PROFILE_VIEW), visible, text 'Message'.
        for a in links:
            href = a.get_attribute("href") or ""
            if not a.is_displayed():
                continue
            if "NON_SELF_PROFILE_VIEW" in href and (a.text or "").strip() == "Message":
                owner = a
                break
        # Fallback: first visible compose link whose visible text is exactly 'Message'.
        if not owner:
            for a in links:
                if a.is_displayed() and (a.text or "").strip() == "Message":
                    owner = a
                    break
    except Exception:
        pass

    if owner is not None:
        try:
            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", owner)
            ActionChains(driver).move_to_element(owner).pause(random.uniform(0.6, 1.0)).click().perform()
        except Exception:
            robust_click(driver, owner)
        return True

    # Last resort: a real <button> whose aria-label is EXACTLY 'Message' (owner
    # action bar in some layouts) — never the sidebar 'Message <name>' ones.
    try:
        for b in driver.find_elements(By.CSS_SELECTOR, "button[aria-label='Message']"):
            if b.is_displayed():
                try:
                    ActionChains(driver).move_to_element(b).pause(0.6).click().perform()
                except Exception:
                    robust_click(driver, b)
                return True
    except Exception:
        pass
    return False

def _owner_recipient_id(driver):
    """Identify the PROFILE OWNER's messaging recipient id.

    The profile page references the owner's fsd_profile URN far more than any
    sidebar 'People you may know' person, and tags it near the Topcard. We pick
    the owner by (a) a URN adjacent to a 'Topcard' marker, else (b) the most
    frequently-referenced ACoAA... id in the page source. This avoids messaging
    a sidebar person by mistake."""
    try:
        html = driver.page_source
    except Exception:
        return None
    # (a) Topcard-tagged URN.
    m = re.search(r'(ACoAA[A-Za-z0-9_-]{10,}?)Topcard', html)
    if m:
        return m.group(1)
    # (b) Most frequent id (strip any trailing word-chars beyond the base id).
    ids = re.findall(r'ACoAA[A-Za-z0-9_-]{10,}', html)
    if not ids:
        return None
    counts = collections.Counter(i[:40] for i in ids)  # 40 = typical id length
    return counts.most_common(1)[0][0]

def open_message_composer(driver, profile_url):
    """Navigate to a profile, resolve the owner's recipient id, and open the
    compose overlay by navigating directly to the compose URL (deterministic —
    no fragile button click, and can't hit a sidebar person). Returns the
    editor element or None."""
    if not _get_with_retry(driver, profile_url):
        return None
    human_delay(2, 4)
    scroll_down(driver, 200)

    editor = _find_message_editor(driver)
    if editor:
        return editor

    recipient = _owner_recipient_id(driver)
    if recipient:
        compose_url = (f"https://www.linkedin.com/messaging/compose/"
                       f"?recipient={recipient}&interop=msgOverlay")
        log_debug(f"opening compose overlay for recipient={recipient}")
        if _get_with_retry(driver, compose_url):
            human_delay(2, 4)
            editor = _find_message_editor(driver)
            if editor:
                return editor
    # Fallback: click the owner's Message link/button.
    if _click_message_trigger(driver):
        human_delay(2, 3)
        return _find_message_editor(driver)
    log("  ⚠️  Could not resolve a message composer for this profile")
    return None

def _find_send_button(driver, editor):
    """Find the messaging 'Send' button by walking up from the editor to the
    <form> and returning the enabled, visible button whose text is 'Send'."""
    try:
        return driver.execute_script("""
            var ed=arguments[0], node=ed, depth=0;
            function pick(root){
                var c=Array.from(root.querySelectorAll('button'));
                for(var i=0;i<c.length;i++){
                    var b=c[i], t=(b.innerText||'').trim(), r=b.getBoundingClientRect();
                    if(t==='Send' && !b.disabled && b.getAttribute('aria-disabled')!=='true'
                       && r.width>0 && r.height>0) return b;
                }
                return null;
            }
            while(node && depth<8){ var b=pick(node); if(b) return b; node=node.parentElement; depth++; }
            return null;
        """, editor)
    except Exception:
        return None

def send_message(driver, conn, linkedin_id, name, headline, profile_url, dry_run=False):
    """Send an AI-generated DM to one profile. Verifies the send registered
    (editor cleared) before recording it. Returns True on verified send."""
    from selenium.webdriver.common.action_chains import ActionChains
    log(f"→ Messaging {name or linkedin_id} ({profile_url})")
    editor = open_message_composer(driver, profile_url)
    if not editor:
        log(f"  ❌ Could not open composer for {name or linkedin_id}")
        return False

    place = conn.execute("SELECT location, country FROM outreach WHERE linkedin_id=?",
                         (linkedin_id,)).fetchone() or ("", "")
    msg = generate_message(headline, name, *place)
    if dry_run:
        log(f"  [DRY RUN] would send: {msg!r}")
        return True

    # Focus + type.
    try:
        ActionChains(driver).move_to_element(editor).pause(0.2).click().perform()
    except Exception:
        robust_click(driver, editor)
    slow_type(driver, editor, msg)
    human_delay(1, 2)

    typed = (editor.text or editor.get_attribute("innerText") or "").strip()
    if not typed:
        log("  ⚠️  Message text didn't register in composer — aborting send")
        return False

    def _editor_empty():
        try:
            return (editor.text or editor.get_attribute("innerText") or "").strip() == ""
        except Exception:
            return True  # composer torn down = sent

    # Send: hover+click, then synthetic click, then Ctrl/Enter — verify each.
    btn = _find_send_button(driver, editor)
    if btn is not None:
        try:
            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", btn)
            ActionChains(driver).move_to_element(btn).pause(random.uniform(0.4, 0.8)).click().perform()
        except Exception:
            pass
        human_delay(1.5, 2.5)
        if not _editor_empty():
            btn = _find_send_button(driver, editor) or btn
            robust_click(driver, btn)
            human_delay(1.5, 2.5)
    if not _editor_empty():
        # Keyboard fallback — Enter sends in the LinkedIn composer.
        try:
            editor.send_keys(Keys.RETURN)
            human_delay(1.5, 2.5)
        except Exception:
            pass

    if _editor_empty():
        update_status(conn, linkedin_id, "messaged", msg)
        upsert_conversation(conn, linkedin_id, name=name, profile_url=profile_url,
                            last_message_text=msg, last_message_from="me",
                            last_message_at=datetime.now().isoformat())
        log(f"  ✅ Message sent to {name or linkedin_id}: {msg!r}")
        _confirm_delay(driver)
        return True

    log(f"  ❌ Send didn't register (composer still has text) — NOT sent to {name or linkedin_id}")
    return False

# ─── CONNECTION SYNC ────────────────────────────────────────────────────────
def _slug(url_or_id):
    """Normalize a profile URL or id to its /in/<slug> identifier."""
    s = (url_or_id or "").split("?")[0].rstrip("/")
    return s.split("/")[-1] if s else ""

def sync_connections(driver, conn):
    """Scan the user's connections list and flip DB rows that were 'requested'
    to 'connected' when their profile now appears among your connections
    (i.e. the invite was accepted). Sets connected_at."""
    log("\n🔗 SYNC CONNECTIONS — detecting accepted invites")
    url = "https://www.linkedin.com/mynetwork/invite-connect/connections/"
    if not _get_with_retry(driver, url):
        log("  ❌ Could not load connections page"); return
    human_delay(3, 5)

    # Lazy-loaded list — scroll to render more, collecting slugs as we go.
    slugs = set()
    for _ in range(15):
        try:
            for a in driver.find_elements(By.CSS_SELECTOR, "a[href*='/in/']"):
                href = a.get_attribute("href") or ""
                if "/in/" in href:
                    slugs.add(_slug(href))
        except Exception:
            pass
        before = len(slugs)
        scroll_down(driver, 1000)
        human_delay(1, 2)
        # stop if scrolling stops adding new profiles for a round
        if len(slugs) == before and _ > 3:
            break
    log(f"  Found {len(slugs)} connections in your network")
    if not slugs:
        _dump_search_dom(driver, "connections")
        return

    # Flip matching requested rows to connected.
    cur = conn.cursor()
    cur.execute("SELECT linkedin_id, profile_url, name FROM outreach WHERE status='requested'")
    pending = cur.fetchall()
    flipped = 0
    for lid, purl, name in pending:
        if lid in slugs or _slug(purl) in slugs:
            update_status(conn, lid, "connected")
            log(f"  ✓ {name or lid}: request accepted → connected")
            flipped += 1
    log(f"  → {flipped}/{len(pending)} pending requests are now connected")

# ─── MESSAGE / REPLY SYNC ───────────────────────────────────────────────────

def _resolve_identity(driver):
    """Resolve (my_name, my_slug) once per session by visiting /in/me/, which
    LinkedIn redirects to my own profile.

    Deterministic, unlike sniffing the nav avatar's alt text — which is empty
    on plenty of renders, and an empty name is exactly what made every message
    look like it came from the other person. LINKEDIN_MY_NAME / LINKEDIN_MY_SLUG
    override it entirely."""
    cached = getattr(driver, "_li_identity", None)
    if cached is not None:
        return cached

    name = os.environ.get("LINKEDIN_MY_NAME", "").strip()
    slug = os.environ.get("LINKEDIN_MY_SLUG", "").strip()
    if not (name and slug):
        try:
            if _get_with_retry(driver, "https://www.linkedin.com/in/me/"):
                human_delay(2, 3)
                resolved = _slug(driver.current_url)
                if resolved and resolved != "me":
                    slug = slug or resolved
                if not name:
                    for sel in ["h1.text-heading-xlarge", "main h1", "h1"]:
                        try:
                            els = driver.find_elements(By.CSS_SELECTOR, sel)
                        except Exception:
                            continue
                        if els and (els[0].text or "").strip():
                            name = els[0].text.strip().split("\n")[0].strip()
                            break
                if not name:
                    # SDUI profile has no h1; the tab title is "(3) Name - Headline | LinkedIn".
                    title = re.sub(r"^\(\d+\)\s*", "", (driver.title or "").strip())
                    title = re.sub(r"\s*\|\s*LinkedIn\s*$", "", title)
                    cand = re.split(r"\s+[-–]\s+", title, 1)[0].strip()
                    if cand and cand.lower() not in ("linkedin", "feed", "sign in"):
                        name = cand
        except Exception:
            pass
    if not name:
        try:
            for el in driver.find_elements(
                    By.CSS_SELECTOR, "img.global-nav__me-photo, .global-nav__me-photo"):
                alt = re.sub(r"^(?:photo of|profile photo of)\s+", "",
                             (el.get_attribute("alt") or "").strip(), flags=re.I).strip()
                if alt:
                    name = alt
                    break
        except Exception:
            pass

    identity = (name, slug)
    driver._li_identity = identity
    return identity

def _my_name(driver):
    return _resolve_identity(driver)[0]

def _close_message_overlays(driver):
    """Close every open conversation overlay.

    Left open they stack up, and a document-wide read then sees several threads
    mixed together — which is how one conversation's last message ended up
    logged against a different person."""
    try:
        driver.execute_script(r"""
            var sels=["button[aria-label^='Close your conversation']",
                      "button[aria-label^='Close conversation']",
                      ".msg-overlay-bubble-header__control--close-btn",
                      ".msg-overlay-conversation-bubble button[aria-label*='Close']"];
            for(var i=0;i<sels.length;i++){
                var bs=document.querySelectorAll(sels[i]);
                for(var j=0;j<bs.length;j++){ try{bs[j].click();}catch(e){} }
            }
        """)
    except Exception:
        pass

# Signals we trust enough to act on (auto-reply / funnel advance). A direction
# inherited from the previous bubble is fine for display, not for sending.
DECISIVE_DIRECTION_SOURCES = {"slug", "name", "class"}

def _read_thread_last_message(driver, my_name, my_slug, partner_name="", partner_slug=""):
    """Read the OPEN thread and return who said what last.

    Returns a dict: last_text, last_from ('me' | 'them' | 'unknown'), source
    (which signal decided it), theirs, mine, count, partner_match.

    Direction comes from the strongest available signal, in order: the sender's
    /in/ slug, the sender's displayed name, then LinkedIn's own --other bubble
    modifier (used only when the thread demonstrably uses it), then inheritance
    from the previous bubble in the same group. Anything unresolved stays
    'unknown' — it is never silently called 'them'."""
    empty = {"last_text": "", "last_from": "unknown", "source": "none",
             "theirs": 0, "mine": 0, "count": 0, "partner_match": None}
    try:
        result = driver.execute_script(r"""
            var me=(arguments[0]||'').replace(/\s+/g,' ').trim().toLowerCase();
            var meSlug=(arguments[1]||'').toLowerCase();
            var them=(arguments[2]||'').replace(/\s+/g,' ').trim().toLowerCase();
            var themSlug=(arguments[3]||'').toLowerCase();
            function norm(s){return (s||'').replace(/\s+/g,' ').trim().toLowerCase();}
            function slugOf(h){var m=(h||'').match(/\/in\/([^\/?#]+)/);return m?m[1].toLowerCase():'';}
            var ITEMS='.msg-s-event-listitem, li[class*="event-listitem"]';

            // Pick ONE thread container: the active overlay first, else the
            // largest visible message list. Never the whole document.
            var container=null;
            var active=document.querySelector(
                '.msg-overlay-conversation-bubble--is-active, .msg-overlay-conversation-bubble--expanded');
            if(active && active.querySelectorAll(ITEMS).length) container=active;
            if(!container){
                var best=0;
                var cands=document.querySelectorAll(
                    '.msg-s-message-list-content, .msg-s-message-list, .msg-s-message-list-container');
                for(var i=0;i<cands.length;i++){
                    var c=cands[i], r=c.getBoundingClientRect();
                    if(r.width<=0||r.height<=0) continue;
                    if(!c.querySelectorAll(ITEMS).length) continue;
                    var area=r.width*r.height;
                    if(area>best){best=area;container=c;}
                }
            }
            if(!container) return null;

            // Does this thread actually belong to the person we asked for?
            var scope=container.closest(
                '.msg-overlay-conversation-bubble, .msg-convo-wrapper, .scaffold-layout__detail') || container;
            var partnerMatch=null;   // null = couldn't tell; only false blocks a write
            var links=scope.querySelectorAll("a[href*='/in/']"), slugs=[];
            for(var k=0;k<links.length;k++){
                var s=slugOf(links[k].getAttribute('href'));
                if(s) slugs.push(s);
            }
            if(themSlug && slugs.length) partnerMatch = slugs.indexOf(themSlug)!==-1;
            if(partnerMatch!==true && them){
                if(norm(scope.innerText).slice(0,600).indexOf(them)!==-1) partnerMatch=true;
                else if(partnerMatch===null) partnerMatch=false;
            }

            // Only trust the --other polarity in a thread that demonstrably uses it.
            var usesOther=/--other\b/.test(container.innerHTML);
            var items=container.querySelectorAll(ITEMS);
            var out=[], inherited='';
            for(var i=0;i<items.length;i++){
                var it=items[i];
                var bodyEl=it.querySelector(
                    '.msg-s-event-listitem__body, [class*="event-listitem__body"], p');
                // Original case: this text is stored, shown in the sheet, and
                // fed to the reply model. norm() is for comparisons only.
                var body=bodyEl?(bodyEl.innerText||'').replace(/\s+/g,' ').trim():'';
                if(!body) continue;
                var dir='', srcName='';

                var a=it.querySelector("a[href*='/in/']");
                var sslug=a?slugOf(a.getAttribute('href')):'';
                if(sslug){
                    if(meSlug && sslug===meSlug){dir='me';srcName='slug';}
                    else if(themSlug && sslug===themSlug){dir='them';srcName='slug';}
                }
                if(!dir){
                    var nEl=it.querySelector(
                        '.msg-s-message-group__name, [class*="message-group__name"], '
                      + '[class*="event-listitem__name"]');
                    var nm=nEl?norm(nEl.innerText):'';
                    if(!nm){
                        var img=it.querySelector('img[alt]');
                        if(img) nm=norm((img.getAttribute('alt')||'')
                            .replace(/^(photo of|profile photo of)\s+/i,''));
                    }
                    if(nm){
                        if(me && (nm===me || nm.indexOf(me)!==-1 || me.indexOf(nm)!==-1)){
                            dir='me';srcName='name';
                        } else if(them && (nm===them || nm.indexOf(them)!==-1 || them.indexOf(nm)!==-1)){
                            dir='them';srcName='name';
                        }
                    }
                }
                if(!dir && usesOther){
                    dir=/--other\b/.test((it.className||'').toString()) ? 'them' : 'me';
                    srcName='class';
                }
                if(!dir && inherited){dir=inherited;srcName='continuation';}
                if(dir) inherited=dir;
                out.push({text:body, dir:dir||'unknown', src:srcName||'none'});
            }
            if(!out.length) return null;
            var last=out[out.length-1], theirs=0, mine=0;
            for(var i=0;i<out.length;i++){
                if(out[i].dir==='them') theirs++;
                else if(out[i].dir==='me') mine++;
            }
            return {last_text:last.text, last_from:last.dir, source:last.src,
                    theirs:theirs, mine:mine, count:out.length, partner_match:partnerMatch};
        """, my_name or "", my_slug or "", partner_name or "", partner_slug or "")
    except Exception as e:
        log(f"  ⚠️  thread read failed — {e}")
        return dict(empty)
    if not result:
        return dict(empty)
    merged = dict(empty)
    merged.update({k: v for k, v in result.items() if k in merged})
    return merged

def _direction_sanity(conn, linkedin_id, last_text, last_from, source):
    """Overrule a 'them' reading when the last message is verbatim what WE last
    sent. Cheap and factual, and it catches the DOM heuristics failing — which
    is how our own messages were being logged as theirs."""
    if last_from != "them":
        return last_from, source
    probe = re.sub(r"\s+", " ", last_text or "").strip().lower()
    if len(probe) < 12:
        return last_from, source
    cur = conn.cursor()
    ours = []
    cur.execute("SELECT message_sent FROM outreach WHERE linkedin_id=?", (linkedin_id,))
    row = cur.fetchone()
    if row and row[0]:
        ours.append(row[0])
    cur.execute("SELECT last_message_text FROM conversations "
                "WHERE linkedin_id=? AND last_message_from='me'", (linkedin_id,))
    row = cur.fetchone()
    if row and row[0]:
        ours.append(row[0])
    for text in ours:
        mine = re.sub(r"\s+", " ", text).strip().lower()
        if mine and (mine == probe or mine.startswith(probe) or probe.startswith(mine)):
            return "me", f"{source}+own-text-match"
    return last_from, source

def _send_in_open_composer(driver, text):
    """Type `text` into the currently-open message composer and send it, reusing
    the same hover-click → synthetic-click → Enter fallback chain as
    send_message. Returns True only if the send verifiably registered (editor
    cleared). Used for auto-replies where the thread is already open."""
    from selenium.webdriver.common.action_chains import ActionChains
    editor = _find_message_editor(driver)
    if not editor:
        log("  ⚠️  reply: composer not found — skipping send")
        return False
    try:
        ActionChains(driver).move_to_element(editor).pause(0.2).click().perform()
    except Exception:
        robust_click(driver, editor)
    slow_type(driver, editor, text)
    human_delay(1, 2)
    typed = (editor.text or editor.get_attribute("innerText") or "").strip()
    if not typed:
        log("  ⚠️  reply: text didn't register — aborting")
        return False

    def _empty():
        try:
            return (editor.text or editor.get_attribute("innerText") or "").strip() == ""
        except Exception:
            return True

    btn = _find_send_button(driver, editor)
    if btn is not None:
        try:
            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", btn)
            ActionChains(driver).move_to_element(btn).pause(random.uniform(0.4, 0.8)).click().perform()
        except Exception:
            pass
        human_delay(1.5, 2.5)
        if not _empty():
            btn = _find_send_button(driver, editor) or btn
            robust_click(driver, btn)
            human_delay(1.5, 2.5)
    if not _empty():
        try:
            editor.send_keys(Keys.RETURN); human_delay(1.5, 2.5)
        except Exception:
            pass
    return _empty()

def sync_messages(driver, conn, dry_run=False):
    """For each person we've messaged, open their thread, read the last message
    + direction, and update conversations + funnel (replied / developing).

    Writes a direction only when a signal actually resolved it. An unreadable
    thread leaves the stored value alone rather than overwriting it with a
    guess, and a thread that doesn't belong to the person we asked for is
    skipped entirely. A dry run never sends the auto-reply."""
    log("\n💬 SYNC MESSAGES — reading threads for replies")
    my_name, my_slug = _resolve_identity(driver)
    log(f"  (me: name={my_name!r} slug={my_slug!r})")
    if not (my_name or my_slug):
        log("  ⚠️  Could not resolve my own identity — reading threads read-only, no auto-reply")

    cur = conn.cursor()
    cur.execute("SELECT linkedin_id, name, profile_url, location, country FROM outreach "
                "WHERE messaged_at IS NOT NULL ORDER BY messaged_at DESC")
    people = cur.fetchall()
    if not people:
        log("  No messaged people to check"); return
    log(f"  Checking {len(people)} conversations...")

    replied = developing = unresolved = 0
    for lid, name, purl, location, country in people:
        try:
            _close_message_overlays(driver)  # one thread open at a time
            purl = purl or f"https://www.linkedin.com/in/{lid}/"
            partner_slug = _slug(purl) or lid
            editor = open_message_composer(driver, purl)
            if not editor:
                log(f"  {name or lid}: couldn't open thread — skipping")
                continue
            human_delay(1, 2)
            info = _read_thread_last_message(driver, my_name, my_slug, name or "", partner_slug)
            last_text, source = info["last_text"], info["source"]
            if not last_text:
                log(f"  {name or lid}: no messages read")
                continue
            if info["partner_match"] is False:
                log(f"  ⚠️  {name or lid}: open thread doesn't match this profile "
                    f"— skipping without writing")
                continue

            last_from, source = _direction_sanity(conn, lid, last_text, info["last_from"], source)
            now = datetime.now().isoformat()
            if last_from == "unknown":
                unresolved += 1
                log(f"  ⚠️  {name or lid}: couldn't tell who sent the last message "
                    f"— keeping the stored direction")
                upsert_conversation(conn, lid, name=name, profile_url=purl,
                                    last_message_checked_at=now, thread_url=driver.current_url)
                human_delay(2, 4)
                continue

            upsert_conversation(conn, lid, name=name, profile_url=purl,
                                last_message_text=last_text[:500], last_message_from=last_from,
                                last_message_at=now, last_message_checked_at=now,
                                direction_source=source, reply_count=info["theirs"],
                                thread_url=driver.current_url)

            if last_from != "them":
                log(f"  {name or lid}: last message from me ({source}) — awaiting reply")
                human_delay(2, 4)
                continue

            theirs = info["theirs"]
            if theirs >= 2:
                update_status(conn, lid, "developing"); developing += 1
            else:
                update_status(conn, lid, "replied"); replied += 1
            log(f"  {name or lid}: THEM replied ({theirs} msgs, via {source}) → "
                f"{'developing' if theirs >= 2 else 'replied'}")

            # Auto-reply into the already-open composer. Off by default: replies
            # are answered by hand. When enabled it is gated on a DECISIVE
            # direction signal: an inherited/continuation guess is good enough
            # to display in the sheet, not to send a message on.
            if not AUTO_REPLY:
                log(f"  ↳ reply waiting for you (auto-reply off): {last_text[:80]!r}")
            elif not (my_name or my_slug):
                log("  (auto-reply skipped — own identity unresolved)")
            elif source not in DECISIVE_DIRECTION_SOURCES:
                log(f"  (auto-reply skipped — direction only inferred via {source})")
            elif messages_today(conn) >= MESSAGE_DAILY_LIMIT or not within_send_hours():
                log("  (auto-reply skipped — daily msg cap or outside send hours)")
            else:
                reply = generate_reply(last_text, name, location, country)
                log(f"  ↳ auto-reply: {reply!r}")
                if dry_run:
                    log("  [DRY RUN] auto-reply not sent")
                elif _send_in_open_composer(driver, reply):
                    update_status(conn, lid, "messaged", reply)
                    upsert_conversation(conn, lid, last_message_text=reply,
                                        last_message_from="me",
                                        last_message_at=datetime.now().isoformat(),
                                        direction_source="sent-by-us")
                    log(f"  ✅ auto-replied to {name or lid}")
                    human_delay(20, 50)
                else:
                    log(f"  ⚠️  auto-reply to {name or lid} didn't send")
            human_delay(2, 4)
        except Exception as e:
            log(f"  ⚠️  {name or lid}: thread read error — {e}")
    _close_message_overlays(driver)
    log(f"  → {replied} newly replied, {developing} developing, "
        f"{unresolved} unresolved (direction left untouched)")

# ─── BULK MESSAGING ─────────────────────────────────────────────────────────
def send_bulk_messages(driver, conn, dry_run=False, limit=None, force=False):
    """Message connected people who haven't been messaged yet — AI-generated,
    under account-safety pacing. Uses get_pending_messages (connected + not
    messaged)."""
    log("\n✉️  BULK MESSAGING — connected people not yet messaged")
    sent_today = messages_today(conn)
    cap = limit if (limit and limit > 0) else MESSAGE_DAILY_LIMIT
    room = max(0, min(cap, MESSAGE_DAILY_LIMIT) - sent_today)
    log(f"  Messages today: {sent_today}/{MESSAGE_DAILY_LIMIT} | room this run: {room}")

    if not dry_run and not force and not within_send_hours():
        log(f"  🕐 Outside send window {SEND_HOURS[0]}:00–{SEND_HOURS[1]}:00 — "
            f"skipping (use --force to override)")
        return
    if room <= 0 and not dry_run:
        log("  Daily message limit reached — run again tomorrow")
        return

    pending = get_pending_messages(conn)  # (linkedin_id, name, title, company, profile_url)
    if not pending:
        log("  No connected people awaiting a first message")
        return
    log(f"  {len(pending)} candidates")

    sent = 0
    for lid, name, title, company, purl in pending:
        if not dry_run and sent >= room:
            break
        if not dry_run and not force and not within_send_hours():
            log("  🕐 Drifted outside send window — stopping")
            break
        ok = send_message(driver, conn, lid, name, title or "", purl, dry_run=dry_run)
        if ok:
            sent += 1
            if not dry_run:
                # Human-like gap between messages, with occasional longer break.
                if random.random() < 0.15:
                    pause = random.uniform(120, 240)
                    log(f"  ☕ Longer break ({int(pause)}s)...")
                    time.sleep(pause)
                else:
                    human_delay(30, 90)
    log(f"  → Messaged {sent} people this run "
        f"(today now ~{messages_today(conn)}/{MESSAGE_DAILY_LIMIT})")

def inspect_message(driver, conn, profile_url=None):
    """Diagnostic: open a profile's message composer (INSPECT_PROFILE_URL), type a placeholder, and
    report the Send button (DOM-walk up from the editor) + composer markup.
    SENDS NOTHING — messaging a real person a placeholder is undesirable."""
    from selenium.webdriver.common.action_chains import ActionChains
    profile_url = profile_url or os.environ.get("INSPECT_PROFILE_URL", "")
    if not profile_url:
        log("✖ --inspect-message needs INSPECT_PROFILE_URL=https://www.linkedin.com/in/<someone>/")
        return
    log(f"\n🔬 MESSAGE COMPOSER INSPECTOR — {profile_url}")
    editor = open_message_composer(driver, profile_url)
    if not editor:
        log("  ⚠️  composer did not open — dumping page DOM")
        try:
            (Path.home() / "linkedin_message_dump.html").write_text(driver.page_source)
            log("  dumped to ~/linkedin_message_dump.html")
        except Exception:
            pass
        return
    log("  ✅ composer opened")
    log(f"  editor: aria={editor.get_attribute('aria-label')!r} "
        f"cls={(editor.get_attribute('class') or '')[:50]!r}")

    try:
        ActionChains(driver).move_to_element(editor).pause(0.2).click().perform()
    except Exception:
        pass
    slow_type(driver, editor, "(inspecting — not sending)")
    human_delay(1, 2)
    log(f"  editor text after typing: {(editor.text or editor.get_attribute('innerText') or '')!r}")

    # Walk UP from the editor to find the Send button.
    report = driver.execute_script("""
        var ed = arguments[0];
        function scan(root){
            var found=[];
            Array.from(root.querySelectorAll('button')).forEach(function(b){
                var t=(b.innerText||'').trim(), al=b.getAttribute('aria-label')||'';
                if (t==='Send'||/send/i.test(al)){
                    var r=b.getBoundingClientRect();
                    var top=(r.width>0&&r.height>0)?document.elementFromPoint(r.left+r.width/2,r.top+r.height/2):null;
                    found.push({text:t, aria:al, disabled:b.disabled,
                        ariaDisabled:b.getAttribute('aria-disabled'), visible:(r.width>0&&r.height>0),
                        rect:{x:Math.round(r.left),y:Math.round(r.top),w:Math.round(r.width),h:Math.round(r.height)},
                        centerIsButton:(top===b||(b.contains&&b.contains(top)))});
                }
            });
            return found;
        }
        var node=ed, depth=0, result=[];
        while(node && depth<12){
            var f=scan(node);
            result.push({depth:depth, tag:node.tagName, cls:(node.className||'').toString().slice(0,40),
                         btnCount:node.querySelectorAll('button').length, sendMatches:f});
            if (f.length) break;
            node=node.parentElement; depth++;
        }
        return result;
    """, editor)
    for lvl in report:
        log(f"  depth {lvl['depth']} <{lvl['tag']} .{lvl['cls']}> buttons={lvl['btnCount']} "
            f"sendMatches={len(lvl['sendMatches'])}")
        for b in lvl['sendMatches']:
            log(f"      → Send text={b['text']!r} aria={b['aria']!r} disabled={b['disabled']} "
                f"ariaDisabled={b['ariaDisabled']} vis={b['visible']} centerIsButton={b['centerIsButton']}")
            log(f"        rect={b['rect']}")

    # Dump thread bubble structure so sync_messages can read direction later.
    bubbles = driver.execute_script("""
        var out=[];
        var sels=['.msg-s-event-listitem','.msg-s-message-list__event','[class*="msg-s-event"]',
                  'li[class*="event"]'];
        for (var i=0;i<sels.length;i++){
            var els=document.querySelectorAll(sels[i]);
            if (els.length){
                Array.from(els).slice(0,4).forEach(function(e){
                    out.push({sel:sels[i], cls:(e.className||'').toString().slice(0,70),
                              text:(e.innerText||'').replace(/\\s+/g,' ').slice(0,60)});
                });
                break;
            }
        }
        return out;
    """)
    log(f"  thread bubbles sample ({len(bubbles)}):")
    for b in bubbles:
        log(f"      [{b['sel']}] cls={b['cls']!r} text={b['text']!r}")
    log("  → Harden _find_message_editor + the Send finder + bubble selectors from the above. Nothing was sent.")

def engage_feed(driver, conn, dry_run=False, limit=None, force=False):
    """React to (Like) and comment on the top feed posts. NEVER comments twice
    on the same post — deduped by post id in the DB, by post id within the run,
    and by scanning the post's own comment list for one of mine. `limit` caps
    how many posts to engage this run (defaults to FEED_POST_COUNT). `force`
    bypasses the business-hours restriction for deliberate testing."""
    target = limit if limit and limit > 0 else FEED_POST_COUNT
    log(f"\n📰 FEED ENGAGEMENT — targeting top {target} posts")

    if not dry_run:
        ok, info = ollama_available()
        if ok:
            log(f"  Ollama up at {OLLAMA_URL} — models: {info}")
            if OLLAMA_MODEL not in " ".join(info):
                log(f"  ⚠️  Model '{OLLAMA_MODEL}' not in list — will still try; "
                    f"set OLLAMA_MODEL to one of the above if it fails.")
        else:
            log(f"  ⚠️  Ollama not reachable ({info}) — "
                f"{'generic fallbacks will be posted' if COMMENT_ALLOW_FALLBACK else 'posts will be liked only'}.")

    my_name, _ = _resolve_identity(driver)
    commented_today = comments_today(conn)
    log(f"  Comments today: {commented_today}/{COMMENT_DAILY_LIMIT}")

    driver.get("https://www.linkedin.com/feed/")
    human_delay(4, 6)

    # The feed is virtualized and scrolls inside an inner container. Engage
    # what is rendered now, then scroll for more and re-locate: element
    # handles held across scrolls go stale, and never jump back to the top.
    engaged, seen_urns, unstable_ids = 0, set(), set()
    index, idle_rounds = 0, 0
    for round_no in range(target * 3 + 6):
        if engaged >= target:
            break
        posts = _find_feed_posts(driver)
        if not posts and round_no == 0:
            log("  Found 0 feed posts")
            _dump_search_dom(driver, "feed")
            return
        fresh = 0
        for post in posts:
            if engaged >= target:
                break
            try:
                urn = _post_urn(post)
                if not urn:
                    if post.id not in unstable_ids:
                        unstable_ids.add(post.id)
                        index += 1
                        log(f"  Post {index}: no stable id — skipping (can't guarantee no repeat)")
                    continue
                # The same post renders as several elements while the feed
                # virtualizes; dedup on the id, not on the element.
                if urn in seen_urns:
                    continue
                seen_urns.add(urn)
                fresh += 1
                index += 1
                i = index

                author, text = _post_author_and_text(post)
                record_engagement(conn, urn, author, text)

                # ── The never-twice guarantee ───────────────────────────────
                if has_commented(conn, urn):
                    log(f"  Post {i} [{author or urn[-12:]}]: already commented — skipping")
                    continue
                if comment_attempts(conn, urn) >= COMMENT_MAX_ATTEMPTS:
                    log(f"  Post {i} [{author or urn[-12:]}]: {COMMENT_MAX_ATTEMPTS} failed "
                        f"attempts already — giving up on this post")
                    continue

                driver.execute_script("arguments[0].scrollIntoView({block:'center'});", post)
                human_delay(1, 2)

                # React (Like)
                if not has_liked(conn, urn):
                    if dry_run:
                        log(f"  Post {i} [{author}]: [DRY RUN] would Like")
                    elif _click_like(driver, post):
                        mark_liked(conn, urn)
                        log(f"  Post {i} [{author}]: 👍 liked")
                        human_delay(1, 3)

                # Comment (guarded by daily limit + hours)
                if commented_today >= COMMENT_DAILY_LIMIT:
                    log(f"  Post {i}: comment daily limit reached — like only")
                    engaged += 1
                    continue
                if not dry_run and not force and not within_send_hours():
                    log("  🕐 Outside send window — like only, no comments (use --force to override)")
                    engaged += 1
                    continue
                if _already_commented_by_me(driver, post, my_name):
                    log(f"  Post {i} [{author}]: my comment is already on this post — skipping")
                    mark_commented(conn, urn, PRE_EXISTING_COMMENT)
                    continue

                comment = generate_comment(text, author)
                if not comment:
                    log(f"  Post {i} [{author}]: no safe comment produced — liked only")
                    engaged += 1
                    continue
                if dry_run:
                    log(f"  Post {i} [{author}]: [DRY RUN] would comment: {comment!r}")
                else:
                    if _post_comment(driver, post, comment):
                        mark_commented(conn, urn, comment)
                        commented_today += 1
                        log(f"  Post {i} [{author}]: 💬 commented: {comment!r}")
                        human_delay(20, 45)
                    else:
                        bump_comment_attempt(conn, urn)
                        log(f"  Post {i} [{author}]: comment failed")
                engaged += 1

            except Exception as e:
                log(f"  Post {index}: engagement error — {e}")
                continue

        log(f"  Feed round {round_no + 1}: {len(posts)} posts rendered, {fresh} new "
            f"(engaged {engaged}/{target})")
        if engaged >= target:
            break
        idle_rounds = 0 if fresh else idle_rounds + 1
        if idle_rounds >= 4:
            log("  Feed stopped yielding new posts — stopping")
            break
        how = _scroll_feed(driver)
        if how in ("none", "fallback"):
            log(f"  (scroll: {how})")
        human_delay(1.5, 3)

    log(f"  Found {len(seen_urns)} feed posts")
    log(f"  → Engaged with {engaged} posts (commented {commented_today} today)")

# ─── HARVEST ────────────────────────────────────────────────────────────────
def _today():
    return datetime.now().date().isoformat()

def harvest_pages_today(conn):
    row = conn.execute("SELECT pages FROM harvest_log WHERE day=?", (_today(),)).fetchone()
    return (row[0] or 0) if row else 0

def harvest_blocked_until(conn):
    """blocked_until while a search block is still in force, else None."""
    until = conn.execute("SELECT MAX(blocked_until) FROM harvest_log").fetchone()[0]
    return until if until and until > datetime.now().isoformat() else None

def _log_harvest(conn, pages=0, note=None, blocked_until=None):
    day = _today()
    conn.execute("INSERT OR IGNORE INTO harvest_log (day, pages) VALUES (?, 0)", (day,))
    conn.execute("""
        UPDATE harvest_log SET
            pages = COALESCE(pages, 0) + ?,
            note = CASE WHEN ? IS NULL THEN note
                        WHEN COALESCE(note, '') = '' THEN ? ELSE note || '; ' || ? END,
            blocked_until = COALESCE(?, blocked_until)
        WHERE day=?
    """, (pages, note, note, note, blocked_until, day))
    conn.commit()

def _search_limit_reset():
    """Local-naive ISO timestamp when LinkedIn's monthly search limit next resets.

    LinkedIn resets at 00:00 America/Los_Angeles on the 1st of each calendar
    month, which is 12.5–13.5 h ahead of local midnight on the Razer (IST). We
    compute that PT instant, convert it to local naive time and add a 1 h margin
    so the block does not release before LinkedIn's own reset. If we are already
    past PT's 1st for this month (still that PT day), block only until this
    month's PT reset instead of next month's, so a single limit hit does not cost
    the whole following month."""
    try:
        from zoneinfo import ZoneInfo
        pt = ZoneInfo("America/Los_Angeles")
        local = datetime.now().astimezone().tzinfo
        now_pt = datetime.now(pt)
        # The most recent PT month-start on or before now.
        this_reset = now_pt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        # If that reset has essentially just happened (we are on the PT 1st and
        # within a day of it), the current-month limit may still be lifting; aim
        # at this reset. Otherwise aim at next month's PT reset.
        if now_pt - this_reset < timedelta(days=1):
            reset_pt = this_reset
        else:
            y, m = now_pt.year + (now_pt.month == 12), now_pt.month % 12 + 1
            reset_pt = now_pt.replace(year=y, month=m, day=1, hour=0, minute=0,
                                      second=0, microsecond=0)
        reset_local = (reset_pt + timedelta(hours=1)).astimezone(local)
        return reset_local.replace(tzinfo=None).isoformat()
    except Exception:
        # Fallback: local 1st of next month (loses the PT margin but never crashes).
        now = datetime.now()
        return datetime(now.year + (now.month == 12), now.month % 12 + 1, 1).isoformat()

def _record_search_stop(conn, kind, detail, dry_run=False):
    """Log why searching stopped; a search limit also blocks searches until the
    next LinkedIn PT reset."""
    until = _search_limit_reset() if kind == "limit" else None
    log(f"  🛑 Stopping searches — {detail}" + (f"; blocked until {until}" if until else ""))
    if not dry_run:
        _log_harvest(conn, note=f"{datetime.now():%H:%M} {detail}", blocked_until=until)

def _harvest_gate(conn, pages=None, force=False):
    """(search pages allowed this run, reason when none)."""
    until = harvest_blocked_until(conn)
    if until:
        return 0, f"search blocked until {until}"
    if not force and not within_send_hours():
        return 0, (f"outside send window {SEND_HOURS[0]}:00–{SEND_HOURS[1]}:00 "
                   f"(use --force to override)")
    per_run = pages if pages and pages > 0 else HARVEST_PAGES_PER_RUN
    budget = max(0, min(per_run, HARVEST_MAX_PAGES_PER_DAY - harvest_pages_today(conn)))
    return budget, ("" if budget else
                    f"daily ceiling of {HARVEST_MAX_PAGES_PER_DAY} search pages reached")

def _cursor_state(conn):
    """{(company_key, role, geo): (next_page, exhausted)}. An exhausted cursor
    older than HARVEST_CURSOR_TTL_DAYS is re-opened from page 1 so discovery
    resumes for slices that may have new people since."""
    state = {}
    for k, r, g, n, e, ts in conn.execute(
            "SELECT company_key, role, geo, next_page, exhausted, updated_at "
            "FROM harvest_cursor"):
        if e and not _is_recent(ts, HARVEST_CURSOR_TTL_DAYS):
            state[(k, r, g)] = (1, 0)
        else:
            state[(k, r, g)] = (n or 1, e or 0)
    return state

def _save_cursor(conn, ckey, next_page, exhausted):
    conn.execute("INSERT OR REPLACE INTO harvest_cursor "
                 "(company_key, role, geo, next_page, exhausted, updated_at) "
                 "VALUES (?, ?, ?, ?, ?, ?)",
                 (*ckey, next_page, int(exhausted), datetime.now().isoformat(timespec="seconds")))
    conn.commit()

def _plan_harvest_pick(companies, cursors):
    """(company, role, geo) over the cursors that are not exhausted: company by
    tier weight, geo 'india' with probability india_share, role by weight.
    None once every cursor is exhausted."""
    def open_options(c):
        geos = [g for g, share in (("india", c["india_share"]), ("global", 1 - c["india_share"]))
                if share > 0]
        return [(r, g) for g in geos for r in SEARCH_ROLES
                if not cursors.get((c["key"], r, g), (1, 0))[1]]
    options = {c["key"]: open_options(c) for c in companies}
    pool = [c for c in companies if options[c["key"]]]
    if not pool:
        return None
    company = random.choices(pool, weights=[TIER_WEIGHTS.get(c["tier"], 1) for c in pool])[0]
    opts = options[company["key"]]
    geo = "india" if random.random() < company["india_share"] else "global"
    if not any(g == geo for _, g in opts):
        geo = opts[0][1]
    roles = [r for r, g in opts if g == geo]
    role = random.choices(roles, weights=[SEARCH_ROLES[r] for r in roles])[0]
    return company, role, geo

def _known_person(conn, linkedin_id):
    return conn.execute("SELECT 1 FROM outreach WHERE linkedin_id=?",
                        (linkedin_id,)).fetchone() is not None

def harvest(driver, conn, companies, pages=None, dry_run=False, force=False):
    """--harvest: search-results-only discovery. Walks (company, role, geo)
    result pages and saves every parsed person as 'found' (source 'harvest').
    Never opens a profile and never sends anything. Bounded per run and per
    day; stops on an authwall/checkpoint or LinkedIn's search limit."""
    log("\n🌾 HARVEST — search-results-only discovery"
        + (" [DRY RUN — writes nothing]" if dry_run else ""))
    budget, why = _harvest_gate(conn, pages, force)
    if not budget:
        log(f"  Harvest skipped — {why}")
        return
    log(f"  Pages this run: {budget} "
        f"(today so far {harvest_pages_today(conn)}/{HARVEST_MAX_PAGES_PER_DAY})")
    cursors = _cursor_state(conn)
    seen = set()
    dumps = os.environ.get("HARVEST_DEBUG") == "1"
    done = saved = empty_streak = 0
    while done < budget:
        if not force and not within_send_hours():
            log("  🕐 Drifted outside send window — stopping harvest")
            break
        pick = _plan_harvest_pick(companies, cursors)
        if not pick:
            log("  Every company's search cursors are exhausted — nothing left to harvest")
            break
        company, role, geo = pick
        ckey = (company["key"], role, geo)
        page = cursors.get(ckey, (1, 0))[0]
        cid = resolve_company_id(driver, conn, company, persist=not dry_run)
        stats = {}
        people = search_people(driver, company, role, geo=geo, page=page, company_id=cid,
                               source="harvest", dump_on_empty=dumps, stats=stats)
        done += 1
        if not dry_run:
            _log_harvest(conn, pages=1)
        kind, detail = _search_block_reason(driver)
        if kind:
            _record_search_stop(conn, kind, detail, dry_run)
            break
        if not stats.get("loaded"):
            _record_search_stop(conn, "error", "search page failed to load", dry_run)
            break
        cards = stats.get("cards") or 0
        no_results = "no results" in _page_text(driver)
        unexplained_empty = not cards and not no_results
        empty_streak = empty_streak + 1 if unexplained_empty else 0
        # A page that rendered cards but parsed none looks like a parser/layout
        # break: stop the run and leave the cursor untouched rather than exhaust it.
        if cards and not people:
            _record_search_stop(conn, "parse", f"{cards} cards rendered but none parsed "
                                "(parser/layout change?)", dry_run)
            break
        new = 0
        for p in people:
            if p["id"] in seen:
                continue
            seen.add(p["id"])
            tag = "" if p["company_verified"] else "  (company unverified)"
            if dry_run:
                if not _known_person(conn, p["id"]):
                    new += 1
                    log(f"  [DRY RUN] would save: {p['name']} | {p['title']} | {p['location']}{tag}")
            elif save_person(conn, p):
                new += 1
                log(f"  + {p['name']} | {p['title']} | {p['location']}{tag}")
        # Exhaust only on a real end of results: LinkedIn's 'no results', a
        # short page (fewer than a full page of cards), or the last page. A full
        # page whose people we already knew is NOT the end — advance to the next.
        thin_page = 0 < len(people) < HARVEST_PAGE_CARDS
        exhausted = no_results or thin_page or page >= HARVEST_MAX_PAGE
        cursors[ckey] = (page if exhausted else page + 1, int(exhausted))
        if not dry_run:
            _save_cursor(conn, ckey, *cursors[ckey])
        saved += new
        log(f"  {company['name']} · {role} · {geo} · page {page}: {new} new"
            + (" — cursor exhausted" if exhausted else ""))
        if empty_streak >= 2:
            _record_search_stop(conn, "empty", "two result pages in a row rendered no cards "
                                "(layout change?)", dry_run)
            break
        if done < budget:
            human_delay(20, 60)
    log(f"  → Harvest: {done} page(s), {saved} new people"
        + (" (dry run, nothing written)" if dry_run else ""))

# ─── SIMILAR-PROFILES HARVEST ────────────────────────────────────────────────
def similar_visits_today(conn):
    return conn.execute("SELECT COUNT(*) FROM similar_seed WHERE visited_at LIKE ?",
                        (f"{_today()}%",)).fetchone()[0]

def _similar_gate(conn, limit=None, force=False):
    """(profiles allowed this run, reason when none)."""
    if not force and not within_send_hours():
        return 0, (f"outside send window {SEND_HOURS[0]}:00–{SEND_HOURS[1]}:00 "
                   f"(use --force to override)")
    per_run = limit if limit and limit > 0 else SIMILAR_PER_RUN
    budget = max(0, min(per_run, SIMILAR_MAX_PER_DAY - similar_visits_today(conn)))
    return budget, ("" if budget else
                    f"daily ceiling of {SIMILAR_MAX_PER_DAY} profile visits reached")

def _engine_mail_factor():
    """{mail domain: seed weight factor} from the email engine's SMTP history,
    read-only: how often a guessed address there came back 'valid'. Catch-all and
    blocked domains cannot confirm anyone (0.1); a rejecting domain scores by its
    smoothed valid rate, so one where every guess fails (aliases, not names) sinks
    and an untested one keeps a neutral prior. {} when the state DB is absent."""
    if not OUTREACH_STATE_DB.is_file():
        return {}
    try:
        src = sqlite3.connect(OUTREACH_STATE_DB.resolve().as_uri() + "?mode=ro", uri=True, timeout=10)
        try:
            rows = src.execute("""
                SELECT d.domain, d.port25_ok, d.catch_all, COALESCE(v.ok, 0), COALESCE(v.bad, 0)
                FROM smtp_domains d LEFT JOIN (
                    SELECT lower(substr(email, instr(email, '@') + 1)) AS dom,
                           SUM(result = 'valid') AS ok, SUM(result = 'invalid') AS bad
                    FROM verifications GROUP BY dom) v ON v.dom = d.domain""").fetchall()
        finally:
            src.close()
    except sqlite3.Error:
        return {}
    out = {}
    for domain, port25_ok, catch_all, ok, bad in rows:
        if port25_ok == 0 or catch_all == 1:
            out[domain] = 0.1
        elif catch_all == 0:
            out[domain] = min(2.0, max(0.1, 4.0 * (ok + 1) / (ok + bad + 5)))
    return out

def _similar_seeds(conn, companies):
    """Profiles to open, best first: registry-verified people never visited, or
    whose rail was empty more than 14 days ago. Weighted towards recruiters,
    higher tiers, companies where the email engine's guesses actually verify
    (_engine_mail_factor), and companies with few known people so every target
    gets coverage."""
    by_key = {c["key"]: c for c in companies}
    retry = (datetime.now() - timedelta(days=14)).isoformat()
    rows = conn.execute("""
        SELECT o.linkedin_id, o.name, o.title, o.profile_url, o.company_key
        FROM outreach o LEFT JOIN similar_seed s ON s.linkedin_id = o.linkedin_id
        WHERE o.company_verified = 1 AND COALESCE(o.profile_url, '') LIKE '%/in/%'
          AND (s.linkedin_id IS NULL OR (COALESCE(s.rail_cards, 0) = 0 AND s.visited_at < ?))
    """, (retry,)).fetchall()
    known = dict(conn.execute("SELECT company_key, COUNT(*) FROM outreach "
                              "WHERE company_verified = 1 GROUP BY company_key").fetchall())
    factor = _engine_mail_factor()

    def weight(r):
        c = by_key[r[4]]
        mail = max([factor.get(d, 0.8) for d in c.get("email_domains", [])] or [0.8])
        role = 2.0 if RECRUITER_TITLE_RE.search(r[2] or "") else 1.0
        return TIER_WEIGHTS.get(c["tier"], 1) * mail * role / (1 + known.get(r[4], 0) / 10)

    seeds = [r for r in rows if r[4] in by_key]
    return [{"id": r[0], "name": r[1] or r[0], "title": r[2] or "", "url": r[3],
             "company_key": r[4]} for r in _weighted_order(seeds, weight)]

# [heading, card] for every person card inside a side-rail section whose heading
# lists other people. Nested boxes are fine: a card is returned once.
_SIMILAR_RAIL_JS = """
var want = arguments[0], skip = arguments[1], out = [], seen = new Set();
var profile = new RegExp('^/in/[^/]+/?$');
var starts = function (h, list) { return list.some(function (s) { return h.indexOf(s) === 0; }); };
var boxes = document.querySelectorAll('section, aside > div');
for (var i = 0; i < boxes.length; i++) {
  var box = boxes[i], h = box.querySelector('h2, h3');
  var head = ((h && (h.innerText || h.textContent)) || '').trim().toLowerCase();
  if (!head || starts(head, skip) || !starts(head, want)) continue;
  var links = box.querySelectorAll('a[href*="/in/"]');
  for (var j = 0; j < links.length; j++) {
    // Profile links only: the rail's "Show all" footer points at
    // /in/<seed>/overlay/browsemap-recommendations/.
    if (!profile.test(links[j].pathname || '')) continue;
    var card = links[j].closest('li') || links[j].parentElement;
    if (!card || seen.has(card) || !box.contains(card)) continue;
    seen.add(card);
    out.push([head, card]);
  }
}
return out;
"""

_PROFILE_URL = re.compile(r"^https?://[^/]+/in/[^/?#]+/?$")

def _similar_rail_people(driver, seed_slug):
    """(rail cards seen, [person]) from the open profile page's people rails."""
    try:
        found = driver.execute_script(_SIMILAR_RAIL_JS, list(SIMILAR_RAIL_HEADINGS),
                                      list(SIMILAR_SKIP_HEADINGS)) or []
    except Exception as e:
        log_debug(f"similar rail script failed: {e}")
        return 0, []
    people, seen = [], set()
    for _head, card in found:
        try:
            p = _parse_result_card(card, "")
        except Exception:
            continue
        if not p or p["id"] in seen or p["id"] == seed_slug or not _PROFILE_URL.match(p["url"]):
            continue
        seen.add(p["id"])
        people.append(p)
    return len(found), people

def _match_registry(headline, companies):
    """The one registry company the headline names as the current employer, else None."""
    hits = [c for c in companies if _names_company(headline, c)]
    return hits[0] if len(hits) == 1 else None

_EMPLOYER_MARKER = re.compile(r"(?:\bat\b|@)\s*[^\W_]", re.I)

def _unconfirmed_company(headline, seed_company, companies):
    """Best-guess registry company for a rail person whose headline does not
    confirm an employer: the one registry company the headline mentions (not as
    an ex-employer), else the seed's company. None when the headline states some
    other employer ('at X'), mentions several registry companies, or only an
    ex-employer. The email engine sends to these only after a mailbox at the
    company domain matches the name, so a wrong guess costs probes, not emails."""
    low = (headline or "").lower()
    if _EMPLOYER_MARKER.search(low):
        return None
    mentioned = []
    for c in companies:
        for n in sorted(_company_names(c), key=len, reverse=True):
            m = re.search(rf"(?<![a-z0-9]){re.escape(n)}(?![a-z0-9])", low)
            if m:
                if not _EX_EMPLOYER.search(low[:m.start()]):
                    mentioned.append(c)
                elif c is seed_company:
                    return None
                break
    if len(mentioned) > 1:
        return None
    return mentioned[0] if mentioned else seed_company

def harvest_similar(driver, conn, companies, limit=None, dry_run=False, force=False):
    """--harvest-similar: open known people's profiles and save the people in their
    'More profiles for you' rail who work at a registry company (source 'similar').
    No search, no invites, no messages. Bounded per run and per day; stops on an
    authwall/checkpoint or when three profiles in a row show no rail."""
    log("\n🧲 SIMILAR-PROFILES HARVEST — profile-page rails, no search"
        + (" [DRY RUN — writes nothing]" if dry_run else ""))
    budget, why = _similar_gate(conn, limit, force)
    if not budget:
        log(f"  Skipped — {why}")
        return
    seeds = _similar_seeds(conn, companies)
    by_key = {c["key"]: c for c in companies}
    log(f"  Profiles this run: {budget} (today so far {similar_visits_today(conn)}/"
        f"{SIMILAR_MAX_PER_DAY}) | seeds available: {len(seeds)}")
    visited = saved = empty_streak = 0
    dumped = False
    for seed in seeds:
        if visited >= budget:
            break
        if not force and not within_send_hours():
            log("  🕐 Drifted outside send window — stopping")
            break
        log(f"→ {seed['name']} · {seed['title'][:70]}")
        if not _get_with_retry(driver, seed["url"]):
            continue
        visited += 1
        human_delay(3, 6)
        kind, detail = _search_block_reason(driver)
        if kind == "auth":
            log(f"  🛑 Stopping — {detail}")
            break
        scroll_down(driver, 400)
        human_delay(1.5, 3)
        cards, people = _similar_rail_people(driver, _slug(seed["url"]) or seed["id"])
        new = 0
        seed_company = by_key.get(seed["company_key"])
        for p in people:
            c = _match_registry(p["title"], companies)
            confirmed = c is not None
            if not c:
                c = _unconfirmed_company(p["title"], seed_company, companies)
            if not c:
                continue
            # Unconfirmed rows only feed the email engine (mailbox-checked there);
            # they are never seeds and never invite targets.
            p.update(company=c["name"], company_key=c["key"], company_verified=confirmed,
                     source="similar", search_geo=None, location="", city="", country="")
            tag = "" if confirmed else "  (employer unconfirmed)"
            if dry_run:
                if not _known_person(conn, p["id"]):
                    new += 1
                    log(f"  [DRY RUN] would save: {p['name']} | {p['title'][:70]} → {c['name']}{tag}")
            elif save_person(conn, p):
                new += 1
                log(f"  + {p['name']} | {p['title'][:70]} → {c['name']}{tag}")
        if not dry_run:
            conn.execute("INSERT OR REPLACE INTO similar_seed (linkedin_id, visited_at, rail_cards, saved) "
                         "VALUES (?, ?, ?, ?)",
                         (seed["id"], datetime.now().isoformat(timespec="seconds"), cards, new))
            conn.commit()
        saved += new
        log(f"  rail: {cards} card(s), {len(people)} people, {new} new at target companies")
        if not cards and not dumped:
            _dump_search_dom(driver, "similar")
            dumped = True
        empty_streak = 0 if cards else empty_streak + 1
        if empty_streak >= 3:
            log("  🛑 Three profiles in a row showed no people rail (layout change?) — stopping")
            break
        if visited < budget:
            if random.random() < 0.15:
                pause = random.uniform(120, 240)
                log(f"  ☕ Taking a longer break ({int(pause)}s)...")
                time.sleep(pause)
            else:
                human_delay(25, 70)
    log(f"  → Similar-profiles harvest: {visited} profile(s) opened, {saved} new people"
        + (" (dry run, nothing written)" if dry_run else ""))

def india_backlog(conn, companies):
    """Invite queue: never-tried 'found' people from India-geo searches whose
    current employer is a verified registry company, oldest first."""
    keys = {c["key"] for c in companies}
    rows = conn.execute("""
        SELECT linkedin_id, name, title, company, location, profile_url, company_key
        FROM outreach
        WHERE status='found' AND search_geo='india' AND company_verified=1
          AND requested_at IS NULL AND last_activity_at IS NULL
          AND COALESCE(profile_url, '') != '' AND COALESCE(notes, '') != ?
        ORDER BY id
    """, (FIRST_DEGREE_NOTE,)).fetchall()
    return [{"id": r[0], "name": r[1] or r[0], "title": r[2] or "", "company": r[3] or "",
             "location": r[4] or "", "url": r[5], "company_key": r[6]}
            for r in rows if r[6] in keys]

def _weighted_order(items, weight):
    """Random order that favours heavier items (weighted shuffle)."""
    return sorted(items, key=lambda it: random.random() ** (1.0 / max(weight(it), 1e-6)),
                  reverse=True)

def connect_search_targets(driver, conn, companies, dry_run=False):
    """Yield invite candidates from fresh India searches (company facet, 2nd/3rd
    degree), saving every new person as 'found'. At most CONNECT_MAX_SEARCHES
    pages, within the daily search-page ceiling; stops on an authwall or
    LinkedIn's search limit."""
    until = harvest_blocked_until(conn)
    budget = min(CONNECT_MAX_SEARCHES, HARVEST_MAX_PAGES_PER_DAY - harvest_pages_today(conn))
    if until or budget <= 0:
        log("  No fresh searches — " + (f"search blocked until {until}" if until
                                        else "daily search-page ceiling reached"))
        return
    seen = set()
    order = _weighted_order(companies, lambda c: TIER_WEIGHTS.get(c["tier"], 1)
                            * max(c["india_share"], 0.05))
    for company in order[:budget]:
        role = random.choices(list(SEARCH_ROLES), weights=list(SEARCH_ROLES.values()))[0]
        log(f"\n── {company['name']} ──────────────────────")
        cid = resolve_company_id(driver, conn, company, persist=not dry_run)
        people = search_people(driver, company, role, geo="india", network=["S", "O"],
                               company_id=cid)
        if not dry_run:
            _log_harvest(conn, pages=1)
        kind, detail = _search_block_reason(driver)
        if kind:
            _record_search_stop(conn, kind, detail, dry_run)
            return
        for p in people:
            if p["id"] in seen:
                continue
            seen.add(p["id"])
            new = (not _known_person(conn, p["id"])) if dry_run else save_person(conn, p)
            if not new:
                continue
            log(f"  + {p['name']} | {p['title']} | {p['location']}"
                + ("" if p["company_verified"] else "  (company unverified — not invited)"))
            if p["company_verified"]:
                yield p
        human_delay(15, 40)  # between companies

# ─── MAIN ─────────────────────────────────────────────────────────────────────
def _find_company(companies, name):
    low = (name or "").strip().lower()
    for c in companies:
        if low and (low == c["key"] or low in _company_names(c)):
            return c
    return None

def debug_search(driver, conn, query, companies):
    """Run ONE people search and print exactly what was parsed (saves nothing).

    The fastest way to confirm the search selectors match the live DOM without
    running the full multi-company loop. Parses the query "Role at Company";
    a registry company is searched like the connect flow (company facet, India,
    2nd/3rd degree), any other name by keywords.
    """
    if " at " in query:
        role, name = query.rsplit(" at ", 1)
    else:
        role, name = query, ""
    role, name = role.strip(), name.strip()
    company = _find_company(companies, name) or {
        "key": "", "name": name or "Unknown", "linkedin_name": name, "aliases": [],
        "tier": "C", "india_share": 1.0, "linkedin_company_id": ""}
    cid = resolve_company_id(driver, conn, company) if company["key"] else None

    log(f"\n🔎 DEBUG SEARCH: role='{role}' company='{company['name']}' company_id={cid or '-'}")
    people = search_people(driver, company, role, geo="india", network=["S", "O"],
                           company_id=cid)

    if not people:
        log("  ⚠️  0 people parsed. Check the DOM dump referenced above.")
        return

    log(f"\n  ✅ Found {len(people)} people:")
    log("  " + "─" * 70)
    for p in people:
        log(f"    • {p['name']}  (first={p['first_name']!r} last={p['last_name']!r})")
        log(f"        title:    {p['title']}")
        log(f"        location: {p['location']}  (city={p['city']!r} country={p['country']!r})")
        log(f"        verified: {bool(p['company_verified'])}")
        log(f"        url:      {p['url']}")
        log("  " + "─" * 70)

def run(email=None, password=None, test_profiles=None, dry_run=False,
        manual_login=False, debug_query=None, engage_feed_flag=False,
        feed_limit=None, force=False, inspect_like_flag=False,
        inspect_comment_flag=False, inspect_message_flag=False,
        message_url=None, sync_connections_flag=False, sync_messages_flag=False,
        message_connections_flag=False, harvest_flag=False, harvest_pages=None,
        resolve_companies_flag=False, harvest_similar_flag=False, similar_limit=None):
    # Session banner — delimits each run in the persistent log file.
    mode = ("engage-feed" if engage_feed_flag else "debug-search" if debug_query
            else "inspect-like" if inspect_like_flag else "inspect-comment" if inspect_comment_flag
            else "inspect-message" if inspect_message_flag else "message" if message_url
            else "sync-connections" if sync_connections_flag else "sync-messages" if sync_messages_flag
            else "message-connections" if message_connections_flag
            else "resolve-companies" if resolve_companies_flag else "harvest" if harvest_flag
            else "harvest-similar" if harvest_similar_flag
            else "test-profiles" if test_profiles else "connect")
    log(f"══════ RUN START | mode={mode} | dry_run={dry_run} | force={force} ══════")
    log_debug(f"log file: {LOG_PATH}")

    if manual_login:
        log("→ Manual login mode: you'll log in by hand in the browser window.")
    else:
        saved_email, saved_password = _load_saved_creds()
        email    = email    or os.environ.get("LINKEDIN_EMAIL")    or saved_email
        password = password or os.environ.get("LINKEDIN_PASSWORD") or saved_password
        if not email:
            email = input("LinkedIn email: ")
        if not password:
            password = input("LinkedIn password: ")
        # Save for next time if not already stored.
        if email and password and not CREDS_PATH.exists():
            _save_creds(email, password)

    conn = init_db()
    log(f"DB: {DB_PATH} | log: {LOG_PATH}")
    companies = (load_companies()
                 if mode in ("connect", "harvest", "harvest-similar", "resolve-companies",
                             "debug-search") else [])
    if companies:
        log(f"Company registry: {len(companies)} target companies ({COMPANIES_CSV})")

    # ── Account-safety accounting ───────────────────────────────────────────
    sent_today = requests_today(conn)
    sent_week = requests_this_week(conn)
    pending = total_pending(conn)
    ever = total_requested_ever(conn)

    # Warm-up ramp: effective daily target grows as the account builds history.
    target = daily_target(ever)
    daily_room = min(DAILY_LIMIT, target) - sent_today
    weekly_room = WEEKLY_LIMIT - sent_week
    remaining = max(0, min(daily_room, weekly_room))

    log(f"Today: {sent_today} sent | This week: {sent_week}/{WEEKLY_LIMIT} | "
        f"Pending: {pending} | Lifetime: {ever}")
    log(f"Warm-up target today: {target} | Room left this run: {remaining}")

    if pending >= PENDING_WARN:
        log(f"⚠️  {pending} outstanding invites (≥{PENDING_WARN}). Consider withdrawing "
            f"stale ones on LinkedIn before sending more — a large pending pile is a risk signal.")

    stale = stale_pending(conn)
    if stale > 0:
        log(f"⚠️  {stale} invites pending >3 weeks — withdraw these on LinkedIn to protect "
            f"your acceptance rate (note: can't re-invite the same person for 3 weeks after).")

    ar = acceptance_rate(conn)
    if ar:
        rate, s, a = ar
        flag = "  ⚠️ LOW — a primary restriction trigger; target more relevant people" if rate < 20 else ""
        log(f"Acceptance rate: {rate:.0f}% ({a}/{s}){flag}")

    # These gates apply ONLY to the connection-request flow. The feed
    # engagement and debug-search flows run independently with their own logic
    # (feed likes off-hours; it only gates *comments* by hour internally).
    is_test_mode = (test_profiles or dry_run or debug_query or engage_feed_flag
                    or inspect_like_flag or inspect_comment_flag or inspect_message_flag
                    or message_url or sync_connections_flag or sync_messages_flag
                    or message_connections_flag or harvest_flag or resolve_companies_flag
                    or harvest_similar_flag)

    if mode == "harvest":
        budget, why = _harvest_gate(conn, harvest_pages, force)
        if not budget:
            log(f"Harvest skipped — {why}")
            conn.close()
            return
    if mode == "harvest-similar":
        budget, why = _similar_gate(conn, similar_limit, force)
        if not budget:
            log(f"Similar-profiles harvest skipped — {why}")
            conn.close()
            return

    if not is_test_mode:
        if sent_week >= WEEKLY_LIMIT:
            log(f"🛑 Weekly cap reached ({sent_week}/{WEEKLY_LIMIT}). Pausing to protect the "
                f"account — this is LinkedIn's real enforcement trigger. Resume in a few days.")
            export_csv(conn)
            return
        if remaining <= 0:
            log("Daily target reached for today. Run again tomorrow.")
            export_csv(conn)
            return
        if not within_send_hours() and not force:
            log(f"🕐 Outside send window {SEND_HOURS[0]}:00–{SEND_HOURS[1]}:00 local. "
                f"Sending invites off-hours looks automated — skipping sends this run. "
                f"(use --force to override)")
            export_csv(conn)
            return

    # Start browser — use version_main to match installed Chrome.
    # A persistent profile dir keeps the session cookie so you rarely need to
    # re-login (and re-face CAPTCHA/2FA) between runs.
    log("→ Starting Chrome with undetected-chromedriver...")
    profile_dir = Path.home() / ".linkedin_outreach_chrome"
    profile_dir.mkdir(exist_ok=True)
    # Chrome's omnibox suggest cache once grew Default/Preferences to 2.4 GB (nested re-escaped JSON) and Chrome
    # then crashed at startup on every run for weeks (2026-10-24..11-13). The login lives in Default/Cookies, so a
    # bloated Preferences file is deleted and Chrome regenerates it; BrowserMetrics is telemetry and only grows.
    prefs = profile_dir / "Default" / "Preferences"
    try:
        if prefs.exists() and prefs.stat().st_size > 50 * 1024 * 1024:
            log(f"⚠️  Chrome Preferences was {prefs.stat().st_size // (1024 * 1024)} MB (crashes Chrome); deleting it")
            prefs.unlink()
        for f in (profile_dir / "BrowserMetrics").glob("*"):
            f.unlink()
    except OSError as e:
        log(f"⚠️  Chrome profile cleanup skipped: {e}")

    def _build_options():
        """Fresh ChromeOptions per attempt — uc consumes the object on use, so
        it CANNOT be reused across retries (raises 'you cannot reuse the
        ChromeOptions object')."""
        o = uc.ChromeOptions()
        o.add_argument("--no-sandbox")
        o.add_argument("--disable-blink-features=AutomationControlled")
        o.add_argument("--start-maximized")
        o.add_argument(f"--user-data-dir={profile_dir}")
        o.add_argument("--profile-directory=Default")
        # Headless for display-less servers (e.g. Razer cron). Mac stays headed.
        if os.environ.get("HEADLESS") == "1":
            o.add_argument("--headless=new")
            o.add_argument("--disable-gpu")
            o.add_argument("--window-size=1280,900")
        return o

    def _detect_chrome_major():
        """Return the installed Chrome major version (int) or None. uc's driver
        auto-download can grab a newer ChromeDriver than the installed Chrome
        (e.g. driver 152 vs Chrome 151), which fails with SessionNotCreated —
        so we pin version_main to what's actually installed."""
        import subprocess, shutil
        candidates = [
            # macOS app bundles (not on PATH, so check explicit paths)
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
        ]
        # Linux / PATH binaries
        for exe in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
            p = shutil.which(exe)
            if p:
                candidates.append(p)
        for path in candidates:
            if not os.path.exists(path):
                continue
            try:
                out = subprocess.check_output([path, "--version"], text=True, timeout=10)
                m = re.search(r"\b(\d+)\.", out)
                if m:
                    return int(m.group(1))
            except Exception:
                continue
        return None

    chrome_major = _detect_chrome_major()
    log(f"→ Installed Chrome major version: {chrome_major or 'unknown'}")

    # Try pinned-to-installed-version first (fixes driver/Chrome mismatch), then
    # auto-detect, then bare. Each attempt gets a FRESH options object.
    driver = None
    attempts = []
    # uc re-downloads the driver on every launch, so a DNS blip kills the run.
    # A copy of the last driver that worked for this Chrome major launches
    # offline; the download path stays as the fallback and refreshes the copy.
    driver_cache = Path.home() / ".local" / "share" / "undetected_chromedriver"
    cached_driver = driver_cache / f"chromedriver-{chrome_major}" if chrome_major else None

    def _driver_matches(path):
        import subprocess
        try:
            out = subprocess.check_output([str(path), "--version"], text=True, timeout=10)
            return f" {chrome_major}." in out
        except Exception:
            return False

    if chrome_major:
        if cached_driver.exists() and _driver_matches(cached_driver):
            attempts.append(("cached driver %d" % chrome_major,
                             dict(version_main=chrome_major, driver_executable_path=str(cached_driver))))
        # Only a driver of the installed major can work. 'auto' downloads the
        # newest driver, which then refuses the older Chrome, so with a known
        # major we retry the pin instead.
        attempts += [("version_main=%d try %d" % (chrome_major, n), dict(version_main=chrome_major))
                     for n in (1, 2, 3)]
    else:
        attempts += [("auto", dict(version_main=None)), ("bare", dict())]

    last_err = None
    for label, kwargs in attempts:
        try:
            driver = uc.Chrome(options=_build_options(), **kwargs)
            log(f"→ Chrome launched ({label})")
            if cached_driver and "driver_executable_path" not in kwargs:
                try:
                    import shutil
                    src = Path(driver.patcher.executable_path)
                    if src.exists() and _driver_matches(src):
                        driver_cache.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(src, cached_driver)
                except Exception as e:
                    log(f"  (could not cache driver: {e})")
            break
        except Exception as e:
            last_err = e
            log(f"⚠️  Chrome launch attempt '{label}' failed: {str(e).splitlines()[0]}")
            time.sleep(10)
    if driver is None:
        raise RuntimeError(f"Could not launch Chrome after {len(attempts)} attempts: {last_err}")

    driver.set_window_size(1280, 900)
    try:
        driver.maximize_window()
        driver.execute_script("window.focus();")
    except Exception:
        pass
    log(f"→ Browser started. Chrome version: {driver.capabilities.get('browserVersion','?')}")

    try:
        if manual_login:
            driver.get("https://www.linkedin.com/login")
            log("→ Log in manually in the browser window (handle any CAPTCHA/2FA).")
            input("Press Enter here once you see your LinkedIn feed...")
            if "linkedin.com" not in driver.current_url:
                log("❌ Not on LinkedIn after manual login — stopping")
                return
            save_cookies(driver)  # so future runs are fully automatic
            log("✅ Continuing with manual session")
        elif not login(driver, email, password):
            log("❌ Login failed — stopping")
            return

        human_delay(2, 4)

        if debug_query:
            debug_search(driver, conn, debug_query, companies)
            export_csv(conn)
            return

        if inspect_like_flag:
            inspect_like(driver, conn)
            return

        if inspect_comment_flag:
            inspect_comment(driver, conn)
            return

        if inspect_message_flag:
            inspect_message(driver, conn)
            return

        if message_url:
            slug = message_url.rstrip("/").split("/")[-1]
            # Grab name/headline from the profile for a tailored message.
            name, headline = "", ""
            if _get_with_retry(driver, message_url):
                human_delay(2, 3)
                for sel in ["h1", ".text-heading-xlarge"]:
                    try:
                        name = driver.find_element(By.CSS_SELECTOR, sel).text.strip()
                        if name:
                            break
                    except Exception:
                        continue
                for sel in [".text-body-medium.break-words", ".text-body-medium",
                            "[data-generated-suggestion-target]"]:
                    try:
                        headline = driver.find_element(By.CSS_SELECTOR, sel).text.strip()
                        if headline:
                            break
                    except Exception:
                        continue
            # Ensure a DB row exists (as a connection, since we're messaging).
            save_person(conn, {"id": slug, "name": name or slug, "title": headline,
                               "company": "", "url": message_url, "status": "connected"})
            send_message(driver, conn, slug, name or slug, headline, message_url, dry_run=dry_run)
            export_csv(conn)
            return

        if sync_connections_flag:
            sync_connections(driver, conn)
            export_csv(conn)
            return

        if sync_messages_flag:
            sync_messages(driver, conn, dry_run=dry_run)
            export_csv(conn)
            return

        if message_connections_flag:
            send_bulk_messages(driver, conn, dry_run=dry_run, limit=feed_limit, force=force)
            export_csv(conn)
            return

        if engage_feed_flag:
            engage_feed(driver, conn, dry_run=dry_run, limit=feed_limit, force=force)
            export_csv(conn)
            return

        if resolve_companies_flag:
            resolve_companies(driver, conn, companies, dry_run=dry_run)
            return

        if harvest_flag:
            harvest(driver, conn, companies, pages=harvest_pages, dry_run=dry_run, force=force)
            return

        if harvest_similar_flag:
            harvest_similar(driver, conn, companies, limit=similar_limit, dry_run=dry_run, force=force)
            return

        if test_profiles:
            log(f"\n🧪 TEST MODE: Processing {len(test_profiles)} profiles")
            for url in test_profiles:
                linkedin_id = url.rstrip("/").split("/")[-1]
                # Visit the profile first to resolve real name/title.
                log(f"  Loading profile: {url}")
                _get_with_retry(driver, url)
                human_delay(3, 5)
                log(f"  Page loaded: {driver.current_url}")

                if "404" in driver.current_url or "unavailable" in driver.current_url:
                    log(f"  ⚠️  Profile not found (404) — skipping {url}")
                    continue

                # Read real name from the profile h1.
                name = linkedin_id
                title = ""
                for sel in ["h1.text-heading-xlarge", "h1"]:
                    try:
                        name = driver.find_element(By.CSS_SELECTOR, sel).text.strip()
                        if name:
                            break
                    except Exception:
                        continue
                for sel in [".text-body-medium.break-words", ".text-body-medium"]:
                    try:
                        title = driver.find_element(By.CSS_SELECTOR, sel).text.strip()
                        if title:
                            break
                    except Exception:
                        continue

                log(f"  Resolved: name={name!r} title={title!r}")
                btn_labels = [b.text.strip() for b in driver.find_elements(By.CSS_SELECTOR, "button")]
                log(f"  All buttons: {[b for b in btn_labels if b][:15]}")

                person = {
                    "id": linkedin_id,
                    "name": name,
                    "title": title or "Test Profile",
                    "company": "Test",
                    "location": "",
                    "url": url,
                    "status": "found"
                }
                save_person(conn, person)
                if not dry_run:
                    send_connection(driver, person, conn)
                    human_delay(3, 6)
                else:
                    log(f"  [DRY RUN] Would connect with {name} ({url})")
            export_csv(conn)
            return

        def _invite(person):
            """'sent' | 'failed' | 'stop' for one invite, with the live safety
            re-checks. A dry run never sends."""
            if dry_run:
                log(f"  [DRY RUN] Would connect with {person['name']} ({person['title']})")
                return "sent"
            # Live safety re-check: never cross the weekly cap mid-run,
            # and stop if we've drifted outside sending hours.
            if requests_this_week(conn) >= WEEKLY_LIMIT:
                log("🛑 Hit weekly cap mid-run — stopping to protect the account.")
                return "stop"
            if not within_send_hours() and not force:
                log("🕐 Drifted outside send window mid-run — stopping sends.")
                return "stop"
            if send_connection(driver, person, conn):
                # Human-like gap between invites: 30–90s, with an
                # occasional longer "coffee break".
                if random.random() < 0.15:
                    pause = random.uniform(120, 240)
                    log(f"  ☕ Taking a longer break ({int(pause)}s)...")
                    time.sleep(pause)
                else:
                    human_delay(30, 90)
                return "sent"
            # Tried once: leaves the backlog so it isn't re-visited every run.
            conn.execute("UPDATE outreach SET last_activity_at=? WHERE linkedin_id=?",
                         (datetime.now().isoformat(), person["id"]))
            conn.commit()
            return "failed"

        backlog = india_backlog(conn, companies)
        log(f"\n── India backlog: {len(backlog)} verified people waiting ──")

        def _targets():
            yield from backlog
            log(f"  Backlog short ({sent}/{remaining}) — fresh India searches")
            yield from connect_search_targets(driver, conn, companies, dry_run=dry_run)

        sent = failed_in_row = 0
        if remaining > 0:
            # Room is checked after each invite so no search starts once full.
            for person in _targets():
                outcome = _invite(person)
                if outcome == "stop":
                    break
                if outcome == "sent":
                    sent, failed_in_row = sent + 1, 0
                else:
                    failed_in_row += 1
                    if failed_in_row >= 5:
                        log("🛑 5 invite attempts in a row failed — stopping "
                            "(check the display / Connect button detection).")
                        break
                if sent >= remaining:
                    break

        log(f"\n✅ Done! Sent {sent} connection requests this run "
            f"(week now ~{requests_this_week(conn)}/{WEEKLY_LIMIT})")

    finally:
        if mode == "resolve-companies" or (mode == "harvest" and dry_run):
            log("→ Closing browser...")
        else:
            log("→ Exporting CSV and closing browser...")
            export_csv(conn)
        driver.quit()
        conn.close()
        log("Done.")

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--email", help="LinkedIn email")
    parser.add_argument("--password", help="LinkedIn password")
    parser.add_argument("--test", nargs="+", help="Test LinkedIn profile URLs")
    parser.add_argument("--dry-run", action="store_true", help="Find but don't send")
    parser.add_argument("--manual-login", action="store_true",
                        help="Log in by hand in the browser (best for CAPTCHA/2FA)")
    parser.add_argument("--debug-search", metavar="QUERY",
                        help="Run ONE search (e.g. 'Software Engineer at Google') "
                             "and print the people found, then exit")
    parser.add_argument("--engage-feed", action="store_true",
                        help="React to + comment on the top feed posts "
                             "(never comments twice on the same post)")
    parser.add_argument("--limit", type=int, metavar="N",
                        help="Cap feed engagement to N posts this run (e.g. --limit 1)")
    parser.add_argument("--force", action="store_true",
                        help="Bypass the business-hours restriction (for deliberate testing)")
    parser.add_argument("--inspect-like", action="store_true",
                        help="Diagnose why the like button click doesn't register")
    parser.add_argument("--inspect-comment", action="store_true",
                        help="Diagnose why the comment submit doesn't register")
    parser.add_argument("--inspect-message", action="store_true",
                        help="Diagnose the message composer/Send DOM (sends nothing)")
    parser.add_argument("--message", metavar="PROFILE_URL",
                        help="Send an AI-generated DM to one profile URL")
    parser.add_argument("--sync-connections", action="store_true",
                        help="Detect accepted requests; flip requested→connected")
    parser.add_argument("--sync-messages", action="store_true",
                        help="Read message threads; track replies/conversation status")
    parser.add_argument("--message-connections", action="store_true",
                        help="Bulk-message connected people not yet messaged (AI-generated)")
    parser.add_argument("--harvest", action="store_true",
                        help="Search-results-only discovery: save people as 'found' "
                             "(never opens profiles, never sends)")
    parser.add_argument("--pages", type=int, metavar="N",
                        help="Search pages for --harvest this run "
                             f"(default HARVEST_PAGES_PER_RUN={HARVEST_PAGES_PER_RUN})")
    parser.add_argument("--harvest-similar", action="store_true",
                        help="Open known people's profiles and save the people in their "
                             "'More profiles for you' rail at target companies (no search; "
                             "--limit N profiles this run)")
    parser.add_argument("--resolve-companies", action="store_true",
                        help="Resolve and print the registry's LinkedIn company ids, then exit")
    parser.add_argument("--sync-sheets", action="store_true",
                        help="Push the SQLite DB to Google Sheets and exit (no browser; "
                             "needs LINKEDIN_SHEETS_SYNC=1)")
    args = parser.parse_args()

    # Standalone Sheets push — no LinkedIn login / browser needed.
    if args.sync_sheets:
        conn = init_db()
        log(f"══════ RUN START | mode=sync-sheets ══════")
        export_google_sheets(conn)
        conn.close()
        log("Done.")
        raise SystemExit(0)

    run(
        email=args.email,
        password=args.password,
        test_profiles=args.test,
        dry_run=args.dry_run,
        manual_login=args.manual_login,
        debug_query=args.debug_search,
        engage_feed_flag=args.engage_feed,
        feed_limit=args.limit,
        force=args.force,
        inspect_like_flag=args.inspect_like,
        inspect_comment_flag=args.inspect_comment,
        inspect_message_flag=args.inspect_message,
        message_url=args.message,
        sync_connections_flag=args.sync_connections,
        sync_messages_flag=args.sync_messages,
        message_connections_flag=args.message_connections,
        harvest_flag=args.harvest,
        harvest_pages=args.pages,
        resolve_companies_flag=args.resolve_companies,
        harvest_similar_flag=args.harvest_similar,
        similar_limit=args.limit,
    )
