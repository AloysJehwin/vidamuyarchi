"""Passive OSINT for a company's address format: harvest real addresses at the domain with theHarvester and
EmailFinder, classify their shape, and store the dominant shape as pattern evidence (outcome "osint")."""
import json
import pathlib
import re
import shutil
import subprocess
from collections import Counter

import config
import state

EMAIL_RE = re.compile(r"[a-z0-9._%+\-]+@([a-z0-9.\-]+\.[a-z]{2,})", re.I)
SHAPE_TO_PATTERNS = {
    "dot": ["first.last", "last.first", "first.middle.last", "finitial.last", "first.linitial", "linitial.first"],
    "underscore": ["first_last"],
    "hyphen": ["first-last"],
    "plain": ["finitial+last", "first+linitial", "linitial+first", "firstlast", "first", "last+first", "last+finitial"],
}


def _run(cmd, timeout):
    """(output, error): error is None when the tool exited 0."""
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        return f"__error__ {type(e).__name__}: {e}", type(e).__name__
    return (out.stdout or "") + "\n" + (out.stderr or ""), None if out.returncode == 0 else f"exit {out.returncode}"


def _tool(name):
    local = config.TOOLS_BIN / name
    if local.exists():
        return str(local)
    found = shutil.which(name) or shutil.which(name.lower())
    return found if found and pathlib.Path(found).exists() else None


def harvest(domain):
    """Return (set_of_emails, notes, ran) from every installed source, best effort; ran is False when no tool is
    installed or every installed tool failed without finding anything."""
    emails, notes, ran = set(), [], False
    config.OSINT_DIR.mkdir(exist_ok=True)
    th = _tool("theHarvester")
    if th:
        out_base = config.OSINT_DIR / re.sub(r"[^a-z0-9.-]", "_", domain)
        text, error = _run([th, "-d", domain, "-b", config.OSINT_SOURCES, "-l", "200", "-f", str(out_base)], config.OSINT_TIMEOUT_S)
        found = set()
        try:
            data = json.loads((out_base.with_suffix(".json")).read_text())
            found.update(e.lower() for e in data.get("emails", []) if isinstance(e, str))
        except (OSError, ValueError):
            found.update(m.group(0).lower() for m in EMAIL_RE.finditer(text))
        ran = ran or error is None or bool(found)
        notes.append(f"theHarvester: {len(found)}" + (f" ({error})" if error else ""))
        emails.update(found)
    else:
        notes.append("theHarvester: not installed")
    ef = _tool("emailfinder")
    if ef:
        text, error = _run([ef, "-d", domain], config.OSINT_TIMEOUT_S)
        found = {m.group(0).lower() for m in EMAIL_RE.finditer(text)}
        ran = ran or error is None or bool(found)
        notes.append(f"emailfinder: {len(found)}" + (f" ({error})" if error else ""))
        emails.update(found)
    else:
        notes.append("emailfinder: not installed")
    same_domain = {e for e in emails if e.split("@", 1)[1] == domain.lower()}
    return same_domain, notes, ran


def usable(email):
    local = email.split("@", 1)[0]
    if local in config.ROLE_LOCAL_PARTS or local.startswith(("noreply", "no-reply", "donotreply")):
        return False
    if any(ch.isdigit() for ch in local) and sum(ch.isdigit() for ch in local) > 2:
        return False
    return 2 <= len(local) <= 40


def shape(email):
    local = email.split("@", 1)[0]
    if "." in local:
        return "dot"
    if "_" in local:
        return "underscore"
    if "-" in local:
        return "hyphen"
    return "plain"


def discover_patterns(conn, domain, run_id=None):
    emails, notes, ran = harvest(domain)
    good = sorted(e for e in emails if usable(e))
    counts = Counter(shape(e) for e in good)
    result = {"domain": domain, "harvested": len(emails), "usable": len(good), "shapes": dict(counts),
              "dominant_shape": None, "patterns": [], "examples": good[:8], "notes": notes, "unavailable": not ran}
    if not ran:
        # a distinct source, so the 90-day "already researched" check (source='osint') keeps retrying this domain
        state.upsert_pattern(conn, domain, "unknown", f"osint-unavailable:{domain}", "osint_unavailable", "osint_unavailable")
        if run_id:
            state.event(conn, run_id, None, "osint_unavailable", f"{domain}: {'; '.join(notes)}")
        return result
    if not good:
        state.upsert_pattern(conn, domain, "unknown", f"osint:{domain}", "osint_none", "osint")
        return result
    dominant, n = counts.most_common(1)[0]
    result["dominant_shape"] = dominant
    result["patterns"] = SHAPE_TO_PATTERNS[dominant]
    # 'plain' does not pin the format (firstlast/finitial+last/... all collapse to one guess), so it never becomes
    # send-grade 'osint' evidence; only separator shapes, which map more tightly, do.
    strength = "osint" if n >= config.OSINT_MIN_EXAMPLES and dominant != "plain" else "osint_weak"
    for i, pattern in enumerate(result["patterns"][:3]):
        state.upsert_pattern(conn, domain, pattern, f"osint:{domain}:{pattern}", strength if i == 0 else "osint_weak", "osint")
    for e in good[:8]:
        state.upsert_pattern(conn, domain, SHAPE_TO_PATTERNS[shape(e)][0], e, "osint_example", "osint")
    if run_id:
        state.event(conn, run_id, None, "osint", f"{domain}: {len(good)} usable, shape {dominant} x{n}")
    return result
