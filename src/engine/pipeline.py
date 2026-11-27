#!/usr/bin/env python3
"""Recruiter outreach pipeline CLI. Every command prints exactly one JSON object on stdout.
Exit 0 for controlled outcomes (check "ok"), exit 2 for unexpected crashes."""
import argparse
import csv
import datetime as dt
import json
import math
import re
import shutil
import signal
import sqlite3
import sys
import time
import traceback
from collections import Counter
from pathlib import Path

import companies
import config
import engine
import mailer
import resolver
import sending
import state
from explorium import Explorium, ExploriumError

INDIA_RE = re.compile(r"\b(india|bharat|bengaluru|bangalore|hyderabad|chennai|pune|mumbai|delhi|gurgaon|gurugram|noida|"
                      r"kolkata|ahmedabad|kochi|cochin|coimbatore|thiruvananthapuram|trivandrum|jaipur|chandigarh|indore|"
                      r"mysuru|mysore|karnataka|tamil nadu|telangana|maharashtra|kerala|haryana|gujarat)\b", re.I)
SHEET_ROWS_MAX = 500
EMPLOYER_RE = re.compile(r"(?:\bat\b|@)\s*([^|•·,;/()]+)", re.I)


class Deadline(BaseException):
    pass


def _deadline(signum, frame):
    raise Deadline()


def log(msg):
    line = f"{state.now()} {msg}"
    try:
        with open(config.LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass
    print(line, file=sys.stderr)


def emit(obj):
    obj.setdefault("ok", True)
    obj.setdefault("ts", state.now())
    print(json.dumps(obj, ensure_ascii=False, default=str))
    return 0


def room_today(conn, limit=None):
    used = state.first_touch_sends_today(conn)
    room = max(0, config.MAX_PER_DAY - used)
    if limit:
        room = min(room, limit)
    return used, room


def rank_title(title):
    t = (title or "").lower()
    score = 0
    if any(k in t for k in ("senior", "head", "lead", "manager", "principal")):
        score -= 2
    if any(k in t for k in ("technical", "engineering", "tech")):
        score -= 2
    if any(k in t for k in ("coordinator", "intern", "assistant", "sourcer")):
        score += 3
    return score


def domain_choices(comp, website=None):
    learned = (comp["domain"] if "domain" in comp.keys() else None) if hasattr(comp, "keys") else comp.get("domain")
    ordered = [resolver.domain_from_website(website or "")] + [learned or ""] + list(companies.DOMAINS.get(comp["name"], []))
    domains = [d for d in dict.fromkeys(d.lower() for d in ordered if d)]
    return {"domain": domains[0] if domains else "", "domains": domains}


def row_from_api(best, comp, india_only):
    full = (best.get("full_name") or "").strip()
    first, middle, last = resolver.split_name(full, best.get("first_name"), best.get("last_name"))
    return {
        "prospect_id": best["prospect_id"], "business_id": comp["business_id"], "company": comp["name"],
        "tier": comp["tier"], "full_name": full, "first_name": (best.get("first_name") or full.split(" ")[0]).strip(),
        "last_name": best.get("last_name") or last, "middle_name": middle,
        "job_title": best.get("job_title") or "", "city": (best.get("city") or "").title(),
        "country": (best.get("country_name") or "").title(), "pass": 1 if india_only else 2,
        "domain": "", "domains": [], "email_hash": best.get("professional_email_hashed") or "",
        "company_name_api": best.get("company_name") or "", "source": "explorium",
    } | domain_choices(comp, best.get("company_website"))


def search_company(client, comp, india_only, known_ids):
    filters = {
        "business_id": {"values": [comp["business_id"]]},
        "job_title": {"values": list(companies.JOB_TITLES), "include_related_job_titles": True},
        "has_contact_details": {"value": "email"},
    }
    if india_only:
        filters["country_code"] = {"values": ["IN"]}
    rows = client.fetch_prospects(filters, page_size=config.SEARCH_PAGE_SIZE, mode=config.SEARCH_MODE)
    picks, mismatch = [], None
    aliases = companies.ALIASES.get(comp["name"], [])
    for r in rows:
        pid = r.get("prospect_id")
        name = (r.get("full_name") or "").strip()
        if not pid or pid in known_ids or name.lower() in companies.EXCLUDED_PEOPLE:
            continue
        if r.get("business_id") and r["business_id"] != comp["business_id"]:
            continue
        if not resolver.company_name_matches(comp["name"], r.get("company_name") or "", aliases):
            mismatch = r.get("company_name")
            continue
        picks.append(r)
    if not picks:
        return None, mismatch
    best = sorted(picks, key=lambda r: rank_title(r.get("job_title")))[0]
    return row_from_api(best, comp, india_only), None


def seed_state(conn):
    companies.require()
    state.sync_companies(conn, companies.COMPANIES)
    for domain, pattern, evidence, outcome in resolver.KNOWN_PATTERNS:
        if not conn.execute("SELECT 1 FROM patterns WHERE domain=? AND evidence=?", (domain, evidence)).fetchone():
            state.upsert_pattern(conn, domain, pattern, evidence, outcome, "seed")
    added = 0
    for full, company, email, variant, date, outcome in companies.CONTACTED_SEED:
        pid = "manual-" + resolver.normalize(full) + "-" + resolver.normalize(company)
        if state.get(conn, pid):
            continue
        state.add_prospect(conn, {"prospect_id": pid, "business_id": "", "company": company, "tier": variant,
                                  "full_name": full, "first_name": full.split(" ")[0], "job_title": "", "city": "",
                                  "country": "", "pass": 1, "domain": email.split("@", 1)[1], "email_hash": "",
                                  "source": "manual"}, "seed")
        state.update(conn, pid, status=outcome, email=email, email_type="professional", variant=variant,
                     sent_at=f"{date}T09:00:00", bounced_at=(f"{date}T12:00:00" if outcome == "bounced" else None),
                     resolve_method="manual")
        added += 1
    state.export_patterns_csv(conn)
    state.export_csv(conn)
    return added


def cmd_seed(args):
    conn = state.connect()
    added = seed_state(conn)
    return emit({"ok": True, "contacted_seeded": added, "patterns": len(state.patterns_all(conn)),
                 "companies": conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0]})


def cmd_preflight(args):
    import verify
    checks = []
    def check(name, ok, detail="", advisory=False):
        checks.append({"name": name, "ok": bool(ok), "detail": detail, **({"advisory": True} if advisory else {})})
        return ok

    check("company_registry", not companies.ERROR,
          companies.ERROR or f"{len(companies.REGISTRY)} companies from {config.COMPANIES_CSV}")
    remaining = None
    if config.EXPLORIUM_ENABLED:
        check("api_key_present", bool(config.API_KEY), "EXPLORIUM_API_KEY set (free search only)" if config.API_KEY
              else "EXPLORIUM_API_KEY missing: discovery needs it, resolve/import do not", advisory=not config.ALLOW_PAID_REVEAL)
        if config.API_KEY:
            try:
                c = Explorium(config.API_KEY, log=log).credits()
                remaining = c["remaining"]
                check("explorium_reachable", True, f"remaining credits {remaining} / allocated {c['allocated']}")
                if config.ALLOW_PAID_REVEAL:
                    check("credits_above_reserve", remaining >= config.CREDIT_RESERVE + config.CREDITS_PER_CONTACT,
                          f"remaining {remaining}, reserve {config.CREDIT_RESERVE}")
            except ExploriumError as e:
                check("explorium_reachable", False, str(e), advisory=not config.ALLOW_PAID_REVEAL)
        check("paid_reveal", True, "ENABLED - Explorium credits may be spent" if config.ALLOW_PAID_REVEAL
              else "disabled - only free resolution (hash match, SMTP probe)", advisory=True)
    else:
        check("explorium", True, "disabled (OUTREACH_EXPLORIUM=false): no Explorium calls", advisory=True)
    check("private_profile", config.PRIVATE_PROFILE_LOADED,
          "private_profile.py loaded" if config.PRIVATE_PROFILE_LOADED
          else "private_profile.py missing: placeholder copy and sender would be used, so sending is blocked")
    info = mailer.resume_info()
    check("resume_pdf", info["ok"], f"{info.get('bytes', 0)} bytes sha256 {info.get('sha256', '')}" if info["ok"] else info["reason"])
    try:
        conn = state.connect()
        if not companies.ERROR:
            seed_state(conn)
        check("state_db_writable", True, str(config.DB_PATH))
    except Exception as e:
        conn = None
        check("state_db_writable", False, str(e))
    verification = None
    capability = getattr(verify, "verification_capability", None)
    if capability:
        try:
            verification = capability()
            path_ok = verification.get("direct_port25") is True or bool(verification.get("proxy_configured"))
            detail = json.dumps(verification, default=str)
            if not path_ok:
                detail = (f"no SMTP verification: direct port 25 {'blocked' if verification.get('direct_port25') is False else 'unknown'}"
                          f" and no SOCKS5 proxy (set OUTREACH_SMTP_PROXY_HOST); unverified pattern sends "
                          f"{'on' if config.UNVERIFIED_PATTERN_SENDS else 'off'} {detail}")
            check("verification_path", path_ok, detail, advisory=True)
        except Exception as e:
            check("verification_path", False, f"{type(e).__name__}: {e}", advisory=True)
    elif config.SMTP_ENABLED and conn:
        hosts = verify.mx_hosts("gmail.com")
        probe = verify.smtp_probe("postmaster@gmail.com", hosts[0]) if hosts else {"result": "unknown", "message": "no MX"}
        check("smtp_port25", probe.get("code") is not None, f"gmail MX answered {probe.get('code')} {probe.get('message', '')[:60]}"
              if probe.get("code") else f"port 25 unreachable: {probe.get('message', '')[:80]}", advisory=True)
        rep = verify.egress_reputation()
        check("egress_ip_reputation", rep["listed"] is False,
              f"{rep['ip']} is on Spamhaus ZEN {rep['codes']} - Microsoft 365 and some others will refuse probes; set OUTREACH_SMTP_PROXY_HOST to a clean SOCKS5 proxy"
              if rep["listed"] else (f"{rep['ip']} not listed on Spamhaus ZEN" if rep["listed"] is False else "lookup failed"), advisory=True)
        check("reacher_cli", verify.reacher_available(), str(config.REACHER_BIN) if verify.reacher_available() else "not installed, built-in probe only", advisory=True)
    free_mb = shutil.disk_usage(config.HERE).free // (1024 * 1024)
    check("disk_free", free_mb >= config.MIN_DISK_FREE_MB, f"{free_mb} MB free")
    import templates
    try:
        for v in "ABC":
            for i in range(len(templates.SUBJECTS[v]) * len(templates.BODIES[v])):
                templates.render(v, i, "X", "Y")
        templates.render_followup("X", "Y", "S")
        check("templates_render", True, "A/B/C + follow-up render")
    except Exception as e:
        check("templates_render", False, str(e))
    used, room = (room_today(conn) if conn else (0, 0))
    check("daily_room", room > 0, f"{used} first-touch sends today, room {room} of {config.MAX_PER_DAY}")
    window_ok = dt.date.today().weekday() in config.SEND_DAYS
    check("send_window", window_ok, f"{dt.date.today():%A}; send days {sorted(config.SEND_DAYS)} (0=Monday)", advisory=True)
    tripped, bounces = state.breaker(conn) if conn else (False, {})
    if conn:
        check("bounce_breaker", not tripped,
              f"{bounces['last_24h']} bounces in 24 h (trips at {config.BREAKER_BOUNCES_24H}), {bounces['last_100']} in the "
              f"last {bounces['window']} verified first-touch sends within {config.BREAKER_WINDOW_DAYS} days "
              f"(trips at {config.BREAKER_BOUNCES_PER_100})")
    backlog = sending.backlog_ready(conn) if conn and not companies.ERROR else 0
    check("verified_backlog", backlog >= config.MAX_PER_DAY,
          f"{backlog} verified contacts ready for a daily cap of {config.MAX_PER_DAY}", advisory=True)
    if conn:
        try:
            import supply
            s, fresh = supply.stored_forecast(conn)
            check("supply_forecast", bool(s) and fresh and s["forecast_1d"] >= s["target"],
                  supply.describe(s) + ("" if fresh else " (stale: not recorded by a recent intake)") if s
                  else "not recorded yet (the evening intake writes it)", advisory=True)
        except Exception as e:
            check("supply_forecast", False, f"{type(e).__name__}: {e}", advisory=True)
    pending = len(state.prospects(conn, list(state.PENDING))) if conn else 0
    unverified = len(state.prospects(conn, ["unverified"])) if conn else 0
    patterns = len(state.patterns_all(conn)) if conn else 0
    hard_ok = all(c["ok"] for c in checks if not c.get("advisory"))
    return emit({"ok": hard_ok, "checks": checks, "remaining_credits": remaining, "resume_sha256": info.get("sha256"),
                 "resume_bytes": info.get("bytes"), "sends_today": used, "room_today": room, "pending_ready": pending,
                 "unverified": unverified, "known_patterns": patterns, "send_window_ok": window_ok,
                 "send_days": sorted(config.SEND_DAYS), "breaker_tripped": tripped, "bounces": bounces,
                 "backlog_ready": backlog, "verification": verification, "explorium": config.EXPLORIUM_ENABLED,
                 "paid_reveal": config.ALLOW_PAID_REVEAL and config.EXPLORIUM_ENABLED, "min_confidence": config.MIN_CONFIDENCE,
                 "failed": [c["name"] for c in checks if not c["ok"] and not c.get("advisory")]})


def cmd_discover(args):
    conn = state.connect()
    run_id = state.new_run(conn, "discover", args.limit)
    used, room = room_today(conn, args.limit)
    pending = state.prospects(conn, list(state.PENDING), tier=args.tier)
    queued = state.prospects(conn, ["discovered", "unverified"], tier=args.tier)
    need = max(0, int(round((room - len(pending)) * config.DISCOVER_OVERSAMPLE)))
    base = {"run_id": run_id, "sends_today": used, "room_today": room, "pending_ready": len(pending),
            "queued_unverified": len(queued), "paid_reveal": config.ALLOW_PAID_REVEAL}
    if not config.EXPLORIUM_ENABLED:
        state.set_run(conn, run_id, status="skipped", note="explorium disabled")
        return emit({**base, "ok": False, "stage": "disabled",
                     "error": "Explorium is disabled (OUTREACH_EXPLORIUM=false); LinkedIn and the sheet feed the engine"})
    if room == 0:
        state.set_run(conn, run_id, status="skipped", note="daily cap reached")
        return emit({**base, "ok": True, "count": 0, "est_credits": 0, "to_enrich": 0, "note": "daily cap reached, nothing to do"})
    if len(pending) >= room:
        state.set_run(conn, run_id, status="done", note="enough verified contacts queued")
        return emit({**base, "ok": True, "count": 0, "est_credits": 0, "to_enrich": 0, "note": "enough verified contacts already queued"})
    if not config.API_KEY:
        state.set_run(conn, run_id, status="failed", note="no api key")
        return emit({**base, "ok": False, "stage": "credits", "error": "EXPLORIUM_API_KEY missing; use 'import' or 'resolve' to feed names without it"})
    try:
        client = Explorium(config.API_KEY, log=log, allow_billing=config.ALLOW_SEARCH_CREDITS)
        remaining = client.credits()["remaining"]
    except ExploriumError as e:
        state.set_run(conn, run_id, status="failed", note=str(e))
        return emit({**base, "ok": False, "error": str(e), "stage": "credits"})
    if remaining <= 0:
        state.set_run(conn, run_id, status="failed", note="zero credits")
        return emit({**base, "ok": False, "stage": "credits", "remaining_credits": 0,
                     "error": "Explorium balance is 0: even free preview searches are refused until the account has credits"})
    seed_state(conn)
    known = state.known_prospect_ids(conn)
    targets = state.companies_to_search(conn, tier=args.tier, limit=args.max_companies)
    deadline = time.monotonic() + args.time_budget
    found, verified, searched, errors, mismatches = [], [], 0, [], []
    for comp in targets:
        if len(verified) >= need or time.monotonic() > deadline:
            break
        searched += 1
        try:
            row, mism = search_company(client, comp, True, known)
            if not row:
                row, mism2 = search_company(client, comp, False, known)
                mism = mism or mism2
        except ExploriumError as e:
            errors.append({"company": comp["name"], "error": str(e)})
            log(f"discover {comp['name']}: {e}")
            if not e.retryable:
                break
            continue
        if mism and not row:
            mismatches.append({"company": comp["name"], "api_company_name": mism})
            state.event(conn, run_id, None, "company_mismatch", f"{comp['name']} returned as {mism!r}")
        state.mark_company_searched(conn, comp["name"], "found" if row else ("mismatch" if mism else "none"),
                                    row["domain"] if row else None)
        if not row:
            continue
        state.add_prospect(conn, row, run_id)
        known.add(row["prospect_id"])
        state.event(conn, run_id, row["prospect_id"], "discovered", f"{row['company']} pass {row['pass']}")
        res = engine.resolve_contact(conn, run_id, row, allow_smtp=not args.no_smtp)
        engine.apply_result(conn, run_id, row["prospect_id"], res)
        row.update({"email": res["email"] if res["verified"] else None, "guess": res.get("guess"),
                    "method": res["method"], "confidence": res["confidence"], "verified": res["verified"],
                    "probes": res["probes"], "note": res["note"]})
        found.append(row)
        if res["verified"]:
            verified.append(row)
        log(f"discover {'IN ' if row['pass'] == 1 else '-- '}{comp['name']:<18} {row['full_name']} | "
            f"{res['method']} conf={res['confidence']} {'VERIFIED ' + res['email'] if res['verified'] else 'unverified ' + str(res.get('guess'))}")
    found.sort(key=lambda r: (not r["verified"], r["pass"]))
    pending_now = len(state.prospects(conn, list(state.PENDING), tier=args.tier))
    unverified_now = state.prospects(conn, ["discovered", "unverified"], tier=args.tier)
    to_enrich = max(0, min(room - pending_now, len(unverified_now))) if config.ALLOW_PAID_REVEAL else 0
    status = "done" if not errors or found else "failed"
    try:
        credits_after = client.credits()["remaining"]
    except ExploriumError:
        credits_after = None
    state.set_run(conn, run_id, status=status, credits_used=client.credits_used,
                  note=f"searched {searched}, found {len(found)}, verified {len(verified)}, errors {len(errors)}, search credits {client.credits_used}")
    slim = [{k: r.get(k) for k in ("prospect_id", "full_name", "company", "tier", "job_title", "city", "country", "pass",
                                   "domain", "email", "guess", "method", "confidence", "verified", "probes", "note")} for r in found]
    return emit({**base, "ok": status == "done", "count": len(found), "verified_free": len(verified),
                 "search_mode": config.SEARCH_MODE, "search_credits_used": client.credits_used, "credits_after": credits_after,
                 "resolved_free": len(verified), "credits_saved": len(verified) * config.CREDITS_PER_CONTACT,
                 "unverified": len(found) - len(verified), "searched_companies": searched,
                 "companies_remaining": max(0, len(targets) - searched), "india": sum(1 for r in found if r["pass"] == 1),
                 "abroad": sum(1 for r in found if r["pass"] == 2), "candidates": slim, "pending_ready": pending_now,
                 "to_enrich": to_enrich, "est_credits": to_enrich * config.CREDITS_PER_CONTACT,
                 "remaining_credits": remaining, "api_calls": client.calls, "errors": errors, "company_mismatches": mismatches})


def new_stats():
    return {"checked": 0, "verified": 0, "unverified": 0, "osint_domains": 0, "stopped": None}


def probes_left():
    import verify
    return config.SMTP_MAX_PROBES_PER_RUN - (getattr(verify, "_probes_this_run", 0) or 0)


def smtp_usable(verify):
    if not config.SMTP_ENABLED:
        return False, None
    capability = getattr(verify, "verification_capability", None)
    if not capability:
        return True, None
    try:
        cap = capability() or {}
    except Exception as e:
        return True, {"error": f"{type(e).__name__}: {e}"}
    return bool(cap.get("proxy_configured")) or cap.get("direct_port25") is not False, cap


def reverify_rows(conn, run_id, rows, allow_smtp, stats, deadline=None, keep=False):
    smtp, done = allow_smtp and config.SMTP_ENABLED, []
    for r in rows:
        if deadline and time.monotonic() >= deadline:
            stats["stopped"] = "time budget"
            break
        why = sending.block_reason(r)
        if why:
            if r["status"] in ("discovered", "unverified"):
                state.update(conn, r["prospect_id"], status="excluded", error=why)
            continue
        if not sending.target_ok(r):
            continue
        # verify caches a budget-refused probe as port25_ok=0 for 30 days: never start a contact that could run out
        if smtp and probes_left() <= config.SMTP_MAX_PROBES_PER_CONTACT:
            stats["stopped"] = "probe budget"
            break
        row, reg = dict(r), companies.lookup(r["company"] or "")
        if reg:
            row["domains"] = [r["domain"]] + [d for d in reg["email_domains"] if d != r["domain"]]
        res = engine.resolve_contact(conn, run_id, row, allow_smtp=smtp)
        engine.apply_result(conn, run_id, r["prospect_id"], res)
        stats["checked"] += 1
        stats["verified" if res["verified"] else "unverified"] += 1
        if keep:
            done.append({"prospect_id": r["prospect_id"], "full_name": r["full_name"], "company": r["company"],
                         "email": res["email"], "method": res["method"], "confidence": res["confidence"],
                         "verified": res["verified"], "probes": res["probes"], "note": res["note"]})
    return done


def cmd_reverify(args):
    import verify
    companies.require()
    conn = state.connect()
    run_id = state.new_run(conn, "reverify", 0)
    purged = verify.purge_blocked(conn)
    state.purge_poisoned_domains(conn)
    rows = [state.get(conn, args.prospect_id)] if args.prospect_id else state.prospects(conn, ["discovered", "unverified"])
    stats = new_stats()
    done = reverify_rows(conn, run_id, [r for r in rows if r], not args.no_smtp, stats, keep=not args.compact)
    state.export_csv(conn)
    out = {"ok": True, "run_id": run_id, "checked": stats["checked"], "verified": stats["verified"],
           "purged_policy_blocks": purged[0], "reset_domain_profiles": purged[1], "stopped": stats["stopped"]}
    return emit(out if args.compact else {**out, "results": done})


def cmd_resolve(args):
    conn = state.connect()
    run_id = state.new_run(conn, "resolve", 0)
    domain = args.domain or ""
    row = {"full_name": args.name, "first_name": None, "last_name": None, "domain": domain, "email_hash": args.hash or ""}
    res = engine.resolve_contact(conn, run_id, row, allow_smtp=not args.no_smtp)
    cands = [e for e, _ in resolver.candidates(args.name, domain)][:8]
    return emit({"ok": True, "name": args.name, "domain": domain, **res, "top_candidates": cands})


def csv_rows(path):
    from pathlib import Path
    p = Path(path)
    files = sorted(p.glob("*.csv")) if p.is_dir() else [p]
    for fp in files:
        with open(fp, newline="", encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                yield {(k or "").strip().lower(): (v or "").strip() for k, v in r.items()}


def normalize_country(country, location=""):
    c = " ".join((country or "").split())
    if c:
        return "India" if c.lower() in ("in", "ind") or INDIA_RE.search(c) else c
    return "India" if INDIA_RE.search(location or "") else ""


def _cased(word):
    return word.title() if word.isupper() or word.islower() else word


def _display(norm, *texts):
    """The display words spelling the resolver's normalized name part, title-cased when typed in one case."""
    for text in texts:
        toks = [re.sub(r"^[\W_]+|[\W_]+$", "", t) for t in re.split(r"[\s.,]+", text or "")]
        toks = [t for t in toks if resolver.normalize(t)]
        for i in range(len(toks)):
            spelled = ""
            for j in range(i, len(toks)):
                spelled += resolver.normalize(toks[j])
                if spelled == norm:
                    return " ".join(_cased(t) for t in toks[i:j + 1])
                if not norm.startswith(spelled):
                    break
    return norm.capitalize()


def split_person(full_name, first=None, last=None):
    """(first, last) as the resolver splits the name: the given name is never an initial, and an initials-only
    surname is stored as spaced letters so the resolver still reads it as initials."""
    p = resolver.split_person_name(full_name, first, last)
    given = _display(p["first"], first, full_name) if p["first"] else ""
    family = _display(p["last"], last, full_name) if p["last"] else " ".join(p["initials"].upper())
    return given, family


def clean_headline(text):
    t = re.split(r"\s*[|•·]\s*|\s[-–—]\s", text or "")[0]
    t = re.split(r"\s(?:at\s|@)", t, flags=re.I)[0]
    return " ".join(re.sub(r"[^\w&/+.,()' -]", " ", t).split())[:120]


def slug_from_url(url):
    m = re.search(r"/in/([^/?#]+)", url or "")
    return m.group(1) if m else ""


def person_keys(conn):
    return {(companies.person_key(r["full_name"]), companies.company_key(r["company"] or ""))
            for r in conn.execute("SELECT full_name, company FROM prospects")}


def check_provided(email):
    verdict = mailer.classify_email(email, "professional", None)
    if not verdict.startswith("ok"):
        return verdict
    return "no MX" if mailer.mx_ok(email.split("@", 1)[1]) is False else "ok"


def import_csv(conn, run_id, path, resolve=True, allow_smtp=True):
    added, results, skipped, reasons, known = 0, [], [], Counter(), person_keys(conn)
    for r in csv_rows(path):
        name = " ".join((r.get("full_name") or r.get("name") or "").split())
        company = (r.get("company") or "").strip()
        if not name or not company:
            continue
        reg = companies.lookup(company)
        company = reg["name"] if reg else company
        pid = "import-" + resolver.normalize(name) + "-" + resolver.normalize(company)
        if state.get(conn, pid):
            continue
        provided = (r.get("email") or "").strip().lower()
        domain = resolver.domain_from_website(r.get("domain") or "") or (reg["email_domains"][0] if reg and reg["email_domains"] else "")
        person = (companies.person_key(name), companies.company_key(company))
        why = ("excluded company" if companies.excluded_company(company, provided)
               else "excluded person" if companies.excluded_person(name)
               else "already contacted" if state.email_already_contacted(conn, provided)
               else "duplicate" if person in known
               else None if domain else "unknown company: add it to config/companies.csv or fill the domain column")
        if why:
            reasons[why.split(":")[0]] += 1
            skipped.append({"name": name, "company": company, "reason": why})
            continue
        first, last = split_person(name, r.get("first_name"), r.get("last_name"))
        country = normalize_country(r.get("country"), r.get("city"))
        row = {"prospect_id": pid, "business_id": "", "company": company,
               "tier": (r.get("tier") or (reg["tier"] if reg else "B")).upper(), "full_name": name, "first_name": first,
               "last_name": last, "middle_name": None, "job_title": r.get("job_title") or "", "city": r.get("city") or "",
               "country": country, "pass": 1 if country == "India" else 2, "domain": domain,
               "domains": [domain] + [d for d in (reg["email_domains"] if reg else []) if d != domain],
               "email_hash": r.get("email_hash") or "", "company_name_api": company, "source": "sheet"}
        state.add_prospect(conn, row, run_id)
        known.add(person)
        added += 1
        verdict = check_provided(provided) if provided else None
        if verdict == "ok":
            state.update(conn, pid, status="resolved", email=provided, email_type="professional", email_status="provided",
                         resolve_method="provided", confidence=1.0, enriched_at=state.now())
            results.append({"name": name, "company": company, "email": provided, "method": "provided", "verified": True})
            continue
        note = f"supplied email not used: {verdict}" if verdict else ""
        smtp = allow_smtp and config.SMTP_ENABLED
        if not resolve or (smtp and probes_left() <= config.SMTP_MAX_PROBES_PER_CONTACT):
            if note:
                state.update(conn, pid, verify_note=note)
            results.append({"name": name, "company": company, "email": None, "method": None, "verified": False,
                            "note": "; ".join(x for x in (note, "queued for verification") if x)})
            continue
        res = engine.resolve_contact(conn, run_id, row, allow_smtp=smtp)
        engine.apply_result(conn, run_id, pid, res)
        results.append({"name": name, "company": company, "email": res["email"], "guess": res.get("guess"),
                        "method": res["method"], "confidence": res["confidence"], "verified": res["verified"],
                        "note": "; ".join(x for x in (note, res["note"]) if x)})
    return {"imported": added, "verified": sum(1 for x in results if x["verified"]),
            "queued": sum(1 for x in results if x["method"] is None), "results": results, "skipped": skipped,
            "skip_counts": dict(reasons)}


def cmd_import(args):
    import verify
    companies.require()
    conn = state.connect()
    run_id = state.new_run(conn, "import", 0)
    seed_state(conn)
    verify.purge_blocked(conn)
    state.purge_poisoned_domains(conn)
    res = import_csv(conn, run_id, args.csv, allow_smtp=not args.no_smtp)
    state.export_csv(conn)
    out = {"ok": True, "run_id": run_id, "imported": res["imported"], "verified": res["verified"]}
    if args.compact:
        return emit({**out, "queued": res["queued"], "skipped": res["skip_counts"]})
    return emit({**out, "results": res["results"], "skipped": res["skipped"]})


def other_employer(title, reg):
    """True when the headline names, after 'at' or '@', an employer that is not this registry company."""
    labels = {companies.norm(x) for x in (reg["key"], reg["name"], reg["linkedin_name"], *reg["aliases"]) if x}
    labels.discard("")
    for m in EMPLOYER_RE.finditer(title or ""):
        seg = companies.norm(m.group(1))
        if seg and not any(seg.startswith(label) for label in labels):
            return True
    return False


def linkedin_row(r, known_ids, known_people):
    keys = r.keys()
    g = lambda k: r[k] if k in keys else None
    key = (g("company_key") or "").strip().lower()
    reg = companies.REGISTRY.get(key)
    if not reg:
        return ("excluded_company" if key in companies.EXCLUDED_REGISTRY else "unknown_company" if key else "no_company_key"), None
    # an unconfirmed employer (headline did not say 'at <company>') is still imported, as source
    # 'linkedin_unconfirmed': sending.py only sends it once SMTP or the hash confirms a mailbox at the company domain
    confirmed = g("company_verified") in (1, "1")
    if not confirmed and other_employer(g("title"), reg):
        return "other_employer", None
    if g("replied_at") or (g("status") or "") in ("replied", "developing"):
        return "linkedin_replied", None
    if companies.excluded_company(reg["name"]) or companies.excluded_company(g("company") or ""):
        return "excluded_company", None
    name = " ".join((g("name") or "").split())
    if not resolver.normalize(name):
        return "no_name", None
    if companies.excluded_person(name):
        return "excluded_person", None
    slug = (g("linkedin_id") or "").strip() or slug_from_url(g("profile_url"))
    pid = re.sub(r"[^a-z0-9-]+", "-", slug.lower()).strip("-")[:64]
    if not pid:
        return "no_linkedin_id", None
    if slug.lower() in known_ids or (companies.person_key(name), reg["key"]) in known_people:
        return "duplicate", None
    first, last = split_person(name, g("first_name"), g("last_name"))
    if not first:
        return "no_name", None
    location = g("location") or ""
    country = normalize_country(g("country"), location)
    city = (g("city") or "").strip() or (location.split(",")[0].strip() if "," in location else "")
    return None, {"prospect_id": "li-" + pid, "business_id": "", "company": reg["name"], "tier": reg["tier"],
                  "full_name": name, "first_name": first, "last_name": last, "middle_name": None,
                  "job_title": clean_headline(g("title")), "city": city, "country": country,
                  "pass": 2 if country and country != "India" else 1,  # unknown location is not flagged as relocation
                  "domain": reg["email_domains"][0] if reg["email_domains"] else "", "email_hash": "",
                  "company_name_api": g("company") or "", "source": "linkedin" if confirmed else "linkedin_unconfirmed",
                  "origin": g("source") or "linkedin", "linkedin_id": slug,
                  "profile_url": g("profile_url") or ""}


def take_linkedin(conn, run_id, r, known_ids, known_people):
    why, row = linkedin_row(r, known_ids, known_people)
    if not why and not state.add_prospect(conn, row, run_id):
        why = "duplicate"
    if not why:
        known_ids.add(row["linkedin_id"].lower())
        known_people.add((companies.person_key(row["full_name"]), companies.company_key(row["company"])))
    return why


def drop_linkedin_replied(conn, run_id, replied):
    """LinkedIn replies after import: an unsent prospect is excluded and a pending follow-up is cancelled."""
    unsent = ("discovered", "unverified", "enriched", "resolved", "composed", "no_email", "send_failed")
    rows = conn.execute(f"""SELECT prospect_id, linkedin_id, status FROM prospects WHERE source LIKE 'linkedin%'
                            AND (status IN ({','.join('?' * len(unsent))})
                                 OR (status='sent' AND followup_due IS NOT NULL AND followup_sent_at IS NULL))""",
                        unsent).fetchall() if replied else []
    n = 0
    for r in rows:
        if (r["linkedin_id"] or "").strip().lower() not in replied:
            continue
        if r["status"] == "sent":
            state.update(conn, r["prospect_id"], followup_due=None, error="replied on LinkedIn: follow-up cancelled")
        else:
            state.update(conn, r["prospect_id"], status="excluded", error="linkedin_replied", raw=None, composed_run=None)
        state.event(conn, run_id, r["prospect_id"], "linkedin_replied", r["status"])
        n += 1
    return n


def import_linkedin(conn, run_id, db_path=None, limit=None):
    path = Path(db_path or config.LINKEDIN_DB).expanduser()
    if not path.exists():
        msg = f"LinkedIn DB not found: {path}"
        return {"ok": False, "error": msg, "reason": msg}
    src = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=30)
    src.row_factory = sqlite3.Row
    keys = sorted(companies.REGISTRY)
    try:
        version = src.execute("PRAGMA user_version").fetchone()[0]
        if version < 1:
            msg = (f"LinkedIn DB {path} is at schema version {version}; run the updated linkedin_outreach.py once "
                   "so it migrates (user_version >= 1 is required)")
            return {"ok": False, "error": msg, "reason": msg}
        mark_key = f"linkedin_watermark:{path.resolve()}"
        mark = int(state.kv_get(conn, mark_key, 0) or 0)
        sql = "SELECT * FROM outreach WHERE id > ? ORDER BY id" + (" LIMIT ?" if limit else "")
        rows = src.execute(sql, (mark, limit) if limit else (mark,)).fetchall()
        # the LinkedIn writer fills company_key and raises company_verified on existing ids, below the watermark;
        # unconfirmed rows below it were skipped by older builds, so they are picked up here too (duplicates are cheap)
        rescan = [] if limit or not mark or not keys else src.execute(
            f"""SELECT * FROM outreach WHERE id <= ?
                AND lower(trim(company_key)) IN ({','.join('?' * len(keys))}) ORDER BY id""", (mark, *keys)).fetchall()
        replied = {(r[0] or "").strip().lower() or slug_from_url(r[1]).lower() for r in src.execute(
            "SELECT linkedin_id, profile_url FROM outreach WHERE COALESCE(replied_at,'')!='' OR status IN ('replied','developing')")}
    finally:
        src.close()
    replied.discard("")
    known_ids = {(r[0] or "").lower() for r in conn.execute("SELECT linkedin_id FROM prospects WHERE linkedin_id IS NOT NULL")}
    known_people, skipped, imported = person_keys(conn), Counter(), 0
    with state.write_txn(conn):
        for r in rows:
            why = take_linkedin(conn, run_id, r, known_ids, known_people)
            if why:
                skipped[why] += 1
            else:
                imported += 1
        recovered = sum(1 for r in rescan if not take_linkedin(conn, run_id, r, known_ids, known_people))
        dropped = drop_linkedin_replied(conn, run_id, replied)
        if rows:
            mark = rows[-1]["id"]
            state.kv_set(conn, mark_key, mark)
    return {"ok": True, "read": len(rows), "imported": imported + recovered, "skipped": dict(skipped), "watermark": mark,
            "rescanned": len(rescan), "recovered": recovered, "replied_excluded": dropped}


def cmd_import_linkedin(args):
    companies.require()
    conn = state.connect()
    run_id = state.new_run(conn, "import-linkedin", args.limit or 0)
    res = import_linkedin(conn, run_id, args.db, args.limit)
    state.set_run(conn, run_id, status="done" if res["ok"] else "failed",
                  note=res.get("error") or f"read {res['read']}, imported {res['imported']}")
    return emit(res)


def osint_targets(conn):
    fresh, failed, domains = state.ago(days=90), state.ago(hours=config.REVERIFY_AFTER_HOURS), []
    for r in state.prospects(conn, ["discovered", "unverified"]):
        d = (r["domain"] or "").lower()
        if not d or d in domains or not sending.target_ok(r) or sending.block_reason(r):
            continue
        if not conn.execute("""SELECT 1 FROM patterns WHERE domain=? AND ((source='osint' AND updated_at > ?)
                               OR (source='osint_unavailable' AND updated_at > ?))""", (d, fresh, failed)).fetchone():
            domains.append(d)
    return domains


def run_osint(conn, run_id, domains, stats, deadline=None):
    import osint
    base, results = config.OSINT_TIMEOUT_S, []
    try:
        for d in domains:
            if deadline:
                left = deadline - time.monotonic()
                if left < 30:
                    break
                config.OSINT_TIMEOUT_S = int(min(base, max(10, (left - 5) / 2)))
            results.append(osint.discover_patterns(conn, d, run_id))
            stats["osint_domains"] += 1
    finally:
        config.OSINT_TIMEOUT_S = base
    return results


def cmd_osint(args):
    if not args.domain:
        companies.require()
    conn = state.connect()
    run_id = state.new_run(conn, "osint", 0)
    domains = [args.domain.lower()] if args.domain else osint_targets(conn)[:args.max_domains]
    results = run_osint(conn, run_id, domains, new_stats())
    state.export_patterns_csv(conn)
    out = {"ok": True, "run_id": run_id, "domains": len(results),
           "tools": {"theHarvester": (config.TOOLS_BIN / "theHarvester").exists(), "emailfinder": (config.TOOLS_BIN / "emailfinder").exists(),
                     "reacher": config.REACHER_BIN.exists()}}
    if args.compact:
        return emit({**out, "with_pattern": sum(1 for r in results if r["dominant_shape"])})
    return emit({**out, "results": results})


def intake_rows(conn):
    cutoff = state.ago(hours=config.REVERIFY_AFTER_HOURS)
    stale = [r for r in state.prospects(conn, ["unverified"], order="pass, updated_at") if (r["updated_at"] or "") < cutoff]
    # with pattern sends off, a 'resolved' pattern guess can never be sent: re-check it so SMTP can confirm or drop it
    held = [] if config.UNVERIFIED_PATTERN_SENDS else [
        r for r in state.prospects(conn, ["resolved"], order="updated_at")
        if r["resolve_method"] == "pattern_unverified" and (r["updated_at"] or "") < cutoff]
    return state.prospects(conn, ["discovered"]) + held + stale


def cmd_intake(args):
    import verify
    companies.require()
    t0 = time.monotonic()
    budget = max(60, args.time_budget)
    stop_at = t0 + budget - 10
    config.SMTP_MAX_PROBES_PER_RUN = max(0, args.probe_budget)
    conn = state.connect()
    run_id = state.new_run(conn, "intake", 0)
    stats, imported, linkedin, sheet, smtp_ok, capability = new_stats(), 0, None, "no file", False, None
    armed = hasattr(signal, "setitimer")
    if armed:
        previous = signal.signal(signal.SIGALRM, _deadline)
        signal.setitimer(signal.ITIMER_REAL, max(1.0, stop_at - time.monotonic()))
    try:
        smtp_ok, capability = smtp_usable(verify)
        seed_state(conn)
        if state.kv_get(conn, "intake_purged_on") != state.today():
            verify.purge_blocked(conn)
            state.kv_set(conn, "intake_purged_on", state.today())
        state.purge_poisoned_domains(conn)
        try:
            linkedin = import_linkedin(conn, run_id)
        except sqlite3.Error as e:
            linkedin = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        imported += linkedin.get("imported", 0)
        path = Path(args.sheet_csv) if args.sheet_csv else None
        if path and path.exists():
            res = import_csv(conn, run_id, path, resolve=False)
            imported += res["imported"]
            stats["verified"] += res["verified"]
            sheet = {"imported": res["imported"], "verified": res["verified"], "skipped": res["skip_counts"]}
        if config.OSINT_ENABLED and args.osint_domains > 0:
            run_osint(conn, run_id, osint_targets(conn)[:args.osint_domains], stats, deadline=t0 + budget * 0.4)
        reverify_rows(conn, run_id, intake_rows(conn), smtp_ok, stats, deadline=stop_at - 20)
    except Deadline:
        stats["stopped"] = "time budget"
    finally:
        if armed:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous)
    state.export_csv(conn)
    try:
        import supply
        s = supply.record(conn)
        supply_out = {k: s[k] for k in ("as_of", "forecast_1d", "forecast_3d", "capacity_1d", "supply_gap", "cap_gap")}
    except Exception as e:  # the forecast is a report: intake must not fail over it
        log(f"SUPPLY record failed: {e}\n{traceback.format_exc()}")
        supply_out = {"error": f"{type(e).__name__}: {e}"}
    elapsed = round(time.monotonic() - t0, 1)
    state.set_run(conn, run_id, status="done", note=f"imported {imported}, checked {stats['checked']}, "
                  f"verified {stats['verified']}, {elapsed}s, stopped: {stats['stopped']}")
    li_ok = bool(linkedin and linkedin.get("ok"))
    return emit({"ok": li_ok, "run_id": run_id, "imported": imported, "verified": stats["verified"],
                 "unverified": stats["unverified"], "probes_used": getattr(verify, "_probes_this_run", None),
                 "elapsed_s": elapsed, "backlog_ready": sending.backlog_ready(conn), "checked": stats["checked"],
                 "osint_domains": stats["osint_domains"], "stopped": stats["stopped"], "smtp": smtp_ok,
                 "verification": capability, "linkedin": linkedin, "sheet": sheet, "supply": supply_out,
                 **({} if li_ok else {"error": f"LinkedIn import: {(linkedin or {}).get('error') or 'did not complete'}"})})


MX_PROVIDERS = (("google", "google"), ("outlook", "microsoft"), ("protection.office", "microsoft"),
                ("pphosted", "proofpoint"), ("mimecast", "mimecast"), ("barracuda", "barracuda"))


def mx_provider(mx):
    host = (mx or "").lower()
    return next((name for marker, name in MX_PROVIDERS if marker in host), "own" if host else "-")


def read_candidates(path):
    """[(name, domain, tier, india_share)] from a CSV with columns name, domain[, tier, india_share]."""
    with open(path, newline="", encoding="utf-8-sig") as f:
        rows = [{k.strip().lower(): (v or "").strip() for k, v in r.items() if k} for r in csv.DictReader(f)]
    return [(r["name"], r["domain"].lower().lstrip("@"), (r.get("tier") or "B").upper(), r.get("india_share") or "0.8")
            for r in rows if r.get("name") and r.get("domain")]


def cmd_profile_domains(args):
    """Measure which companies' mail the free SMTP check can verify (checkable / catch_all / blocked / no_mx), with
    the cap totals per class against what the daily supply target needs. --candidates screens companies that are
    not in the registry and prints CSV rows to paste after review; nothing here edits the registry."""
    import supply
    import verify
    companies.require()
    problems = companies.lint()
    if args.lint_only:
        return emit({"ok": not problems, "lint": problems})
    config.SMTP_MAX_PROBES_PER_RUN = max(0, args.probe_budget)
    conn = state.connect()
    only = {d.strip().lower() for d in (args.domains or "").split(",") if d.strip()}
    cands = read_candidates(args.candidates) if args.candidates else []
    todo = [(None, d) for d in only] if only else (
        [(c, c[1]) for c in cands] if cands else [(reg, d) for reg in companies.REGISTRY.values() for d in reg["email_domains"]])
    seen = {}
    for owner, d in todo:
        if d in seen:
            continue
        try:
            seen[d] = verify.domain_profile(conn, d, force=args.force)
        except Exception as e:
            seen[d] = {"domain": d, "port25_ok": None, "note": f"{type(e).__name__}: {e}"}
    profs = supply.profiles(conn)
    if cands:
        out = []
        for name, d, tier, share in cands:
            p = profs.get(d) or seen.get(d) or {}
            cls = supply.domain_class(p)
            key = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
            row = (f"{key},{name},{tier},{name},,{d},{mx_provider(p.get('mx'))},yes,,"
                   f"{3 if tier == 'A' else 2},{60 if tier == 'A' else 30},{share},,0,1," if cls == "checkable" else None)
            out.append({"name": name, "domain": d, "class": cls, "mx": p.get("mx"), "note": (p.get("note") or "")[:90],
                        "in_registry": companies.lookup(name) is not None, "csv_row": row})
        keep = [c["csv_row"] for c in out if c["csv_row"] and not c["in_registry"]]
        return emit({"ok": True, "candidates": out, "checkable": len(keep), "csv_rows": keep})
    target = config.SUPPLY_TARGET
    rows, totals = [], {}
    for reg in companies.REGISTRY.values():
        cls = supply.company_class(reg, profs)
        mx = next((profs[d].get("mx") for d in reg["email_domains"] if profs.get(d)), None)
        rows.append({"key": reg["key"], "tier": reg["tier"], "class": cls, "domains": reg["email_domains"],
                     "mx": mx_provider(mx), "csv_says": reg["verifiable"] or "-", "daily_cap": reg["daily_cap"],
                     "monthly_cap": reg["monthly_cap"], "harvest": reg["harvest"]})
        t = totals.setdefault(cls, {"companies": 0, "daily_cap": 0, "monthly_cap": 0})
        t["companies"] += 1
        t["daily_cap"] += reg["daily_cap"]
        t["monthly_cap"] += reg["monthly_cap"]
    ok = totals.get("checkable", {"daily_cap": 0, "monthly_cap": 0})
    # the batch never fills every company's cap, so the checkable caps need headroom over the target itself
    need_month, need_day = math.ceil(target * 30 / 0.75), math.ceil(target * 1.6)
    if args.compact:
        rows = [{k: r[k] for k in ("key", "class", "mx")} for r in rows]
    return emit({"ok": True, "lint": problems, "companies": rows, "totals": totals, "target_per_day": target,
                 "checkable_caps": ok, "need": {"monthly_cap": need_month, "daily_cap": need_day},
                 "cap_gap": {"monthly": max(0, need_month - ok["monthly_cap"]), "daily": max(0, need_day - ok["daily_cap"])},
                 "probes_used": getattr(verify, "_probes_this_run", None)})


def cmd_supply(args):
    import supply
    companies.require()
    conn = state.connect()
    if args.record:
        data = supply.record(conn)
        snap = {"summary": {k: v for k, v in data.items() if k not in ("funnel", "rates")}, "funnel": data["funnel"],
                "rates": data["rates"]}
    else:
        snap = supply.snapshot(conn)
        snap["funnel"] = supply.funnel(conn, state.today(), supply.profiles(conn))
    if args.compact:
        return emit({"ok": True, **snap["summary"], "line": supply.describe(snap["summary"])})
    return emit({"ok": True, "line": supply.describe(snap["summary"]), **snap})


def cmd_sheet_status(args):
    conn = state.connect()
    rows = conn.execute("""SELECT prospect_id, source, full_name, company, status, email, email_guess, confidence, resolve_method,
                                  verify_note, error, updated_at FROM prospects WHERE source='sheet' OR prospect_id LIKE 'import-%'
                                  ORDER BY updated_at DESC LIMIT ?""", (SHEET_ROWS_MAX,)).fetchall()
    out = []
    for r in rows:
        out.append({"prospect_id": r["prospect_id"], "source": r["source"] or "sheet",
                    "name": r["full_name"], "company": r["company"], "status": r["status"],
                    "email": r["email"] or (f"(guess) {r['email_guess']}" if r["email_guess"] else ""),
                    "confidence": r["confidence"] if r["confidence"] is not None else "",
                    "method": r["resolve_method"] or "", "note": (r["verify_note"] or r["error"] or "")[:200],
                    "updated_at": r["updated_at"]})
    return emit({"ok": True, "count": len(out), "rows": out})


def cmd_enrich(args):
    conn = state.connect()
    run_id = args.run_id or state.new_run(conn, "enrich", args.limit)
    used, room = room_today(conn, args.limit)
    pending = state.prospects(conn, list(state.PENDING), tier=args.tier)
    base = {"run_id": run_id, "sends_today": used, "room_today": room, "pending_ready": len(pending)}
    if not (config.ALLOW_PAID_REVEAL and config.EXPLORIUM_ENABLED):
        return emit({**base, "ok": True, "enriched": 0, "credits_used": 0, "remaining_credits": "n/a",
                     "note": "paid reveal disabled (OUTREACH_EXPLORIUM / OUTREACH_ALLOW_PAID_REVEAL); unverified contacts stay queued"})
    n = max(0, room - len(pending))
    candidates = state.prospects(conn, ["discovered", "unverified"], tier=args.tier)[:n]
    if not candidates:
        return emit({**base, "ok": True, "enriched": 0, "credits_used": 0, "note": "nothing to enrich"})
    est = len(candidates) * config.CREDITS_PER_CONTACT
    if est > args.max_credits:
        return emit({**base, "ok": False, "error": f"estimated {est} credits exceeds cap {args.max_credits}", "stage": "cap"})
    try:
        client = Explorium(config.API_KEY, log=log, allow_billing=True)
        remaining = client.credits()["remaining"]
    except ExploriumError as e:
        return emit({**base, "ok": False, "error": str(e), "stage": "credits"})
    if remaining - est < config.CREDIT_RESERVE:
        return emit({**base, "ok": False, "stage": "reserve",
                     "error": f"remaining {remaining} minus {est} would breach reserve {config.CREDIT_RESERVE}"})
    ids = [r["prospect_id"] for r in candidates]
    by_id = {r["prospect_id"]: r for r in candidates}
    results, rejected, errors, learned = [], [], [], 0
    for i in range(0, len(ids), config.ENRICH_BATCH):
        batch = ids[i:i + config.ENRICH_BATCH]
        try:
            rows = client.bulk_enrich_contacts(batch)
        except ExploriumError as e:
            errors.append(str(e))
            log(f"enrich batch failed: {e}")
            break
        seen = set()
        for row in rows:
            pid = row["prospect_id"]
            if pid not in by_id:
                continue
            seen.add(pid)
            p = by_id[pid]
            addr, etype, mx, rej = mailer.pick_email(row["emails"], row["professional_email_status"])
            if addr:
                state.update(conn, pid, status="enriched", email=addr, email_type=etype, confidence=1.0,
                             email_status=row["professional_email_status"], mx_ok=mx, enriched_at=state.now(),
                             resolve_method="explorium")
                state.event(conn, run_id, pid, "enriched", f"{addr} {etype} {row['professional_email_status']}")
                pattern = resolver.infer_pattern(p["full_name"], addr, p["first_name"], p["last_name"])
                if pattern:
                    state.upsert_pattern(conn, addr.split("@", 1)[1], pattern, addr, "explorium", "enrich")
                    learned += 1
                results.append({**dict(p), "email": addr, "email_type": etype,
                                "email_status": row["professional_email_status"], "mx_ok": mx})
            else:
                why = "; ".join(f"{a}:{v}" for a, v in rej) or "no email returned"
                state.update(conn, pid, status="no_email", error=why, enriched_at=state.now())
                state.event(conn, run_id, pid, "no_email", why)
                rejected.append({"prospect_id": pid, "company": p["company"], "reason": why})
        for pid in batch:
            if pid not in seen:
                state.update(conn, pid, status="no_email", error="not returned by enrichment", enriched_at=state.now())
                rejected.append({"prospect_id": pid, "company": by_id[pid]["company"], "reason": "not returned"})
    credits_used = client.credits_used or (len(results) * config.CREDITS_PER_CONTACT)
    state.add_run_credits(conn, run_id, credits_used)
    state.export_csv(conn)
    state.export_patterns_csv(conn)
    slim = [{k: r[k] for k in ("prospect_id", "full_name", "company", "tier", "job_title", "city", "country",
                                "pass", "email", "email_type", "email_status", "mx_ok")} for r in results]
    return emit({**base, "ok": not errors, "enriched": len(results), "no_email": len(rejected), "patterns_learned": learned,
                 "credits_used": credits_used, "remaining_credits": remaining - credits_used,
                 "contacts": slim, "rejected": rejected, "errors": errors})


def main(argv=None):
    ap = argparse.ArgumentParser(prog="pipeline.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("preflight")
    sub.add_parser("seed")
    d = sub.add_parser("discover")
    d.add_argument("--limit", type=int, default=config.MAX_PER_DAY)
    d.add_argument("--tier", choices=["A", "B", "C"])
    d.add_argument("--max-companies", type=int, default=config.DISCOVER_MAX_COMPANIES)
    d.add_argument("--time-budget", type=int, default=config.DISCOVER_TIME_BUDGET_S)
    d.add_argument("--no-smtp", action="store_true")
    rv = sub.add_parser("reverify")
    rv.add_argument("--prospect-id")
    rv.add_argument("--no-smtp", action="store_true")
    rv.add_argument("--compact", action="store_true")
    rs = sub.add_parser("resolve")
    rs.add_argument("--name", required=True)
    rs.add_argument("--domain", required=True)
    rs.add_argument("--hash")
    rs.add_argument("--no-smtp", action="store_true")
    im = sub.add_parser("import")
    im.add_argument("--csv", required=True, help="a CSV file or a directory of CSV files")
    im.add_argument("--no-smtp", action="store_true")
    im.add_argument("--compact", action="store_true")
    os_ = sub.add_parser("osint")
    os_.add_argument("--domain")
    os_.add_argument("--max-domains", type=int, default=10)
    os_.add_argument("--compact", action="store_true")
    il = sub.add_parser("import-linkedin")
    il.add_argument("--db")
    il.add_argument("--limit", type=int)
    it = sub.add_parser("intake")
    it.add_argument("--time-budget", type=int, default=480)
    it.add_argument("--probe-budget", type=int, default=60)
    it.add_argument("--sheet-csv")
    it.add_argument("--osint-domains", type=int, default=3)
    sub.add_parser("sheet-status")
    pd_ = sub.add_parser("profile-domains")
    pd_.add_argument("--force", action="store_true", help="re-probe domains even if a fresh profile is cached")
    pd_.add_argument("--domains", help="comma-separated domains instead of the whole registry")
    pd_.add_argument("--candidates", help="CSV of name,domain[,tier,india_share] to screen for the registry")
    pd_.add_argument("--probe-budget", type=int, default=120)
    pd_.add_argument("--lint-only", action="store_true")
    pd_.add_argument("--compact", action="store_true")
    sp = sub.add_parser("supply")
    sp.add_argument("--record", action="store_true", help="store it (company_supply, supply_daily, kv) as intake does")
    sp.add_argument("--compact", action="store_true")
    e = sub.add_parser("enrich")
    e.add_argument("--run-id")
    e.add_argument("--limit", type=int, default=config.MAX_PER_DAY)
    e.add_argument("--tier", choices=["A", "B", "C"])
    e.add_argument("--max-credits", type=int, default=config.MAX_CREDITS_PER_RUN)
    sending.register(sub)
    args = ap.parse_args(argv)
    handlers = {"preflight": cmd_preflight, "seed": cmd_seed, "discover": cmd_discover, "reverify": cmd_reverify,
                "resolve": cmd_resolve, "import": cmd_import, "osint": cmd_osint, "sheet-status": cmd_sheet_status,
                "enrich": cmd_enrich, "import-linkedin": cmd_import_linkedin, "intake": cmd_intake,
                "profile-domains": cmd_profile_domains, "supply": cmd_supply, **sending.HANDLERS}
    try:
        return handlers[args.cmd](args)
    except companies.RegistryError as e:
        log(f"REGISTRY {args.cmd}: {e}")
        print(json.dumps({"ok": False, "error": str(e), "stage": "registry", "cmd": args.cmd}))
        return 2
    except (Exception, Deadline) as e:
        log(f"CRASH {args.cmd}: {e}\n{traceback.format_exc()}")
        print(json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}", "stage": "crash", "cmd": args.cmd}))
        return 2


if __name__ == "__main__":
    sys.exit(main())
