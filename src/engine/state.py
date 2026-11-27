import csv
import datetime as dt
import secrets
import sqlite3
from contextlib import contextmanager

import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs(
  run_id TEXT PRIMARY KEY, kind TEXT, created_at TEXT, status TEXT,
  limit_n INTEGER, credits_used INTEGER DEFAULT 0, note TEXT);
CREATE TABLE IF NOT EXISTS companies(
  name TEXT PRIMARY KEY, business_id TEXT, tier TEXT, excluded INTEGER DEFAULT 0,
  last_searched_at TEXT, last_result TEXT, domain TEXT);
CREATE TABLE IF NOT EXISTS prospects(
  prospect_id TEXT PRIMARY KEY, business_id TEXT, company TEXT, tier TEXT,
  full_name TEXT, first_name TEXT, job_title TEXT, city TEXT, country TEXT,
  pass INTEGER, status TEXT, discovered_run TEXT, discovered_at TEXT,
  email TEXT, email_type TEXT, email_status TEXT, mx_ok INTEGER, enriched_at TEXT,
  composed_run TEXT, variant TEXT, subject TEXT, body TEXT, raw TEXT, composed_at TEXT,
  claimed_at TEXT, sent_at TEXT, message_id TEXT, thread_id TEXT, error TEXT,
  followup_due TEXT, followup_subject TEXT, followup_body TEXT, followup_raw TEXT,
  followup_claimed_at TEXT, followup_sent_at TEXT, followup_message_id TEXT,
  bounced_at TEXT, replied_at TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, run_id TEXT, prospect_id TEXT,
  event TEXT, detail TEXT);
CREATE TABLE IF NOT EXISTS patterns(
  domain TEXT, pattern TEXT, evidence TEXT, outcome TEXT, source TEXT, updated_at TEXT,
  PRIMARY KEY(domain, evidence));
CREATE TABLE IF NOT EXISTS smtp_domains(
  domain TEXT PRIMARY KEY, mx TEXT, port25_ok INTEGER, catch_all INTEGER, checked_at TEXT, note TEXT);
CREATE TABLE IF NOT EXISTS verifications(
  email TEXT PRIMARY KEY, result TEXT, code INTEGER, message TEXT, checked_at TEXT);
CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS supply_daily(day TEXT PRIMARY KEY, recorded_at TEXT, data TEXT);
CREATE TABLE IF NOT EXISTS company_supply(
  company_key TEXT PRIMARY KEY, name TEXT, tier TEXT, class TEXT, daily_cap INTEGER, monthly_cap INTEGER,
  sent_30d INTEGER, headroom INTEGER, ready INTEGER, pending INTEGER, verify_rate REAL, people_needed INTEGER,
  steer_weight REAL, harvest INTEGER, updated_at TEXT);
CREATE INDEX IF NOT EXISTS ix_prospects_status ON prospects(status);
CREATE INDEX IF NOT EXISTS ix_prospects_company ON prospects(company);
CREATE INDEX IF NOT EXISTS ix_events_run ON events(run_id);
"""
MIGRATIONS = {
    "prospects": ["last_name TEXT", "middle_name TEXT", "domain TEXT", "email_hash TEXT",
                  "company_name_api TEXT", "resolve_method TEXT", "resolve_pattern TEXT",
                  "confidence REAL", "email_guess TEXT", "verify_note TEXT",
                  "source TEXT", "linkedin_id TEXT", "profile_url TEXT", "origin TEXT"],
    "companies": ["domain TEXT"],
}

READY = ("enriched", "resolved")
PENDING = READY + ("composed", "sending")
ACTIVE_COMPANY = PENDING + ("sent", "sent_manual", "followup_composed", "followup_sending", "followup_sent", "replied")
CONTACTED = ("sending", "sent", "sent_manual", "followup_composed", "followup_sending", "followup_sent",
             "followup_failed", "bounced", "replied")
CSV_COLS = ["date", "full_name", "email", "company", "tier", "variant", "subject",
            "message_id", "thread_id", "status", "followup_date", "resolve_method"]
PATTERN_COLS = ["domain", "pattern", "evidence", "outcome", "source", "updated_at"]


def now():
    return dt.datetime.now().replace(microsecond=0).isoformat()


def today():
    return dt.date.today().isoformat()


def ago(**delta):
    return (dt.datetime.now() - dt.timedelta(**delta)).replace(microsecond=0).isoformat()


def add_business_days(start, n):
    d = start
    while n > 0:
        d += dt.timedelta(days=1)
        if d.weekday() < 5:
            n -= 1
    return d


def connect():
    conn = sqlite3.connect(str(config.DB_PATH), timeout=15, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=15000")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(SCHEMA)
    for table, cols in MIGRATIONS.items():
        have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        for col in cols:
            if col.split()[0] not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col}")
    return conn


@contextmanager
def write_txn(conn):
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def kv_get(conn, k, default=None):
    row = conn.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
    return row[0] if row else default


def kv_set(conn, k, v):
    conn.execute("INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, str(v)))


def new_run(conn, kind, limit_n=0, note=None):
    run_id = dt.datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(2)
    conn.execute("INSERT INTO runs(run_id,kind,created_at,status,limit_n,note) VALUES(?,?,?,?,?,?)",
                 (run_id, kind, now(), "started", limit_n, note))
    return run_id


def set_run(conn, run_id, **fields):
    if fields:
        cols = ", ".join(f"{k}=?" for k in fields)
        conn.execute(f"UPDATE runs SET {cols} WHERE run_id=?", (*fields.values(), run_id))


def add_run_credits(conn, run_id, credits):
    conn.execute("UPDATE runs SET credits_used=COALESCE(credits_used,0)+? WHERE run_id=?", (int(credits), run_id))


def event(conn, run_id, prospect_id, name, detail=""):
    conn.execute("INSERT INTO events(ts,run_id,prospect_id,event,detail) VALUES(?,?,?,?,?)",
                 (now(), run_id, prospect_id, name, str(detail)[:2000]))


def sync_companies(conn, companies):
    for name, (bid, tier) in companies.items():
        conn.execute("""INSERT INTO companies(name,business_id,tier,excluded) VALUES(?,?,?,0)
                        ON CONFLICT(name) DO UPDATE SET business_id=COALESCE(NULLIF(excluded.business_id,''),
                        companies.business_id), tier=excluded.tier, excluded=0""", (name, bid, tier))
    names = list(companies)
    conn.execute(f"UPDATE companies SET excluded=1 WHERE name NOT IN ({','.join('?' * len(names))})", names)


def mark_company_searched(conn, name, result, domain=None):
    conn.execute("UPDATE companies SET last_searched_at=?, last_result=?, domain=COALESCE(?, domain) WHERE name=?",
                 (now(), result, domain or None, name))


def companies_to_search(conn, tier=None, limit=60):
    cooldown = (dt.date.today() - dt.timedelta(days=config.COMPANY_COOLDOWN_DAYS)).isoformat()
    research = (dt.datetime.now() - dt.timedelta(days=config.COMPANY_RESEARCH_DAYS)).isoformat()
    placeholders = ",".join("?" * len(ACTIVE_COMPANY))
    rows = conn.execute(f"""
        SELECT c.* FROM companies c
        WHERE c.excluded=0 AND (? IS NULL OR c.tier=?)
          AND NOT EXISTS (SELECT 1 FROM prospects p WHERE p.company=c.name
                          AND (p.status IN ({placeholders}) OR COALESCE(p.sent_at,'') >= ?))
          AND (c.last_searched_at IS NULL OR c.last_searched_at < ?)
        ORDER BY COALESCE(c.last_searched_at,''), c.name""",
        (tier, tier, *ACTIVE_COMPANY, cooldown, research)).fetchall()
    by_tier = {"A": [], "B": [], "C": []}
    for r in rows:
        by_tier.setdefault(r["tier"], []).append(r)
    ordered, i = [], 0
    while len(ordered) < limit and any(by_tier.values()):
        bucket = by_tier[("A", "B", "C")[i % 3]]
        if bucket:
            ordered.append(bucket.pop(0))
        i += 1
    return ordered


def known_prospect_ids(conn):
    return {r[0] for r in conn.execute("SELECT prospect_id FROM prospects")}


def add_prospect(conn, row, run_id):
    cur = conn.execute("""INSERT OR IGNORE INTO prospects(prospect_id,business_id,company,tier,full_name,first_name,
                          last_name,middle_name,job_title,city,country,pass,status,discovered_run,discovered_at,
                          domain,email_hash,company_name_api,source,linkedin_id,profile_url,origin,updated_at)
                          VALUES(?,?,?,?,?,?,?,?,?,?,?,?,'discovered',?,?,?,?,?,?,?,?,?,?)""",
                       (row["prospect_id"], row["business_id"], row["company"], row["tier"], row["full_name"],
                        row["first_name"], row.get("last_name"), row.get("middle_name"), row["job_title"], row["city"],
                        row["country"], row["pass"], run_id, now(), row.get("domain"), row.get("email_hash"),
                        row.get("company_name_api"), row.get("source"), row.get("linkedin_id"), row.get("profile_url"),
                        row.get("origin") or row.get("source"), now()))
    return cur.rowcount == 1


def update(conn, prospect_id, **fields):
    fields["updated_at"] = now()
    cols = ", ".join(f"{k}=?" for k in fields)
    conn.execute(f"UPDATE prospects SET {cols} WHERE prospect_id=?", (*fields.values(), prospect_id))


def prospects(conn, statuses=None, tier=None, run_id=None, order="pass, discovered_at"):
    where, params = ["1=1"], []
    if statuses:
        where.append(f"status IN ({','.join('?' * len(statuses))})")
        params.extend(statuses)
    if tier:
        where.append("tier=?")
        params.append(tier)
    if run_id:
        where.append("(composed_run=? OR discovered_run=?)")
        params.extend([run_id, run_id])
    return conn.execute(f"SELECT * FROM prospects WHERE {' AND '.join(where)} ORDER BY {order}", params).fetchall()


def get(conn, prospect_id):
    return conn.execute("SELECT * FROM prospects WHERE prospect_id=?", (prospect_id,)).fetchone()


def claimable(r, kind):
    stale = ago(minutes=config.CLAIM_STALE_MINUTES)
    if kind == "followup":
        return r["status"] == "followup_composed" or (r["status"] == "followup_sending"
                                                      and (r["followup_claimed_at"] or "") < stale)
    return r["status"] == "composed" or (r["status"] == "sending" and (r["claimed_at"] or "") < stale)


def claim(conn, prospect_id, kind):
    stale = (dt.datetime.now() - dt.timedelta(minutes=config.CLAIM_STALE_MINUTES)).replace(microsecond=0).isoformat()
    if kind == "followup":
        cur = conn.execute("""UPDATE prospects SET status='followup_sending', followup_claimed_at=?, updated_at=?
                              WHERE prospect_id=? AND (status='followup_composed'
                                 OR (status='followup_sending' AND COALESCE(followup_claimed_at,'') < ?))""",
                           (now(), now(), prospect_id, stale))
    else:
        cur = conn.execute("""UPDATE prospects SET status='sending', claimed_at=?, updated_at=?
                              WHERE prospect_id=? AND (status='composed'
                                 OR (status='sending' AND COALESCE(claimed_at,'') < ?))""",
                           (now(), now(), prospect_id, stale))
    return cur.rowcount == 1


def first_touch_sends_today(conn):
    return conn.execute("SELECT COUNT(*) FROM prospects WHERE substr(sent_at,1,10)=? AND status!='sent_manual'",
                        (today(),)).fetchone()[0]


def followup_sends_today(conn):
    return conn.execute("SELECT COUNT(*) FROM prospects WHERE substr(followup_sent_at,1,10)=?", (today(),)).fetchone()[0]


def sends_today(conn):
    return first_touch_sends_today(conn) + followup_sends_today(conn)


def sending_today(conn, kind="new", exclude_id=None):
    status, col = ("followup_sending", "followup_claimed_at") if kind == "followup" else ("sending", "claimed_at")
    return conn.execute(f"SELECT COUNT(*) FROM prospects WHERE status=? AND substr({col},1,10)=? AND prospect_id!=?",
                        (status, today(), exclude_id or "")).fetchone()[0]


def company_first_touches(conn, keyfn, exclude_ids=(), composed=True, as_of=None):
    """{company key: [today, rolling 30 days]}: sent/sent_manual by sent_at plus in-flight sending and composed rows.
    `as_of` (a datetime) moves 'today' and the 30-day window, e.g. to forecast tomorrow's batch."""
    at = as_of or dt.datetime.now()
    t, cutoff = at.date().isoformat(), (at - dt.timedelta(days=30)).replace(microsecond=0).isoformat()
    skip, out = set(exclude_ids), {}
    rows = conn.execute("""SELECT prospect_id, company, status, sent_at, claimed_at FROM prospects
                           WHERE (sent_at IS NOT NULL AND sent_at >= ?) OR status IN ('composed','sending')""", (cutoff,))
    for r in rows:
        if r["prospect_id"] in skip or (r["status"] == "composed" and not composed):
            continue
        when = r["sent_at"] or (r["claimed_at"] if r["status"] == "sending" else t) or ""
        if when < cutoff:
            continue
        counts = out.setdefault(keyfn(r["company"] or ""), [0, 0])
        counts[0] += when[:10] == t
        counts[1] += 1
    return out


def bounce_stats(conn):
    """Bounce breaker inputs. The rolling window counts only verified first-touch sends: opt-in pattern_unverified
    sends are a deliberate gamble with their own per-domain breaker (PATTERN_BREAKER_DAYS), and manual sends were
    never resolved by the engine. Bounces older than BREAKER_WINDOW_DAYS no longer count, so a tripped breaker
    clears by itself instead of waiting for sends it is blocking."""
    last_24h = conn.execute("SELECT COUNT(*) FROM prospects WHERE bounced_at >= ?", (ago(hours=24),)).fetchone()[0]
    recent = conn.execute("""SELECT bounced_at FROM prospects WHERE sent_at IS NOT NULL AND status!='sent_manual'
                             AND COALESCE(resolve_method,'') NOT IN ('manual','pattern_unverified')
                             ORDER BY sent_at DESC LIMIT 100""").fetchall()
    cutoff = ago(days=config.BREAKER_WINDOW_DAYS)
    return {"last_24h": last_24h, "last_100": sum(1 for r in recent if r[0] and r[0] >= cutoff), "window": len(recent)}


def breaker(conn):
    stats = bounce_stats(conn)
    tripped = stats["last_24h"] >= config.BREAKER_BOUNCES_24H or stats["last_100"] >= config.BREAKER_BOUNCES_PER_100
    return tripped, stats


def domain_bounced_recently(conn, domain, days):
    d = (domain or "").strip().lower().lstrip("@")
    if not d:
        return False
    return conn.execute("""SELECT 1 FROM prospects WHERE bounced_at >= ?
                           AND lower(substr(email, instr(email, '@') + 1))=? LIMIT 1""", (ago(days=days), d)).fetchone() is not None


def email_already_contacted(conn, email, exclude_id=None):
    e = (email or "").strip().lower()
    if not e:
        return False
    return conn.execute(f"""SELECT 1 FROM prospects WHERE lower(trim(email))=? AND prospect_id!=?
                            AND (sent_at IS NOT NULL OR status IN ({','.join('?' * len(CONTACTED))})) LIMIT 1""",
                        (e, exclude_id or "", *CONTACTED)).fetchone() is not None


def contacted_emails(conn):
    return {r[0] for r in conn.execute(f"""SELECT lower(trim(email)) FROM prospects WHERE COALESCE(email,'')!=''
                                           AND (sent_at IS NOT NULL OR status IN ({','.join('?' * len(CONTACTED))}))""",
                                       CONTACTED)}


def purge_poisoned_domains(conn):
    return conn.execute("DELETE FROM smtp_domains WHERE note LIKE '%probe budget exhausted%'").rowcount


def followups_due(conn):
    return conn.execute("""SELECT * FROM prospects WHERE status='sent' AND followup_due IS NOT NULL
                           AND followup_due <= ? AND followup_sent_at IS NULL AND bounced_at IS NULL
                           AND replied_at IS NULL ORDER BY followup_due, sent_at""", (today(),)).fetchall()


def run_summary(conn, run_id):
    rows = conn.execute("SELECT status, COUNT(*) n FROM prospects WHERE composed_run=? GROUP BY status", (run_id,)).fetchall()
    run = conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
    return {r["status"]: r["n"] for r in rows}, (dict(run) if run else {})


def upsert_pattern(conn, domain, pattern, evidence, outcome, source):
    if not domain or not pattern:
        return
    conn.execute("""INSERT INTO patterns(domain,pattern,evidence,outcome,source,updated_at) VALUES(?,?,?,?,?,?)
                    ON CONFLICT(domain,evidence) DO UPDATE SET pattern=excluded.pattern, outcome=excluded.outcome,
                    source=excluded.source, updated_at=excluded.updated_at""",
                 (domain.lower(), pattern, (evidence or "").lower(), outcome, source, now()))


def best_pattern(conn, domain, scores):
    rows = conn.execute("SELECT * FROM patterns WHERE domain=?", ((domain or "").lower(),)).fetchall()
    if not rows:
        return None
    best = max(rows, key=lambda r: (scores.get(r["outcome"], 0), r["updated_at"] or ""))
    return best["pattern"]


def patterns_all(conn):
    return conn.execute("SELECT * FROM patterns ORDER BY domain, updated_at DESC").fetchall()


def export_patterns_csv(conn):
    rows = patterns_all(conn)
    with open(config.PATTERNS_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=PATTERN_COLS)
        w.writeheader()
        for r in rows:
            w.writerow({k: r[k] for k in PATTERN_COLS})
    return len(rows)


def export_csv(conn):
    rows = conn.execute("""SELECT * FROM prospects WHERE sent_at IS NOT NULL OR status IN ('composed','sending','send_failed')
                           ORDER BY COALESCE(sent_at, composed_at, updated_at)""").fetchall()
    with open(config.CSV_EXPORT, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CSV_COLS)
        w.writeheader()
        for r in rows:
            w.writerow({
                "date": (r["sent_at"] or r["composed_at"] or "")[:10], "full_name": r["full_name"],
                "email": r["email"], "company": r["company"], "tier": r["tier"], "variant": r["variant"],
                "subject": r["subject"], "message_id": r["message_id"], "thread_id": r["thread_id"],
                "status": r["status"], "followup_date": (r["followup_sent_at"] or "")[:10],
                "resolve_method": r["resolve_method"],
            })
    return len(rows)
