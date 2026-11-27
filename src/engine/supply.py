"""Supply: will the verified backlog fill the next batch? The forecast runs the compose selection
(sending.select_batch) with the per-company caps as of the next 09:30 send, so it predicts what Find & Send picks.
company_supply says, per registry company, whether its mail can be verified for free (SMTP profile of its email
domains) and how many more people it needs; LinkedIn harvesting reads it to steer toward companies whose people can
become sendable addresses. supply_daily keeps one funnel row per day."""
import datetime as dt
import json
import math
import sqlite3
from collections import Counter, defaultdict

import companies
import config
import sending
import state

CLASS_ORDER = ("checkable", "unknown", "catch_all", "blocked", "no_mx")
CLASS_FACTOR = {"checkable": 1.0, "unknown": 0.3, "catch_all": 0.0, "blocked": 0.0, "no_mx": 0.0}
TIER_WEIGHT = {"A": 3, "B": 2, "C": 1}
CONFIRMED = ("hash_verified", "smtp_valid", "provided")
SEND_AT = (9, 30)  # Find & Send schedule (src/n8n/build_workflows.py)
COHORT_DAYS = (10, 3)  # imports this many days old have had at least three nights of ladder probing
MIN_COHORT = 8


def domain_class(profile):
    if not profile or profile.get("port25_ok") is None:
        return "unknown"
    if profile["port25_ok"]:
        return "catch_all" if profile.get("catch_all") else "checkable"
    return "blocked" if profile.get("mx") else "no_mx"


def profiles(conn):
    return {r["domain"]: dict(r) for r in conn.execute("SELECT * FROM smtp_domains")}


def company_class(reg, profs):
    """The best class across the company's email domains: one checkable domain is enough."""
    found = [domain_class(profs.get(d)) for d in reg["email_domains"]]
    return min(found, key=CLASS_ORDER.index) if found else "unknown"


def next_send(now=None):
    now = now or dt.datetime.now()
    at = now.replace(hour=SEND_AT[0], minute=SEND_AT[1], second=0, microsecond=0)
    return at if now < at else at + dt.timedelta(days=1)


def verified(r):
    return r["resolve_method"] in CONFIRMED and (r["confidence"] or 0) >= config.MIN_CONFIDENCE


def cohort_rates(conn, profs):
    """Verify rates of imports that have had their probing nights: {company key | class | (origin, class): rate}
    for groups of at least MIN_COHORT people, plus raw counts under 'n'."""
    lo, hi = state.ago(days=COHORT_DAYS[0]), state.ago(days=COHORT_DAYS[1])
    groups = defaultdict(lambda: [0, 0])
    for r in conn.execute("""SELECT company, resolve_method, confidence, COALESCE(origin, source, 'sheet') AS origin
                             FROM prospects WHERE discovered_at BETWEEN ? AND ?""", (lo, hi)):
        reg = companies.lookup(r["company"] or "")
        cls = company_class(reg, profs) if reg else "other"
        for g in ((reg["key"] if reg else "~"), cls, (r["origin"], cls)):
            groups[g][0] += 1
            groups[g][1] += verified(r)
    rates = {g: v / n for g, (n, v) in groups.items() if n >= MIN_COHORT}
    rates["n"] = {str(g): n for g, (n, _) in groups.items()}
    return rates


def forecast(conn, as_of=None):
    """(picked rows, skipped Counter, company counts, ready rows) for the batch at `as_of` (default: next send)."""
    as_of = as_of or next_send()
    used = state.first_touch_sends_today(conn) if as_of.date() == dt.date.today() else 0
    ready = state.prospects(conn, list(state.READY) + ["composed"], order="pass, enriched_at")
    counts = state.company_first_touches(conn, companies.company_key, exclude_ids={r["prospect_id"] for r in ready},
                                         as_of=as_of)
    room = max(0, config.MAX_PER_DAY - used)
    items, skipped, _ = sending.select_batch(conn, ready, counts, state.contacted_emails(conn), room)
    return items, skipped, counts, ready


def company_rows(conn, counts, ready, profs, rates):
    contacted = state.contacted_emails(conn)
    sendable = Counter(companies.company_key(r["company"] or "") for r in ready
                       if not sending.skip_reason(conn, r, contacted))
    recent = state.ago(days=COHORT_DAYS[1] + 1)
    pending = Counter(companies.company_key(r["company"] or "") for r in conn.execute(
        "SELECT company FROM prospects WHERE status='discovered' OR (status='unverified' AND discovered_at >= ?)", (recent,)))
    out = []
    for reg in companies.REGISTRY.values():
        key, cls = reg["key"], company_class(reg, profs)
        done_month = counts.get(key, (0, 0))[1]
        headroom = max(0, reg["monthly_cap"] - done_month)
        rate = rates.get(key) or rates.get(cls) or config.DEFAULT_VERIFY_RATE
        want = min(3 * reg["daily_cap"], headroom)
        short = want - sendable[key] - pending[key] * rate
        needed = math.ceil(short / rate) if short > 0 and rate > 0 else 0
        base = CLASS_FACTOR[cls] * TIER_WEIGHT.get(reg["tier"], 1) * (reg["harvest"] == "1")
        out.append({"company_key": key, "name": reg["name"], "tier": reg["tier"], "class": cls,
                    "daily_cap": reg["daily_cap"], "monthly_cap": reg["monthly_cap"], "sent_30d": done_month,
                    "headroom": headroom, "ready": sendable[key], "pending": pending[key], "verify_rate": round(rate, 3),
                    "people_needed": needed, "harvest": int(reg["harvest"]),
                    "steer_weight": round(base * (1 + math.log1p(needed)) if needed else base * 0.1, 3)})
    return out, sendable


def linkedin_saves(day, profs):
    """New LinkedIn people on `day` by LinkedIn source and by mail class (read-only), plus page views by phase."""
    out = {"by_source": {}, "by_class": {}, "views": {}}
    try:
        li = sqlite3.connect(config.LINKEDIN_DB.as_uri() + "?mode=ro", uri=True, timeout=15)
    except sqlite3.Error:
        return out
    try:
        rows = li.execute("SELECT source, company_key FROM outreach WHERE date(created_at,'localtime')=?", (day,)).fetchall()
        out["by_source"] = dict(Counter(r[0] or "?" for r in rows))
        out["by_class"] = dict(Counter(company_class(companies.REGISTRY[r[1]], profs) if r[1] in companies.REGISTRY
                                       else "other" for r in rows))
        try:
            out["views"] = dict(li.execute("SELECT phase, n FROM page_views WHERE day=?", (day,)).fetchall())
        except sqlite3.Error:
            pass
    except sqlite3.Error as e:
        out["error"] = f"{type(e).__name__}: {e}"
    finally:
        li.close()
    return out


def funnel(conn, day, profs):
    def cls_of(company):
        reg = companies.lookup(company or "")
        return company_class(reg, profs) if reg else "other"
    imported = conn.execute("""SELECT company, COALESCE(origin, source, 'sheet') FROM prospects
                               WHERE substr(discovered_at,1,10)=?""", (day,)).fetchall()
    resolved = conn.execute("""SELECT company, COALESCE(origin, source, 'sheet'), resolve_method, confidence FROM prospects
                               WHERE substr(enriched_at,1,10)=?""", (day,)).fetchall()
    resolved = [r for r in resolved if r[2] in CONFIRMED and (r[3] or 0) >= config.MIN_CONFIDENCE]
    backlog = Counter(cls_of(r[0]) for r in conn.execute("SELECT company FROM prospects WHERE status='unverified'"))
    return {
        "linkedin": linkedin_saves(day, profs),
        "imported": {"total": len(imported), "by_origin": dict(Counter(r[1] for r in imported)),
                     "by_class": dict(Counter(cls_of(r[0]) for r in imported))},
        "verified": {"total": len(resolved), "by_origin": dict(Counter(r[1] for r in resolved)),
                     "by_class": dict(Counter(cls_of(r[0]) for r in resolved))},
        "probes": conn.execute("SELECT COUNT(*) FROM verifications WHERE substr(checked_at,1,10)=?", (day,)).fetchone()[0],
        "sent": conn.execute("SELECT COUNT(*) FROM prospects WHERE substr(sent_at,1,10)=? AND status!='sent_manual'",
                             (day,)).fetchone()[0],
        "unverified_by_class": dict(backlog),
    }


def snapshot(conn, as_of=None):
    target = config.SUPPLY_TARGET
    profs = profiles(conn)
    rates = cohort_rates(conn, profs)
    items, skipped, counts, ready = forecast(conn, as_of)
    rows, sendable = company_rows(conn, counts, ready, profs, rates)
    by_key = {r["company_key"]: r for r in rows}
    outside = [k for k in sendable if k not in by_key]  # sheet rows at companies outside the registry: 1/day each
    capacity = min(config.MAX_PER_DAY, sum(min(r["daily_cap"], r["headroom"]) for r in rows
                                           if r["class"] == "checkable" or r["ready"]) + len(outside))
    three_day = (sum(min(r["ready"], 3 * r["daily_cap"], r["headroom"]) for r in rows)
                 + sum(min(sendable[k], 3) for k in outside))
    tripped, bounces = state.breaker(conn)
    summary = {"as_of": (as_of or next_send()).isoformat(timespec="minutes"), "target": target,
               "forecast_1d": len(items), "forecast_3d": three_day, "capacity_1d": capacity,
               "supply_gap": max(0, min(target, capacity) - len(items)), "cap_gap": max(0, target - capacity),
               "breaker_tripped": tripped, "skipped": dict(skipped),
               "by_company": dict(Counter(r["company"] for r in items)),
               "classes": dict(Counter(r["class"] for r in rows)),
               "recorded_at": state.now()}
    return {"summary": summary, "companies": rows, "rates": {str(k): round(v, 3) for k, v in rates.items() if k != "n"},
            "cohort_sizes": rates["n"], "bounces": bounces}


def record(conn):
    """Store the snapshot for steering (company_supply), reports (supply_daily) and the digest (kv supply_forecast)."""
    snap = snapshot(conn)
    day = state.today()
    data = {**snap["summary"], "funnel": funnel(conn, day, profiles(conn)), "rates": snap["rates"]}
    cols = ("company_key", "name", "tier", "class", "daily_cap", "monthly_cap", "sent_30d", "headroom", "ready",
            "pending", "verify_rate", "people_needed", "steer_weight", "harvest")
    with state.write_txn(conn):
        conn.execute("DELETE FROM company_supply")
        conn.executemany(f"INSERT INTO company_supply({','.join(cols)},updated_at) VALUES({','.join('?' * len(cols))},?)",
                         [tuple(r[c] for c in cols) + (state.now(),) for r in snap["companies"]])
        conn.execute("""INSERT INTO supply_daily(day, recorded_at, data) VALUES(?,?,?)
                        ON CONFLICT(day) DO UPDATE SET recorded_at=excluded.recorded_at, data=excluded.data""",
                     (day, state.now(), json.dumps(data, default=str)))
        state.kv_set(conn, "supply_forecast", json.dumps(snap["summary"], default=str))
    return data


def stored_forecast(conn):
    """(summary dict, fresh) from the last record(), or (None, False)."""
    try:
        s = json.loads(state.kv_get(conn, "supply_forecast") or "null")
    except ValueError:
        s = None
    fresh = bool(s) and s.get("recorded_at", "") >= state.ago(hours=config.SUPPLY_STALE_HOURS)
    return s, fresh


def describe(s):
    gaps = []
    if s["supply_gap"]:
        gaps.append(f"{s['supply_gap']} short on verified people")
    if s["cap_gap"]:
        gaps.append(f"{s['cap_gap']} short on company caps (add companies)")
    return (f"{s['forecast_1d']} of target {s['target']} for {s['as_of'].replace('T', ' ')}"
            + (f" ({'; '.join(gaps)})" if gaps else " (on target)")
            + (" - BOUNCE BREAKER TRIPPED" if s.get("breaker_tripped") else ""))


def digest_lines(conn):
    s, fresh = stored_forecast(conn)
    if not s:
        return ["Supply forecast: not recorded yet (it is written by the evening intake)."]
    lines = [f"Supply forecast{'' if fresh else ' (STALE)'}: {describe(s)}"]
    prev = conn.execute("SELECT day, data FROM supply_daily ORDER BY day DESC LIMIT 1").fetchone()
    if prev:
        f = json.loads(prev["data"]).get("funnel", {})
        li = f.get("linkedin", {}).get("by_source", {})
        lines.append(f"Funnel {prev['day']}: LinkedIn saved {sum(li.values())} {li or ''}, engine imported "
                     f"{f.get('imported', {}).get('total', 0)}, verified {f.get('verified', {}).get('total', 0)}, "
                     f"probes {f.get('probes', 0)}")
    return lines
