"""Email resolution engine: name + domain -> verified address, without paid reveals.
Order of evidence: Explorium hash match (free) > SMTP mailbox accepted on a non-catch-all domain >
SMTP accepted on a catch-all domain backed by a confirmed pattern > pattern guess (never sent, except through the
opt-in OUTREACH_UNVERIFIED_PATTERN_SENDS for a strong learned pattern that SMTP could not check).
The company's other registry mail domains are probed too, within the per-contact probe cap, when the first cannot
be verified."""
import re
from collections import Counter

import config
import mailer
import resolver
import state
import verify

CONFIDENCE = {
    "hash_verified": 1.0,
    "smtp_valid": 0.9,
    "smtp_valid_initial": 0.6,
    "pattern_unverified": 0.8,
    "smtp_catch_all_osint_pattern": 0.7,
    "smtp_catch_all_delivered_pattern": 0.6,
    "smtp_catch_all": 0.4,
    "pattern_delivered": 0.5,
    "pattern_seed": 0.3,
    "guess": 0.2,
}
PATTERN_SEND_EVIDENCE = ("delivered", "explorium", "hash_verified", "smtp_verified", "osint")
ALT_DOMAIN_STATES = ("no_mx", "blocked", "catch_all", "exhausted")
PATTERN_SEND_STATES = ("blocked", "unknown", "catch_all", "skipped")
# formats that drop the surname (or the first name): an accepted mailbox may belong to a namesake
NAMESAKE_PRONE = ("first", "last", "first+linitial", "first.linitial", "linitial+first", "linitial.first")


def pattern_evidence(conn, domain):
    rows = conn.execute("SELECT pattern, outcome FROM patterns WHERE domain=?", (domain,)).fetchall()
    strong = {r["pattern"] for r in rows if r["outcome"] in ("delivered", "explorium", "hash_verified", "smtp_verified")}
    osint = {r["pattern"] for r in rows if r["outcome"] == "osint"}
    weak = {r["pattern"] for r in rows if r["outcome"] in ("seed", "bounced", "osint_weak")}
    return strong, weak | osint, osint


def usable(cands):
    return [(e, p) for e, p in cands if mailer.classify_email(e, "professional", None).startswith("ok")]


def global_prior(conn):
    """Counter pattern -> net evidence across every domain (verified or delivered +1, bounced -1). Orders the patterns
    a domain has no evidence for, so the formats that actually work elsewhere are probed before the long tail."""
    score = Counter()
    for r in conn.execute("SELECT pattern, outcome FROM patterns WHERE outcome IN "
                          "('delivered','hash_verified','smtp_verified','explorium','bounced')"):
        score[r["pattern"]] += -1 if r["outcome"] == "bounced" else 1
    return score


def profile_state(profile):
    note = profile.get("note") or ""
    if profile.get("port25_ok") == 0:
        return "blocked" if profile.get("mx") else "no_mx"
    return "throttled" if note.startswith("throttled") else "budget" if verify.BUDGET_MSG in note else "unknown"


def names_contact(email, pattern, surname, established):
    """True when an accepted mailbox identifies this contact rather than a namesake at the same employer: it spells
    their full surname (and is not the bare 'last'), or it uses a first-name/surname-initial format this domain is
    known to use (e.g. one that uses lastfirst-initial or first+linitial)."""
    if pattern in NAMESAKE_PRONE:
        return pattern in established
    return surname in re.sub(r"[^a-z]", "", email.split("@", 1)[0].lower())


def probe_domain(conn, out, domain, cands, profile, preferred, evidence, limit, tried, surname=""):
    """Probe up to `limit` untried candidates at one domain: (hit, state, note), hit = (email, pattern, method, conf,
    rec). Order: this domain's evidence, then the candidates' own order, with formats that drop the surname last
    unless the domain is known to use them. With an initial-only or missing surname an accepted mailbox is too
    ambiguous to send (a different person may own it at a large employer), so a plain smtp_valid hit is capped below
    MIN_CONFIDENCE; with a full surname an accepted address that does not identify the contact (john@, jp@) is kept
    only as a fallback and probing goes on."""
    strong, weak, osint = evidence
    if not profile.get("port25_ok"):
        return None, profile_state(profile), f"port 25 unusable for {domain}: {(profile.get('note') or '')[:80]}"
    established = {preferred} | strong | osint

    def rank(c):
        if c[1] in established:
            return (0 if c[1] == preferred else 1 if c[1] in strong else 2, 0)
        return (3 + (c[1] in NAMESAKE_PRONE), 0 if c[1] in weak else 1)
    ordered = sorted(cands, key=rank)
    # skip addresses already rejected so a re-check probes the next patterns instead of re-reading the cache
    ordered = [c for c in ordered if tried.get(c[0]) != "invalid" and not known_invalid(conn, c[0])]
    namesake = None
    for email, pattern in ordered[:limit]:
        rec = verify.verify_email(conn, email, profile)
        out["probes"] += 1
        tried[email] = rec["result"]
        message = (rec.get("message") or "")[:80]
        if rec["result"] == "valid":
            if not profile.get("catch_all"):
                if not surname:
                    return (email, pattern, "smtp_valid", CONFIDENCE["smtp_valid_initial"], rec), "valid", None
                if names_contact(email, pattern, surname, established):
                    return (email, pattern, "smtp_valid", CONFIDENCE["smtp_valid"], rec), "valid", None
                namesake = namesake or (email, pattern, "smtp_valid", CONFIDENCE["smtp_valid_initial"], rec)
                continue
            if pattern in osint:
                conf = CONFIDENCE["smtp_catch_all_osint_pattern"]
            elif pattern in strong:
                conf = CONFIDENCE["smtp_catch_all_delivered_pattern"]
            else:
                conf = CONFIDENCE["smtp_catch_all"]
            return (email, pattern, "smtp_catch_all", conf, rec), "catch_all", None
        if rec["result"] == "blocked":
            return None, "blocked", f"mail server rejects our IP by policy, cannot verify: {message}"
        if rec["result"] == "throttled":
            return None, "throttled", f"mail server throttled us, retry next run: {message}"
        if rec["result"] == "unknown" and rec.get("code") is None:
            why = "budget" if verify.BUDGET_MSG in (rec.get("message") or "") else "unknown"
            return None, why, f"smtp unreachable: {message}"
    if namesake:
        return namesake, "valid", f"{namesake[0]} exists but does not name this contact (namesake risk), not sendable"
    return None, "exhausted", None


def strong_patterns(conn, domain):
    """[(pattern, outcome)] backed by delivery, verification or OSINT evidence and not bounced since, best first."""
    support, bounced = {}, {}
    for r in conn.execute("SELECT pattern, outcome, updated_at FROM patterns WHERE domain=?", (domain,)):
        when = r["updated_at"] or ""
        if r["outcome"] == "bounced":
            bounced[r["pattern"]] = max(bounced.get(r["pattern"], ""), when)
        elif r["outcome"] in PATTERN_SEND_EVIDENCE:
            score, newest, outcome = support.get(r["pattern"], (-1, "", ""))
            gain = resolver.OUTCOME_SCORE.get(r["outcome"], 0)
            support[r["pattern"]] = (max(score, gain), max(newest, when), r["outcome"] if gain > score else outcome)
    ranked = sorted(((s, t, p, o) for p, (s, t, o) in support.items() if bounced.get(p, "") <= t), reverse=True)
    return [(p, o) for _, _, p, o in ranked]


def known_invalid(conn, email):
    row = conn.execute("SELECT result FROM verifications WHERE email=?", (email,)).fetchone()
    return bool(row) and row["result"] == "invalid"


def pattern_send(conn, row, domains, states, tried):
    """The opt-in unverified send: (domain, email, pattern, outcome) or None."""
    if not getattr(config, "UNVERIFIED_PATTERN_SENDS", False) or any(s in ("throttled", "budget") for s in states.values()):
        return None
    full, first_name, last_name = row["full_name"], row.get("first_name"), row.get("last_name")
    surname = resolver.split_person_name(full, first_name, last_name)["last"]
    common = getattr(config, "COMMON_SURNAMES", None)
    bounced = getattr(state, "domain_bounced_recently", None)
    if len(surname) < 2 or common is None or surname in common or bounced is None:
        return None
    first, middle, last = resolver.split_name(full, first_name, last_name)
    days = getattr(config, "PATTERN_BREAKER_DAYS", 14)
    for d in domains:
        if states.get(d) not in PATTERN_SEND_STATES or bounced(conn, d, days) or not verify.mx_hosts(d):
            continue
        for pattern, outcome in strong_patterns(conn, d):
            local = resolver.apply_pattern(pattern, first, middle, last) if pattern != "first" else None
            email = f"{local}@{d}" if local and len(local) >= 2 else ""
            if email and usable([(email, pattern)]) and tried.get(email) != "invalid" and not known_invalid(conn, email):
                return d, email, pattern, outcome
    return None


def resolve_contact(conn, run_id, row, allow_smtp=True):
    domains = [d.lower() for d in (row.get("domains") or []) if d] or [(row.get("domain") or "").lower()]
    domains = [d for d in dict.fromkeys(domains) if d]
    out = {"email": None, "guess": None, "method": None, "pattern": None, "confidence": 0.0, "verified": False,
           "catch_all": None, "probes": 0, "note": "", "domain": domains[0] if domains else ""}
    if not domains:
        out["note"] = "no company domain"
        return out
    domain = domains[0]
    full, first, last = row["full_name"], row.get("first_name"), row.get("last_name")
    surname = resolver.split_person_name(full, first, last)["last"]
    learn = bool(surname)
    preferred = state.best_pattern(conn, domain, resolver.OUTCOME_SCORE)
    strong, weak, osint = pattern_evidence(conn, domain)
    prior = global_prior(conn)
    cands = usable(resolver.candidates(full, domain, first, last, preferred, prior=prior))
    if cands:
        out["guess"], out["pattern"] = cands[0]

    hashed = row.get("email_hash") or ""
    if hashed:
        for alt in domains:
            alt_pref = preferred if alt == domain else state.best_pattern(conn, alt, resolver.OUTCOME_SCORE)
            for email, pattern in usable(resolver.candidates(full, alt, first, last, alt_pref, safe=False)):
                if resolver.hash_matches(email, hashed):
                    out["domain"] = alt
                    return finish(conn, run_id, out, email, pattern, "hash_verified", CONFIDENCE["hash_verified"], None, learn)
    if not cands:
        out["note"] = "no usable candidates"
        return out

    states, tried, held = {}, {}, None
    if allow_smtp and config.SMTP_ENABLED:
        cap, previous = config.SMTP_MAX_PROBES_PER_CONTACT, None
        for d in domains:
            if d == domain:
                profile = verify.domain_profile(conn, d)
                out["catch_all"] = profile.get("catch_all")
                depth = (config.SMTP_LADDER_DEPTH_UNCONFIRMED if row.get("source") == "linkedin_unconfirmed"
                         else config.SMTP_LADDER_DEPTH)
                rejected = sum(1 for e, _ in cands if known_invalid(conn, e))
                d_cands, d_pref, evidence, limit = cands, preferred, (strong, weak, osint), min(cap, max(0, depth - rejected))
            else:
                if previous not in ALT_DOMAIN_STATES or out["probes"] >= cap:
                    break
                before = verify._probes_this_run
                profile = verify.domain_profile(conn, d)
                out["probes"] += verify._probes_this_run - before
                d_pref, evidence = state.best_pattern(conn, d, resolver.OUTCOME_SCORE), pattern_evidence(conn, d)
                d_cands = usable(resolver.candidates(full, d, first, last, d_pref, prior=prior))
                limit = max(0, cap - out["probes"])
                if limit == 0:
                    states[d] = previous = "budget"
                    continue
            hit, previous, note = probe_domain(conn, out, d, d_cands, profile, d_pref, evidence, limit, tried, surname)
            states[d] = previous
            if note:
                out["note"] = note if d == domain else f"{out['note']} | {d}: {note}".strip(" |")
            if hit and previous == "valid":
                out["domain"] = d
                return finish(conn, run_id, out, *hit, learn)
            if hit:
                if config.ALLOW_CATCH_ALL_OSINT and hit[1] in (evidence[2] | evidence[0]):
                    out["domain"] = d
                    res = finish(conn, run_id, out, *hit, learn)
                    res["verified"] = True
                    res["note"] = (res["note"] + " | accepted: catch-all + confirmed pattern").strip(" |")
                    return res
                held = held or (d, hit)
    else:
        states = dict.fromkeys(domains, "skipped")

    unverified = pattern_send(conn, row, domains, states, tried)
    if unverified:
        d, email, pattern, outcome = unverified
        out["domain"] = d
        out["note"] = f"{out['note']} | unverified: {pattern} pattern ({outcome} evidence), {d} {states[d]}".strip(" |")
        return finish(conn, run_id, out, email, pattern, "pattern_unverified", CONFIDENCE["pattern_unverified"], None, learn)
    if held:
        out["domain"] = held[0]
        return finish(conn, run_id, out, *held[1], learn)

    for email, pattern in cands:
        if pattern in strong:
            return finish(conn, run_id, out, email, pattern, "pattern_delivered", CONFIDENCE["pattern_delivered"], None, learn)
    conf = CONFIDENCE["pattern_seed"] if cands[0][1] in weak else CONFIDENCE["guess"]
    return finish(conn, run_id, out, cands[0][0], cands[0][1], "guess", conf, None, learn)


def finish(conn, run_id, out, email, pattern, method, confidence, rec, learn=True):
    out.update({"email": email, "pattern": pattern, "method": method, "confidence": confidence,
                "verified": confidence >= config.MIN_CONFIDENCE})
    if rec:
        out["note"] = f"smtp {rec.get('code')}: {str(rec.get('message', ''))[:80]}"
    domain = email.split("@", 1)[1]
    # only an identity-confirmed hit teaches the domain's format: a namesake's or an initial-surname mailbox would
    # otherwise become 'smtp_verified' evidence and steer every later contact at the domain
    if learn and out["verified"] and method in ("hash_verified", "smtp_valid"):
        state.upsert_pattern(conn, domain, pattern, email, "hash_verified" if method == "hash_verified" else "smtp_verified", "engine")
    return out


def apply_result(conn, run_id, prospect_id, res):
    if res["verified"]:
        state.update(conn, prospect_id, status="resolved", email=res["email"], email_type="professional",
                     email_status=res["method"], mx_ok=1, enriched_at=state.now(), resolve_method=res["method"],
                     resolve_pattern=res["pattern"], confidence=res["confidence"], email_guess=res["email"],
                     verify_note=res["note"], domain=res.get("domain") or None)
        state.event(conn, run_id, prospect_id, "resolved", f"{res['email']} {res['method']} conf={res['confidence']} probes={res['probes']}")
    else:
        state.update(conn, prospect_id, status="unverified", email_guess=res.get("guess") or res.get("email"),
                     resolve_method=res["method"], resolve_pattern=res["pattern"], confidence=res["confidence"],
                     verify_note=res["note"])
        state.event(conn, run_id, prospect_id, "unverified", f"{res.get('email')} {res['method']} conf={res['confidence']} {res['note']}")
    return res["verified"]
