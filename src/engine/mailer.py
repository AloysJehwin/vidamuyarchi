import base64
import datetime as dt
import email
import hashlib
import html
import re
import subprocess
from email.message import EmailMessage
from email.utils import formatdate

import config

# bare links in the copy (shop.example.in, github.com/your-handle/...): the HTML part links them explicitly so the
# reader sees the text exactly as written instead of whatever their client's auto-linker makes of it
LINK_RE = re.compile(r"\b((?:https?://)?(?:[a-z0-9-]+\.)+(?:in|com|io|ai|dev|org)(?:/[A-Za-z0-9._~/-]*)?)", re.I)
EMAIL_RE = re.compile(r"^[a-z0-9._%+\-']+@[a-z0-9.\-]+\.[a-z]{2,}$")
_mx_cache = {}


def resume_info():
    path = config.RESUME
    if not path.exists():
        return {"ok": False, "path": str(path), "reason": "missing"}
    data = path.read_bytes()
    ok = len(data) >= config.MIN_RESUME_BYTES and data[:4] == b"%PDF"
    return {"ok": ok, "path": str(path), "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(),
            "reason": None if ok else "not a PDF or too small"}


def body_html(body):
    """The plain-text body as minimal HTML: escaped, line breaks kept, bare links as anchors with the same text."""
    def link(m):
        text = m.group(1).rstrip("./")
        tail = m.group(1)[len(text):]
        href = text if text.lower().startswith(("http://", "https://")) else f"https://{text}"
        return f'<a href="{html.escape(href, quote=True)}">{text}</a>{tail}'
    escaped = html.escape(body, quote=False)
    linked = LINK_RE.sub(link, escaped)
    return '<div style="font-family:Arial,sans-serif;font-size:14px">' + linked.replace("\n", "<br>\n") + "</div>"


def build_raw(to, subject, body, attach=True, bcc=None, in_reply_to=None, references=None):
    msg = EmailMessage()
    msg["To"] = to
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    if bcc:
        msg["Bcc"] = bcc
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = references or in_reply_to
    msg.set_content(body)
    msg.add_alternative(body_html(body), subtype="html")
    sha = None
    if attach:
        info = resume_info()
        if not info["ok"]:
            raise RuntimeError(f"resume check failed: {info['reason']} ({info['path']})")
        data = config.RESUME.read_bytes()
        msg.add_attachment(data, maintype="application", subtype="pdf", filename=config.RESUME.name)
        sha = info["sha256"]
    raw_bytes = msg.as_bytes()
    if attach:
        verify_attachment(raw_bytes, sha)
    return {"raw": base64.urlsafe_b64encode(raw_bytes).decode(), "bytes": len(raw_bytes), "attachment_sha256": sha}


def verify_attachment(raw_bytes, expected_sha):
    parsed = email.message_from_bytes(raw_bytes)
    for part in parsed.walk():
        if part.get_content_disposition() == "attachment":
            got = hashlib.sha256(part.get_payload(decode=True)).hexdigest()
            if got != expected_sha:
                raise RuntimeError("attachment bytes do not match the resume on disk")
            return True
    raise RuntimeError("attachment part missing from built message")


def classify_email(address, email_type, status):
    address = (address or "").strip().lower()
    if not EMAIL_RE.match(address):
        return "invalid_syntax"
    local, domain = address.split("@", 1)
    if status == "invalid":
        return "provider_invalid"
    if local in config.ROLE_LOCAL_PARTS or local.startswith(("noreply", "no-reply", "donotreply")):
        return "role_address"
    if domain in config.FREE_MAIL_DOMAINS or email_type == "personal":
        return "ok_personal" if config.ALLOW_PERSONAL_EMAIL else "personal_not_allowed"
    if status == "catch_all" and not config.ALLOW_CATCH_ALL:
        return "catch_all_not_allowed"
    return "ok"


def mx_ok(domain):
    if not config.MX_CHECK:
        return None
    if domain in _mx_cache:
        return _mx_cache[domain]
    result = None
    for cmd in (["dig", "+short", "+time=3", "+tries=1", "MX", domain], ["host", "-t", "MX", "-W", "3", domain]):
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=8)
        except (OSError, subprocess.TimeoutExpired):
            continue
        text = (out.stdout or "").strip().lower()
        if out.returncode == 0 and text and "no mx record" not in text and "not found" not in text:
            result = True
            break
        if out.returncode == 0 and (not text or "no mx record" in text):
            result = False
            break
    _mx_cache[domain] = result
    return result


def pick_email(emails, status):
    rank = {"current_professional": 0, "professional": 1, "": 2, "personal": 3}
    rejected = []
    for e in sorted(emails, key=lambda e: rank.get(e.get("type", ""), 2)):
        verdict = classify_email(e["address"], e.get("type", ""), status)
        if verdict.startswith("ok"):
            domain = e["address"].split("@", 1)[1]
            mx = mx_ok(domain)
            if mx is False:
                rejected.append((e["address"], "no_mx"))
                continue
            return e["address"], e.get("type", ""), mx, rejected
        rejected.append((e["address"], verdict))
    return None, None, None, rejected


def render_digest(title, run_id, items, meta):
    n = len(items)
    lines = [
        title,
        f"Run: {run_id}    Prepared: {dt.datetime.now().strftime('%d %b %Y %H:%M %Z').strip()}",
        f"Emails to send: {n}    India {meta.get('india', 0)} / abroad {meta.get('abroad', 0)}",
        *([f"Credits spent so far this run: {meta.get('credits_used', 0)}    Explorium credits remaining: {meta.get('remaining_credits', '?')}"]
          if config.EXPLORIUM_ENABLED else []),
        (f"Attachment on every email: {config.RESUME.name} ({meta.get('resume_bytes', '?')} bytes, sha256 {str(meta.get('resume_sha256', ''))[:16]}...)"
         if meta.get("attach", True) else "No attachment (follow-ups reply inside the original thread)."),
        f"BCC copy to {config.SELF_EMAIL} on every send: {'yes' if config.BCC_SELF and not meta.get('test_to') else 'no'}",
        f"Sends today before this batch: {meta.get('sends_today', 0)} of max {meta.get('cap', config.MAX_PER_DAY)}",
        *meta.get("supply_lines", []),
        "",
        "APPROVE = send everything below now, one email every 30-60 seconds.",
        "DECLINE = nothing is sent; these contacts stay queued for the next run (no credits wasted).",
        "No reply within the wait limit = treated as DECLINE.",
        "=" * 70,
    ]
    for i, it in enumerate(items, 1):
        where = f"{it.get('city') or '?'}, {it.get('country') or '?'}"
        flag = "  ** relocation/visa likely **" if it.get("pass") == 2 else ""
        if it.get("source") == "linkedin_unconfirmed":
            flag += "  ** employer from LinkedIn search, confirmed by mailbox **"
        how = {"hash_verified": "hash-verified (free)", "smtp_valid": "mailbox confirmed by SMTP (free)",
               "smtp_catch_all": "catch-all domain, pattern-backed", "explorium": "Explorium reveal (paid)",
               "provided": "address supplied by you", "manual": "manual",
               "pattern_unverified": "NOT VERIFIED - learned company pattern only"}.get(it.get("resolve_method") or "", it.get("resolve_method") or "?")
        conf = f", confidence {it['confidence']:.2f}" if isinstance(it.get("confidence"), (int, float)) else ""
        lines += [
            f"{i}/{n}  {it['full_name']} - {it['company']}   [Variant {it['variant']} | {it.get('job_title') or '?'} | {where}]{flag}",
            f"To: {it['to']}   ({how}{conf})",
            f"Subject: {it['subject']}",
            "",
            it["body"],
            "-" * 70,
        ]
    return "\n".join(lines)
