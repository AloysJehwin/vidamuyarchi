import datetime as dt
import json
import re
from collections import Counter

import companies
import config
import mailer
import resolver
import state
import templates

RECRUITER_RE = re.compile(r"\b(recruit\w*|talent|acquisition|hiring|sourc\w*|staffing|hr|human resources|people partner)\b", re.I)
MANAGER_RE = re.compile(r"\b(manager|director|head|vp|vice president|cto|chief)\b", re.I)
DELAY_RE = re.compile(r"\(delay\)|delivery incomplete|will (?:retry|keep trying)|has been delayed|delivery delayed", re.I)
TERMINAL = {"excluded company": "excluded", "excluded person": "excluded", "already contacted": "duplicate"}
MAILBOX_CONFIRMED = ("hash_verified", "smtp_valid")


def emit(obj):
    obj.setdefault("ok", True)
    obj.setdefault("ts", state.now())
    print(json.dumps(obj, ensure_ascii=False, default=str))
    return 0


def item_view(r, to=None):
    return {"prospect_id": r["prospect_id"], "to": to or r["email"], "full_name": r["full_name"],
            "first_name": r["first_name"], "company": r["company"], "tier": r["tier"], "variant": r["variant"],
            "job_title": r["job_title"], "city": r["city"], "country": r["country"], "pass": r["pass"],
            "email_status": r["email_status"], "resolve_method": r["resolve_method"], "source": r["source"],
            "confidence": r["confidence"] if "confidence" in r.keys() else None,
            "subject": r["subject"], "body": r["body"]}


def test_recipient(args):
    return (getattr(args, "test_recipient", "") or "").strip() or (config.SELF_EMAIL if getattr(args, "test", False) else None)


def role_rank(title):
    return 0 if RECRUITER_RE.search(title or "") else 1 if MANAGER_RE.search(title or "") else 2


def from_sheet(r):
    return (r["source"] or "") in ("sheet", "manual") or r["prospect_id"].startswith(("import-", "manual-"))


def target_ok(r):
    return from_sheet(r) or companies.lookup(r["company"] or "") is not None


def block_reason(r):
    if companies.excluded_company(r["company"] or "", r["email"] or ""):
        return "excluded company"
    if companies.excluded_person(r["full_name"] or ""):
        return "excluded person"
    return None


def company_caps(r):
    reg = companies.lookup(r["company"] or "")
    return (reg["daily_cap"], reg["monthly_cap"]) if reg else (1, None)


def employer_unconfirmed(r):
    """A LinkedIn person whose employer the headline did not confirm is sent to only when a mailbox at the company
    domain matched their name: the provider hash matched, or SMTP accepted it on a non-catch-all domain and the
    surname is distinctive (a 'vkumar@' mailbox at a large employer may belong to a namesake)."""
    if (r["source"] or "") != "linkedin_unconfirmed":
        return False
    if r["resolve_method"] not in MAILBOX_CONFIRMED:
        return True
    if r["resolve_method"] == "hash_verified":
        return False
    surname = resolver.split_person_name(r["full_name"], r["first_name"], r["last_name"])["last"]
    return len(surname) < 2 or surname in config.COMMON_SURNAMES


def pattern_send_ok(conn, r):
    domain = (r["email"] or "").rpartition("@")[2]
    return config.UNVERIFIED_PATTERN_SENDS and not state.domain_bounced_recently(conn, domain, config.PATTERN_BREAKER_DAYS)


def skip_reason(conn, r, contacted):
    email = (r["email"] or "").strip().lower()
    if not email:
        return "no email"
    why = block_reason(r) or ("already contacted" if email in contacted else None)
    if why:
        return why
    if not target_ok(r):
        return "company not in registry"
    if employer_unconfirmed(r):
        return "employer unconfirmed"
    if r["resolve_method"] == "pattern_unverified" and not pattern_send_ok(conn, r):
        return "unverified pattern"
    return None


def backlog_ready(conn):
    contacted = state.contacted_emails(conn)
    return sum(1 for r in state.prospects(conn, list(state.READY)) if not skip_reason(conn, r, contacted))


def breaker_refusal(conn):
    tripped, b = state.breaker(conn)
    if not tripped:
        return None
    return (f"bounce breaker tripped: {b['last_24h']} bounces in 24 h (limit {config.BREAKER_BOUNCES_24H}), "
            f"{b['last_100']} in the last {b['window']} verified first-touch sends within {config.BREAKER_WINDOW_DAYS} days "
            f"(limit {config.BREAKER_BOUNCES_PER_100})")


def select_batch(conn, ready, counts, contacted, room):
    """Which ready rows a batch takes, without side effects: (picked rows, skipped Counter, [(row, terminal reason)]).
    India first, verified before pattern guesses, recruiters first; per-company daily and 30-day caps against
    `counts` (state.company_first_touches). `contacted` gains the picked addresses."""
    ready = sorted(ready, key=lambda r: (r["pass"] or 2, r["resolve_method"] == "pattern_unverified",
                                         role_rank(r["job_title"])))
    items, picked, skipped, terminal = [], Counter(), Counter(), []
    for r in ready:
        key = companies.company_key(r["company"] or "")
        why = skip_reason(conn, r, contacted)
        if not why and len(items) >= room:
            why = "daily room"
        if not why:
            daily, monthly = company_caps(r)
            done_today, done_month = counts.get(key, (0, 0))
            if done_today + picked[key] >= daily or (monthly is not None and done_month + picked[key] >= monthly):
                why = "company cap"
        if why:
            skipped[why] += 1
            if why in TERMINAL:
                terminal.append((r, why))
            continue
        picked[key] += 1
        contacted.add(r["email"].strip().lower())
        items.append(r)
    return items, skipped, terminal


def cmd_compose(args):
    companies.require()
    conn = state.connect()
    run_id = args.run_id or state.new_run(conn, "compose", args.limit)
    info = mailer.resume_info()
    if not info["ok"]:
        return emit({"ok": False, "run_id": run_id, "error": f"resume: {info['reason']}", "stage": "resume"})
    used = state.first_touch_sends_today(conn)
    stop = breaker_refusal(conn)
    if stop:
        state.set_run(conn, run_id, status="skipped", note=stop)
        return emit({"ok": False, "run_id": run_id, "count": 0, "items": [], "digest": "", "sends_today": used,
                     "room_today": 0, "test_mode": bool(test_recipient(args)), "stage": "breaker", "error": stop})
    ready = state.prospects(conn, list(state.READY) + ["composed"], tier=args.tier, order="pass, enriched_at")
    ids = {r["prospect_id"] for r in ready}
    elsewhere = sum(1 for r in state.prospects(conn, ["composed"]) if r["prospect_id"] not in ids)
    room = max(0, min(args.limit, config.MAX_PER_DAY - used - state.sending_today(conn) - elsewhere))
    counts = state.company_first_touches(conn, companies.company_key, exclude_ids=ids)
    items, skipped, terminal = select_batch(conn, ready, counts, state.contacted_emails(conn), room)
    for r, why in terminal:
        state.update(conn, r["prospect_id"], status=TERMINAL[why], error=why, raw=None, composed_run=None)
    test_to = test_recipient(args)
    bcc = config.SELF_EMAIL if (config.BCC_SELF and not test_to) else None
    out = []
    for i, r in enumerate(items):
        variant = r["tier"] if r["tier"] in templates.BODIES else "B"
        subject, body = templates.render(variant, i, r["first_name"], r["company"])
        built = mailer.build_raw(test_to or r["email"], subject, body, attach=not args.no_attach, bcc=bcc)
        state.update(conn, r["prospect_id"], status="composed", composed_run=run_id, variant=variant,
                     subject=subject, body=body, raw=built["raw"], composed_at=state.now(), error=None)
        state.event(conn, run_id, r["prospect_id"], "composed", f"{variant} {subject}" + (f" TEST->{test_to}" if test_to else ""))
        view = item_view({**dict(r), "variant": variant, "subject": subject, "body": body})
        view["real_to"] = r["email"]
        out.append(view)
    meta = {"india": sum(1 for r in items if r["pass"] == 1), "abroad": sum(1 for r in items if r["pass"] == 2),
            "credits_used": args.credits_used, "remaining_credits": args.remaining_credits,
            "resume_bytes": info["bytes"], "resume_sha256": info["sha256"], "sends_today": used, "cap": config.MAX_PER_DAY,
            "resolved_free": sum(1 for r in items if r["resolve_method"] == "hash_verified"), "test_to": test_to}
    try:
        import supply
        meta["supply_lines"] = supply.digest_lines(conn)
    except Exception as e:  # the digest must not fail over a report line
        meta["supply_lines"] = [f"Supply forecast unavailable: {type(e).__name__}: {e}"]
    title = "RECRUITER OUTREACH - TEST MODE (every email goes to you, nothing is marked sent)" if test_to \
        else "RECRUITER OUTREACH - APPROVAL REQUEST"
    digest = mailer.render_digest(title, run_id, out, meta) if out else ""
    state.set_run(conn, run_id, status="composed" if out else "empty", note=f"{len(out)} composed" + (" test" if test_to else ""))
    return emit({"ok": True, "run_id": run_id, "count": len(out), "sends_today": used, "room_today": room,
                 "india": meta["india"], "abroad": meta["abroad"], "resolved_free": meta["resolved_free"],
                 "resume_sha256": info["sha256"], "test_mode": bool(test_to), "items": out, "digest": digest,
                 "skipped_pending": max(0, len(ready) - len(items)), "skipped": dict(skipped)})


def first_touch_refusal(conn, r):
    stop = breaker_refusal(conn)
    if stop:
        return stop
    pid = r["prospect_id"]
    used = state.first_touch_sends_today(conn) + state.sending_today(conn, exclude_id=pid)
    if used >= config.MAX_PER_DAY:
        return f"daily cap reached ({used}/{config.MAX_PER_DAY})"
    daily, monthly = company_caps(r)
    counts = state.company_first_touches(conn, companies.company_key, exclude_ids=(pid,), composed=False)
    done_today, done_month = counts.get(companies.company_key(r["company"] or ""), (0, 0))
    if done_today >= daily:
        return f"{r['company']} daily cap reached ({done_today}/{daily})"
    if monthly is not None and done_month >= monthly:
        return f"{r['company']} 30-day cap reached ({done_month}/{monthly})"
    if state.email_already_contacted(conn, r["email"], exclude_id=pid):
        return "already contacted"
    if employer_unconfirmed(r):
        return "employer unconfirmed"
    if r["resolve_method"] == "pattern_unverified" and not pattern_send_ok(conn, r):
        return "unverified pattern"
    return None


def followup_refusal(conn, r):
    stop = breaker_refusal(conn)
    if stop:
        return stop
    used = state.followup_sends_today(conn) + state.sending_today(conn, "followup", exclude_id=r["prospect_id"])
    if used >= config.MAX_FOLLOWUPS_PER_DAY:
        return f"follow-up cap reached ({used}/{config.MAX_FOLLOWUPS_PER_DAY})"
    return None


def release(conn, r, followup, reason):
    if followup:
        state.update(conn, r["prospect_id"], status="sent", followup_raw=None, followup_claimed_at=None, error=reason)
    else:
        state.update(conn, r["prospect_id"], status=TERMINAL.get(reason) or restore_ready(conn, r), raw=None,
                     composed_run=None, claimed_at=None, error=reason)


def cmd_claim(args):
    conn = state.connect()
    if not state.get(conn, args.prospect_id):
        return emit({"ok": False, "claimed": False, "reason": "unknown prospect"})
    fu = args.kind == "followup"
    if not fu:
        companies.require()
    refused, claimed = None, False
    with state.write_txn(conn):
        r = state.get(conn, args.prospect_id)
        if state.claimable(r, args.kind):
            # raw is addressed by the run that composed the row last (test or real recipient); leave that run's row alone
            if not fu and args.run_id and r["composed_run"] != args.run_id:
                refused = f"composed by another run ({r['composed_run'] or 'none'})"
            else:
                refused = followup_refusal(conn, r) if fu else first_touch_refusal(conn, r)
                if refused:
                    release(conn, r, fu, refused)
                else:
                    claimed = state.claim(conn, args.prospect_id, args.kind)
    if refused:
        state.event(conn, args.run_id, args.prospect_id, f"claim_refused_{args.kind}", refused)
        return emit({"ok": False, "claimed": False, "prospect_id": args.prospect_id, "reason": refused})
    if not claimed:
        return emit({"ok": True, "claimed": False, "prospect_id": args.prospect_id, "status": r["status"],
                     "reason": f"not claimable from status {r['status']}"})
    state.event(conn, args.run_id, args.prospect_id, f"claimed_{args.kind}", "")
    raw = r["followup_raw"] if fu else r["raw"]
    subject = r["followup_subject"] if fu else r["subject"]
    return emit({"ok": True, "claimed": True, "prospect_id": args.prospect_id, "to": r["email"], "company": r["company"],
                 "full_name": r["full_name"], "subject": subject, "raw": raw, "thread_id": r["thread_id"], "kind": args.kind})


def cmd_record(args):
    conn = state.connect()
    r = state.get(conn, args.prospect_id)
    if not r:
        return emit({"ok": False, "error": "unknown prospect"})
    fu = args.kind == "followup"
    if args.test:
        back = "sent" if fu else ("resolved" if r["resolve_method"] in ("hash_verified", "smtp_valid", "provided") else "enriched")
        state.update(conn, args.prospect_id, status=back, raw=None, composed_run=None, claimed_at=None,
                     followup_claimed_at=None, error=None if args.status == "sent" else (args.error or "")[:1500])
        state.event(conn, args.run_id, args.prospect_id, f"test_{args.kind}_{args.status}", args.error or args.message_id or "")
        return emit({"ok": True, "prospect_id": args.prospect_id, "status": args.status, "kind": args.kind,
                     "test_mode": True, "restored_status": back, "sends_today": state.sends_today(conn)})
    if args.status == "sent":
        due = state.add_business_days(dt.date.today(), config.FOLLOWUP_BUSINESS_DAYS).isoformat()
        if fu:
            state.update(conn, args.prospect_id, status="followup_sent", followup_sent_at=state.now(),
                         followup_message_id=args.message_id, error=None)
        else:
            state.update(conn, args.prospect_id, status="sent", sent_at=state.now(), message_id=args.message_id,
                         thread_id=args.thread_id or r["thread_id"], followup_due=due, error=None, raw=None)
    else:
        err = (args.error or "send failed")[:1500]
        state.update(conn, args.prospect_id, status="followup_failed" if fu else "send_failed", error=err)
    state.event(conn, args.run_id, args.prospect_id, f"{args.kind}_{args.status}", args.error or args.message_id or "")
    state.export_csv(conn)
    return emit({"ok": True, "prospect_id": args.prospect_id, "status": args.status, "kind": args.kind,
                 "sends_today": state.sends_today(conn)})


def restore_ready(conn, r):
    if r["resolve_method"] in ("hash_verified", "smtp_valid", "provided"):
        return "resolved"
    return "enriched" if r["email"] else "discovered"


def cmd_abort(args):
    conn = state.connect()
    n = 0
    for r in state.prospects(conn, ["composed", "sending"], run_id=args.run_id):
        state.update(conn, r["prospect_id"], status=restore_ready(conn, r), raw=None, composed_run=None, claimed_at=None)
        n += 1
    state.set_run(conn, args.run_id, status="aborted", note=args.reason)
    state.event(conn, args.run_id, None, "aborted", args.reason)
    return emit({"ok": True, "run_id": args.run_id, "requeued": n, "reason": args.reason})


def cmd_requeue(args):
    conn = state.connect()
    rows = [state.get(conn, args.prospect_id)] if args.prospect_id else state.prospects(conn, ["send_failed"])
    n = 0
    for r in rows:
        if r and r["status"] in ("send_failed", "no_email", "sending", "composed"):
            state.update(conn, r["prospect_id"], status=restore_ready(conn, r), raw=None, composed_run=None,
                         claimed_at=None, error=None)
            n += 1
    return emit({"ok": True, "requeued": n})


def cmd_mark(args):
    conn = state.connect()
    r = state.get(conn, args.prospect_id)
    if not r:
        return emit({"ok": False, "error": "unknown prospect"})
    fields = {"bounced": {"status": "bounced", "bounced_at": state.now()},
              "replied": {"status": "replied", "replied_at": state.now()}}[args.status]
    state.update(conn, args.prospect_id, error=args.note, **fields)
    if r["email"]:
        domain = r["email"].split("@", 1)[1]
        pattern = r["resolve_pattern"] or resolver.infer_pattern(r["full_name"], r["email"], r["first_name"], r["last_name"])
        outcome = "bounced" if args.status == "bounced" else "delivered"
        state.upsert_pattern(conn, domain, pattern or "unknown", r["email"], outcome, "mark")
        state.export_patterns_csv(conn)
    state.event(conn, args.run_id, args.prospect_id, args.status, args.note or "")
    state.export_csv(conn)
    return emit({"ok": True, "prospect_id": args.prospect_id, "status": args.status})


def cmd_followups(args):
    conn = state.connect()
    run_id = state.new_run(conn, "followups", args.limit)
    used = state.followup_sends_today(conn)
    stop = breaker_refusal(conn)
    if stop:
        state.set_run(conn, run_id, status="skipped", note=stop)
        return emit({"ok": False, "run_id": run_id, "count": 0, "items": [], "digest": "", "sends_today": used,
                     "room_today": 0, "test_mode": bool(test_recipient(args)), "stage": "breaker", "error": stop})
    room = max(0, min(args.limit, config.MAX_FOLLOWUPS_PER_DAY - used - state.sending_today(conn, "followup")))
    due = state.followups_due(conn)[:room]
    test_to = test_recipient(args)
    items = []
    for r in due:
        subject, body = templates.render_followup(r["first_name"], r["company"], r["subject"])
        items.append({**item_view(r, to=test_to), "real_to": r["email"], "subject": subject, "body": body,
                      "message_id": r["message_id"], "thread_id": r["thread_id"], "sent_at": r["sent_at"],
                      "followup_due": r["followup_due"]})
    meta = {"india": sum(1 for r in due if r["pass"] == 1), "abroad": sum(1 for r in due if r["pass"] == 2),
            "sends_today": used, "cap": config.MAX_FOLLOWUPS_PER_DAY, "attach": False, "test_to": test_to}
    title = "RECRUITER OUTREACH - FOLLOW-UP TEST MODE (every email goes to you, nothing is marked sent)" if test_to \
        else "RECRUITER OUTREACH - FOLLOW-UP APPROVAL"
    digest = mailer.render_digest(title, run_id, items, meta) if items else ""
    state.set_run(conn, run_id, status="listed", note=f"{len(items)} due" + (" test" if test_to else ""))
    return emit({"ok": True, "run_id": run_id, "count": len(items), "sends_today": used, "room_today": room,
                 "test_mode": bool(test_to), "items": items, "digest": digest})


def cmd_followup_compose(args):
    conn = state.connect()
    r = state.get(conn, args.prospect_id)
    if not r or r["status"] != "sent" or r["followup_sent_at"] or r["bounced_at"] or r["replied_at"]:
        return emit({"ok": True, "composed": False, "prospect_id": args.prospect_id,
                     "reason": f"not eligible (status {r['status'] if r else 'unknown'})"})
    subject, body = templates.render_followup(r["first_name"], r["company"], r["subject"])
    test_to = test_recipient(args)
    bcc = config.SELF_EMAIL if (config.BCC_SELF and not test_to) else None
    built = mailer.build_raw(test_to or r["email"], subject, body, attach=False, bcc=bcc,
                             in_reply_to=args.in_reply_to, references=args.references or args.in_reply_to)
    state.update(conn, args.prospect_id, status="followup_composed", followup_subject=subject,
                 followup_body=body, followup_raw=built["raw"])
    state.event(conn, args.run_id, args.prospect_id, "followup_composed", subject + (f" TEST->{test_to}" if test_to else ""))
    return emit({"ok": True, "composed": True, "prospect_id": args.prospect_id, "to": test_to or r["email"],
                 "real_to": r["email"], "subject": subject, "thread_id": r["thread_id"], "test_mode": bool(test_to)})


def find_sent(conn, args):
    if args.thread_id:
        where, params = "thread_id=?", (args.thread_id.strip(),)
    elif args.message_id:
        mid = args.message_id.strip().strip("<>")
        where, params = "(message_id=? OR followup_message_id=?)", (mid, mid)
    else:
        where, params = "lower(trim(email))=?", (args.email.strip().lower(),)
    return conn.execute(f"SELECT * FROM prospects WHERE {where} AND sent_at IS NOT NULL ORDER BY sent_at DESC LIMIT 1",
                        params).fetchone()


def cmd_bounce(args):
    conn = state.connect()
    r = find_sent(conn, args)
    if not r:
        return emit({"ok": True, "matched": None})
    pid, reason = r["prospect_id"], (args.reason or "").strip()
    if r["bounced_at"] or r["replied_at"] or DELAY_RE.search(reason):
        note = "already bounced" if r["bounced_at"] else "replied" if r["replied_at"] else "delay notice ignored"
        return emit({"ok": True, "matched": pid, "changed": False, "note": note})
    state.update(conn, pid, status="bounced", bounced_at=state.now(), error=(reason or "bounce")[:1500])
    if r["email"] and "@" in r["email"]:
        pattern = r["resolve_pattern"] or resolver.infer_pattern(r["full_name"], r["email"], r["first_name"], r["last_name"])
        state.upsert_pattern(conn, r["email"].split("@", 1)[1], pattern or "unknown", r["email"], "bounced", "bounce")
        state.export_patterns_csv(conn)
    state.event(conn, None, pid, "bounced", reason or "bounce")
    state.export_csv(conn)
    return emit({"ok": True, "matched": pid, "changed": True})


def cmd_summary(args):
    conn = state.connect()
    counts, run = state.run_summary(conn, args.run_id)
    rows = state.prospects(conn, run_id=args.run_id, order="company")
    lines = [f"Run {args.run_id}: " + (", ".join(f"{k} {v}" for k, v in sorted(counts.items())) or "no items"),
             f"Credits used this run: {run.get('credits_used', 0)}    First-touch sends today: "
             f"{state.first_touch_sends_today(conn)} / {config.MAX_PER_DAY}    Follow-ups today: "
             f"{state.followup_sends_today(conn)} / {config.MAX_FOLLOWUPS_PER_DAY}", ""]
    batch = []
    for r in rows:
        if r["composed_run"] != args.run_id and r["discovered_run"] != args.run_id:
            continue
        mark = {"sent": "SENT   ", "send_failed": "FAILED ", "sending": "STUCK  "}.get(r["status"], r["status"].upper()[:7].ljust(7))
        via = " (free)" if r["resolve_method"] in ("hash_verified", "smtp_valid") else ""
        lines.append(f"{mark} {r['full_name']:<26} {r['company']:<18} {r['email'] or ''}{via}  {r['error'] or ''}".rstrip())
        batch.append(r)
    return emit({"ok": True, "run_id": args.run_id, "counts": counts, "credits_used": run.get("credits_used", 0),
                 "sends_today": state.sends_today(conn), "text": "\n".join(lines),
                 "html": _summary_html(conn, args.run_id, counts, run, batch)})


def _summary_html(conn, run_id, counts, run, rows):
    import html as _h
    sent = counts.get("sent", 0)
    failed = counts.get("send_failed", 0) + counts.get("sending", 0)
    ft, fu = state.first_touch_sends_today(conn), state.followup_sends_today(conn)
    badge = {"sent": ("#e7f6ec", "#1e7e34", "SENT"), "send_failed": ("#fdecec", "#c0392b", "FAILED"),
             "sending": ("#fef6e0", "#8a6d00", "STUCK")}
    def chip(label, value):
        return (f'<td style="padding:6px 14px;background:#f2f4f7;border-radius:8px;'
                f'font:13px -apple-system,Segoe UI,Roboto,sans-serif;color:#344054">'
                f'<b style="color:#101828">{value}</b> {_h.escape(label)}</td>')
    trs = []
    for r in rows:
        bg, fg, txt = badge.get(r["status"], ("#eef0f3", "#475467", (r["status"] or "").upper()[:8]))
        free = ' <span style="color:#1e7e34;font-size:11px">&#10003; free</span>' if r["resolve_method"] in ("hash_verified", "smtp_valid") else ""
        err = f'<div style="color:#c0392b;font-size:11px">{_h.escape(r["error"] or "")}</div>' if r["error"] else ""
        trs.append(
            f'<tr style="border-top:1px solid #eaecf0">'
            f'<td style="padding:9px 10px"><span style="display:inline-block;padding:2px 8px;border-radius:12px;'
            f'background:{bg};color:{fg};font-size:11px;font-weight:600">{_h.escape(txt)}</span></td>'
            f'<td style="padding:9px 10px;font-weight:600;color:#101828">{_h.escape(r["full_name"] or "")}</td>'
            f'<td style="padding:9px 10px;color:#475467">{_h.escape(r["company"] or "")}</td>'
            f'<td style="padding:9px 10px;color:#475467">{_h.escape(r["email"] or "")}{free}{err}</td></tr>')
    body_rows = "".join(trs) or '<tr><td colspan="4" style="padding:12px;color:#98a2b3">no items in this run</td></tr>'
    f = "font:14px -apple-system,BlinkMacSystemFont,Segoe UI,Roboto,sans-serif"
    return (
        f'<div style="{f};max-width:640px;margin:0 auto;color:#101828">'
        f'<div style="background:#101828;color:#fff;padding:16px 20px;border-radius:12px 12px 0 0">'
        f'<div style="font-size:17px;font-weight:700">Outreach batch complete</div>'
        f'<div style="font-size:12px;color:#98a2b3;margin-top:2px">Run {_h.escape(run_id or "")}</div></div>'
        f'<div style="border:1px solid #eaecf0;border-top:0;border-radius:0 0 12px 12px;padding:18px 20px">'
        f'<table cellspacing="6" cellpadding="0" style="margin-bottom:6px"><tr>'
        f'{chip("sent", sent)}{chip("failed", failed)}'
        f'{chip(f"first-touch today / {config.MAX_PER_DAY}", ft)}'
        f'{chip(f"follow-ups today / {config.MAX_FOLLOWUPS_PER_DAY}", fu)}</tr></table>'
        f'<table width="100%" cellspacing="0" cellpadding="0" style="margin-top:12px;border-collapse:collapse">'
        f'<tr style="text-align:left;color:#667085;font-size:11px;text-transform:uppercase">'
        f'<th style="padding:6px 10px">Status</th><th style="padding:6px 10px">Name</th>'
        f'<th style="padding:6px 10px">Company</th><th style="padding:6px 10px">Email</th></tr>'
        f'{body_rows}</table>'
        f'<div style="margin-top:16px;padding-top:12px;border-top:1px solid #eaecf0;color:#667085;font-size:12px">'
        f'Follow-ups come due in 5 business days. Re-queue failed sends on the Razer:<br>'
        f'<code style="background:#f2f4f7;padding:2px 6px;border-radius:4px">cd ~/outreach &amp;&amp; python3 pipeline.py requeue</code>'
        f'</div></div></div>')


def cmd_status(args):
    conn = state.connect()
    counts = {r[0]: r[1] for r in conn.execute("SELECT status, COUNT(*) FROM prospects GROUP BY status")}
    return emit({"ok": True, "counts": counts, "sends_today": state.sends_today(conn),
                 "first_touch_today": state.first_touch_sends_today(conn), "followups_today": state.followup_sends_today(conn),
                 "bounces": state.bounce_stats(conn), "followups_due": len(state.followups_due(conn)),
                 "csv_rows": state.export_csv(conn), "patterns": state.export_patterns_csv(conn)})


def cmd_patterns(args):
    conn = state.connect()
    if args.add:
        domain, pattern, evidence, outcome = args.add
        if pattern not in resolver.PATTERNS:
            return emit({"ok": False, "error": f"unknown pattern {pattern}; known: {resolver.PATTERNS}"})
        state.upsert_pattern(conn, domain, pattern, evidence, outcome, "manual")
    rows = [dict(r) for r in state.patterns_all(conn)]
    state.export_patterns_csv(conn)
    return emit({"ok": True, "count": len(rows), "patterns": rows})


def register(sub):
    c = sub.add_parser("compose")
    c.add_argument("--run-id")
    c.add_argument("--limit", type=int, default=config.MAX_PER_DAY)
    c.add_argument("--tier", choices=["A", "B", "C"])
    c.add_argument("--credits-used", type=int, default=0)
    c.add_argument("--remaining-credits", default="?")
    c.add_argument("--no-attach", action="store_true")
    c.add_argument("--test-recipient", default="")
    c.add_argument("--test", action="store_true", help="send every email to --test-recipient or OUTREACH_SELF_EMAIL")
    cl = sub.add_parser("claim")
    cl.add_argument("--run-id")
    cl.add_argument("--prospect-id", required=True)
    cl.add_argument("--kind", choices=["new", "followup"], default="new")
    rc = sub.add_parser("record")
    rc.add_argument("--run-id")
    rc.add_argument("--prospect-id", required=True)
    rc.add_argument("--kind", choices=["new", "followup"], default="new")
    rc.add_argument("--status", choices=["sent", "failed"], required=True)
    rc.add_argument("--message-id")
    rc.add_argument("--thread-id")
    rc.add_argument("--error")
    rc.add_argument("--test", action="store_true")
    ab = sub.add_parser("abort")
    ab.add_argument("--run-id", required=True)
    ab.add_argument("--reason", default="declined")
    rq = sub.add_parser("requeue")
    rq.add_argument("--prospect-id")
    mk = sub.add_parser("mark")
    mk.add_argument("--run-id")
    mk.add_argument("--prospect-id", required=True)
    mk.add_argument("--status", choices=["bounced", "replied"], required=True)
    mk.add_argument("--note")
    fu = sub.add_parser("followups")
    fu.add_argument("--limit", type=int, default=config.MAX_FOLLOWUPS_PER_DAY)
    fc = sub.add_parser("followup-compose")
    fc.add_argument("--run-id")
    fc.add_argument("--prospect-id", required=True)
    fc.add_argument("--in-reply-to")
    fc.add_argument("--references")
    for p in (fu, fc):
        p.add_argument("--test-recipient", default="")
        p.add_argument("--test", action="store_true")
    bo = sub.add_parser("bounce")
    by = bo.add_mutually_exclusive_group(required=True)
    by.add_argument("--thread-id")
    by.add_argument("--message-id")
    by.add_argument("--email")
    bo.add_argument("--reason")
    sm = sub.add_parser("summary")
    sm.add_argument("--run-id", required=True)
    sub.add_parser("status")
    pt = sub.add_parser("patterns")
    pt.add_argument("--add", nargs=4, metavar=("DOMAIN", "PATTERN", "EVIDENCE_EMAIL", "OUTCOME"))


HANDLERS = {"compose": cmd_compose, "claim": cmd_claim, "record": cmd_record, "abort": cmd_abort,
            "requeue": cmd_requeue, "mark": cmd_mark, "followups": cmd_followups,
            "followup-compose": cmd_followup_compose, "summary": cmd_summary, "status": cmd_status,
            "patterns": cmd_patterns, "bounce": cmd_bounce}
