"""Free mailbox verification. Prefers the self-hosted reacher CLI (check_if_email_exists) when installed,
falls back to a plain SMTP RCPT probe; both go through the SOCKS5 proxy when OUTREACH_SMTP_PROXY_HOST is set.
Results are cached in SQLite per domain and per address, except failures on our side (timeouts, refused or
unreachable connections, proxy errors, a failed MX lookup, an exhausted probe budget), which are never cached.
Result vocabulary: valid | invalid | unknown | blocked (our IP rejected by policy) | throttled (4xx, retry later)."""
import datetime as dt
import json
import os
import re
import secrets
import smtplib
import socket
import subprocess
import time

import config
import state

_mx_cache = {}
_mx_failed = set()
_unsettled = {}
_last_probe = {}
_probes_this_run = 0
_reacher_off = False
_capability = None
BUDGET_MSG = "probe budget exhausted"
BLOCK_WORDS = ("spamhaus", "blocked", "blacklist", "block list", "blocklist", "policy", "access denied", "service unavailable",
               "not authorized", "reputation", "dynamic", "rbl", "dnsbl", "barracuda", "listed", "sbrs", "senderbase",
               "score too low")
UNKNOWN_USER_WORDS = ("unknown", "no such user", "does not exist", "not exist", "invalid recipient", "recipient rejected",
                      "user not found", "mailbox unavailable", "illegal alias", "5.1.1", "not found")
LOCAL_WORDS = ("timed out", "timeout", "refused", "unreachable", "no route", "errno", "disconnected", "reset by peer",
               "gaierror", "socks", "proxy", "budget", "connect none", "reacher none", "reacher unknown")
SOCKS_REPLIES = {1: "general failure", 2: "not allowed by ruleset", 3: "network unreachable", 4: "host unreachable",
                 5: "connection refused", 6: "TTL expired", 7: "command not supported", 8: "address type not supported"}


class ProxyError(OSError):
    pass


def reacher_available():
    return not _reacher_off and bool(config.REACHER_BIN) and config.REACHER_BIN.exists()


def verification_capability(refresh=False):
    """{'direct_port25': True | False | None (no DNS), 'proxy_configured': bool, 'reacher': bool}, from one short
    banner check against Gmail's MX, remembered for the rest of the process."""
    global _capability
    if _capability is None or refresh:
        _capability = {"direct_port25": _direct_port25(), "proxy_configured": bool(config.SMTP_PROXY_HOST),
                       "reacher": reacher_available()}
    return dict(_capability)


def _direct_port25(timeout=5):
    host = (mx_hosts("gmail.com") or ["gmail-smtp-in.l.google.com"])[0]
    try:
        with socket.create_connection((host, 25), timeout=timeout) as s:
            banner = s.recv(512)
            try:
                s.sendall(b"QUIT\r\n")
            except OSError:
                pass
    except socket.gaierror:
        return None
    except OSError:
        return False
    return banner.startswith(b"220")


def classify_reply(code, text):
    t = (text or "").lower()
    if code in (250, 251):
        return "valid"
    if code is None:
        return "unknown"
    if 400 <= code < 500:
        return "throttled"
    if re.search(r"\b5\.7\.\d", t) or any(w in t for w in BLOCK_WORDS) and not any(w in t for w in UNKNOWN_USER_WORDS):
        return "blocked"
    if code in (550, 551, 553) or (code == 554 and any(w in t for w in UNKNOWN_USER_WORDS)):
        return "invalid"
    return "unknown"


def mx_hosts(domain):
    if domain in _mx_cache:
        return _mx_cache[domain]
    hosts, status = [], None
    try:
        # full output, not +short: +short prints nothing and exits 0 on SERVFAIL too, which read as "no MX" and
        # cached a resolver hiccup as an unverifiable domain for DOMAIN_CACHE_DAYS
        out = subprocess.run(["dig", "+noall", "+comments", "+answer", "+time=4", "+tries=2", "MX", domain],
                             capture_output=True, text=True, timeout=20)
        m = re.search(r"status: ([A-Z]+)", out.stdout)
        status = m.group(1) if m and out.returncode == 0 else None
        for line in out.stdout.splitlines():
            parts = line.split()
            if len(parts) >= 6 and parts[3] == "MX" and parts[4].isdigit() and parts[5].rstrip("."):
                hosts.append((int(parts[4]), parts[5].rstrip(".")))  # "0 ." is a null MX (RFC 7505): no mail
    except (OSError, subprocess.TimeoutExpired):
        pass
    hosts = [h for _, h in sorted(hosts)]
    if not hosts and status not in ("NOERROR", "NXDOMAIN"):
        _mx_failed.add(domain)
    _mx_cache[domain] = hosts
    return hosts


def _throttle(domain):
    global _probes_this_run
    if _probes_this_run >= config.SMTP_MAX_PROBES_PER_RUN:
        raise RuntimeError(BUDGET_MSG)
    gap = config.SMTP_MIN_INTERVAL_S - (time.monotonic() - _last_probe.get(domain, 0))
    if gap > 0:
        time.sleep(gap)
    _last_probe[domain] = time.monotonic()
    _probes_this_run += 1


def _parse_json_blob(text):
    text = (text or "").strip()
    try:
        return json.loads(text)
    except ValueError:
        pass
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except ValueError:
            return None
    return None


def _code_from_text(text):
    m = re.search(r"\b([245]\d\d)\b", text or "")
    return int(m.group(1)) if m else None


def _read(sock, n):
    data = b""
    while len(data) < n:
        chunk = sock.recv(n - len(data))
        if not chunk:
            raise ProxyError("SOCKS5 proxy closed the connection")
        data += chunk
    return data


def socks5_connect(host, port, timeout):
    """TCP connection to host:port through the configured SOCKS5 proxy (RFC 1928, username/password per RFC 1929);
    the proxy resolves the host name."""
    user, password, name = (config.SMTP_PROXY_USER or "").encode(), (config.SMTP_PROXY_PASS or "").encode(), host.encode()
    if max(len(user), len(password), len(name)) > 255:
        raise ProxyError("SOCKS5 username, password or host name longer than 255 bytes")
    sock = socket.create_connection((config.SMTP_PROXY_HOST, int(config.SMTP_PROXY_PORT)), timeout)
    try:
        sock.sendall(b"\x05\x02\x00\x02" if user else b"\x05\x01\x00")
        version, method = _read(sock, 2)
        if version != 5 or method not in ((0, 2) if user else (0,)):
            raise ProxyError("SOCKS5 proxy refused our authentication methods")
        if method == 2:
            sock.sendall(bytes([1, len(user)]) + user + bytes([len(password)]) + password)
            if _read(sock, 2)[1] != 0:
                raise ProxyError("SOCKS5 proxy rejected the username/password")
        sock.sendall(bytes([5, 1, 0, 3, len(name)]) + name + int(port).to_bytes(2, "big"))
        _, reply, _, atyp = _read(sock, 4)
        if reply:
            raise ProxyError(f"SOCKS5 connect to {host}:{port} failed: {SOCKS_REPLIES.get(reply, reply)}")
        if atyp not in (1, 3, 4):
            raise ProxyError("SOCKS5 proxy sent a malformed reply")
        _read(sock, (_read(sock, 1)[0] if atyp == 3 else 4 if atyp == 1 else 16) + 2)
        return sock
    except BaseException:
        sock.close()
        raise


class _ProxySMTP(smtplib.SMTP):
    def _get_socket(self, host, port, timeout):
        return socks5_connect(host, port, timeout)


def _reacher_env():
    env = {k: v for k, v in os.environ.items() if not k.startswith("PROXY_")}
    if config.SMTP_PROXY_HOST and config.SMTP_PROXY_USER:
        env.update(PROXY_USERNAME=config.SMTP_PROXY_USER, PROXY_PASSWORD=config.SMTP_PROXY_PASS)
    return env


def _reacher_unusable():
    """Use the built-in probe for the rest of this process and refund the probe reacher never made."""
    global _reacher_off, _probes_this_run
    _reacher_off = True
    _probes_this_run = max(0, _probes_this_run - 1)
    return None


def reacher_check(email):
    """Run the reacher CLI once. Returns the normalized dict or None when the tool is unusable."""
    try:
        _throttle(email.split("@", 1)[1])
    except RuntimeError as e:
        return {"result": "unknown", "code": None, "message": str(e), "catch_all": None, "engine": "reacher"}
    cmd = [str(config.REACHER_BIN), "--from-email", config.SMTP_FROM, "--hello-name", config.SMTP_HELO]
    if config.SMTP_PROXY_HOST:
        cmd += ["--proxy-host", config.SMTP_PROXY_HOST, "--proxy-port", str(config.SMTP_PROXY_PORT)]
    cmd.append(email)
    try:
        # proxy credentials go through PROXY_USERNAME/PROXY_PASSWORD (read by the CLI) so they stay out of `ps`
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=config.REACHER_TIMEOUT_S, env=_reacher_env())
    except subprocess.TimeoutExpired:
        return None
    except OSError:
        return _reacher_unusable()
    data = _parse_json_blob(out.stdout)
    if not isinstance(data, dict):
        return _reacher_unusable() if out.returncode else None
    smtp = data.get("smtp") or {}
    reach = data.get("is_reachable")
    catch_all = smtp.get("is_catch_all")
    err = smtp.get("error") if isinstance(smtp.get("error"), (str, dict)) else None
    err_text = json.dumps(err) if isinstance(err, dict) else (err or "")
    if smtp.get("is_deliverable") is True or reach == "safe":
        result = "valid"
    elif reach == "risky" and catch_all:
        result = "valid"
    elif smtp.get("is_disabled") or reach == "invalid":
        result = "invalid"
    else:
        result = classify_reply(_code_from_text(err_text), err_text) if err_text else "unknown"
        if result == "invalid":
            result = "unknown"
        # a permanent rejection of our IP that reacher reports without an SMTP code (Amazon: "SBRS score too low")
        low = err_text.lower()
        if result == "unknown" and "permanent" in low and any(w in low for w in BLOCK_WORDS):
            result = "blocked"
    if config.SMTP_PROXY_HOST and result == "unknown" and re.search(r"socks|proxy", err_text, re.I):
        return _reacher_unusable()
    return {"result": result, "code": _code_from_text(err_text), "message": f"reacher {reach}: {err_text[:140]}",
            "catch_all": catch_all, "engine": "reacher",
            "raw": {k: smtp.get(k) for k in ("is_catch_all", "is_deliverable", "is_disabled", "has_full_inbox", "can_connect_smtp")}}


def smtp_probe(email, mx):
    domain = email.split("@", 1)[1]
    try:
        _throttle(domain)
    except RuntimeError as e:
        return {"result": "unknown", "code": None, "message": str(e), "mx": mx}
    smtp_class = _ProxySMTP if config.SMTP_PROXY_HOST else smtplib.SMTP
    try:
        with smtp_class(mx, 25, local_hostname=config.SMTP_HELO, timeout=config.SMTP_TIMEOUT) as s:
            s.ehlo(config.SMTP_HELO)
            code, msg = s.mail(config.SMTP_FROM)
            if code >= 400:
                text = (msg.decode(errors="replace") if isinstance(msg, bytes) else str(msg))[:200]
                return {"result": classify_reply(code, text) if code >= 500 else "throttled", "code": code,
                        "message": f"MAIL FROM {code}: {text}", "mx": mx}
            code, msg = s.rcpt(email)
            text = (msg.decode(errors="replace") if isinstance(msg, bytes) else str(msg))[:200]
            try:
                s.quit()
            except smtplib.SMTPException:
                pass
    except smtplib.SMTPConnectError as e:
        code = getattr(e, "smtp_code", None)
        text = str(getattr(e, "smtp_error", e))
        result = "throttled" if code and 400 <= code < 500 else ("blocked" if code and code >= 500 else "unknown")
        return {"result": result, "code": code, "message": f"connect {code}: {text}"[:200], "mx": mx}
    except (smtplib.SMTPException, socket.timeout, OSError) as e:
        return {"result": "unknown", "code": None, "message": f"{type(e).__name__}: {e}"[:200], "mx": mx}
    return {"result": classify_reply(code, text), "code": code, "message": text, "mx": mx}


def _save_profile(conn, profile):
    conn.execute("""INSERT INTO smtp_domains(domain,mx,port25_ok,catch_all,checked_at,note) VALUES(?,?,?,?,?,?)
                    ON CONFLICT(domain) DO UPDATE SET mx=excluded.mx, port25_ok=excluded.port25_ok,
                    catch_all=excluded.catch_all, checked_at=excluded.checked_at, note=excluded.note""",
                 (profile["domain"], profile["mx"], profile["port25_ok"], profile["catch_all"], profile["checked_at"], profile["note"]))


def _unsettle(profile, note):
    """Remember an inconclusive profile for this process only, so its domain is probed again on the next run."""
    profile["note"] = note[:120]
    _unsettled[profile["domain"]] = profile
    return dict(profile)


def domain_profile(conn, domain, force=False):
    row = conn.execute("SELECT * FROM smtp_domains WHERE domain=?", (domain,)).fetchone()
    fresh = (dt.datetime.now() - dt.timedelta(days=config.DOMAIN_CACHE_DAYS)).isoformat()
    if row and not force and (row["checked_at"] or "") > fresh and row["port25_ok"] is not None:
        return dict(row)
    if domain in _unsettled and not force:
        return dict(_unsettled[domain])
    hosts = mx_hosts(domain)
    profile = {"domain": domain, "mx": hosts[0] if hosts else None, "port25_ok": None, "catch_all": None,
               "checked_at": state.now(), "note": ""}
    if not hosts:
        if domain in _mx_failed:
            return _unsettle(profile, "unknown, not cached: MX lookup failed on our side")
        profile.update(port25_ok=0, note="no MX")
        _save_profile(conn, profile)
        return profile
    random_addr = f"zq{secrets.token_hex(5)}@{domain}"
    rec = reacher_check(random_addr) if reacher_available() else None
    if rec is None:
        rec = smtp_probe(random_addr, hosts[0])
        rec.setdefault("catch_all", None)
    if rec["result"] == "valid":
        profile.update(port25_ok=1, catch_all=1, note=f"catch-all ({rec.get('engine', 'smtp')})")
    elif rec["result"] == "invalid":
        profile.update(port25_ok=1, catch_all=0, note=f"rejects unknown users ({rec.get('engine', 'smtp')})")
    elif rec.get("catch_all") is not None:
        profile.update(port25_ok=1, catch_all=1 if rec["catch_all"] else 0, note=rec["message"][:120])
    elif rec["result"] == "blocked":
        profile.update(port25_ok=0, note=f"our IP is rejected by policy: {rec['message'][:100]}")
    elif rec["result"] == "throttled":
        profile.update(port25_ok=None, note=f"throttled, retry later: {rec['message'][:100]}")
        _save_profile(conn, profile)
        return _unsettle(profile, profile["note"])
    elif rec.get("code") is None:
        return _unsettle(profile, f"unknown, not cached: {rec['message'][:100]}")
    else:
        profile.update(port25_ok=0, note=rec["message"][:120])
    _save_profile(conn, profile)
    return profile


def verify_email(conn, email, profile):
    row = conn.execute("SELECT * FROM verifications WHERE email=?", (email,)).fetchone()
    fresh = (dt.datetime.now() - dt.timedelta(days=config.SMTP_CACHE_DAYS)).isoformat()
    if row and (row["checked_at"] or "") > fresh and row["result"] in ("valid", "invalid"):
        return dict(row)
    if not profile.get("mx") or not profile.get("port25_ok"):
        return {"email": email, "result": "unknown", "code": None, "message": profile.get("note") or "no reachable MX",
                "checked_at": state.now()}
    rec = reacher_check(email) if reacher_available() else None
    if rec is None:
        rec = smtp_probe(email, profile["mx"])
    saved = {"email": email, "result": rec["result"], "code": rec.get("code"), "message": rec.get("message", ""),
             "checked_at": state.now()}
    conn.execute("""INSERT INTO verifications(email,result,code,message,checked_at) VALUES(?,?,?,?,?)
                    ON CONFLICT(email) DO UPDATE SET result=excluded.result, code=excluded.code,
                    message=excluded.message, checked_at=excluded.checked_at""",
                 (email, saved["result"], saved["code"], saved["message"], saved["checked_at"]))
    return saved


def purge_blocked(conn):
    """Forget verdicts that were really policy rejections of our IP, reset those domain profiles, and drop profiles
    an older build cached as unusable after a failure on our side."""
    params = [f"%{w}%" for w in BLOCK_WORDS]
    like_msg = " OR ".join(["lower(message) LIKE ?"] * len(BLOCK_WORDS))
    like_note = " OR ".join(["lower(note) LIKE ?"] * len(BLOCK_WORDS))
    like_local = " OR ".join(["lower(note) LIKE ?"] * len(LOCAL_WORDS))
    n = conn.execute(f"DELETE FROM verifications WHERE result='invalid' AND ({like_msg})", params).rowcount
    m = conn.execute(f"DELETE FROM smtp_domains WHERE ({like_note}) OR port25_ok IS NULL OR (port25_ok=0 AND ({like_local}))",
                     params + [f"%{w}%" for w in LOCAL_WORDS]).rowcount
    return n, m


def egress_reputation():
    """Spamhaus ZEN lookup of our public IP: 127.0.0.10/11 = PBL (residential policy list)."""
    try:
        ip = subprocess.run(["curl", "-s", "-m", "8", "https://api.ipify.org"], capture_output=True, text=True, timeout=12).stdout.strip()
        if not re.fullmatch(r"\d+\.\d+\.\d+\.\d+", ip):
            return {"ip": None, "listed": None, "codes": []}
        rev = ".".join(reversed(ip.split(".")))
        out = subprocess.run(["dig", "+short", "+time=4", f"{rev}.zen.spamhaus.org"], capture_output=True, text=True, timeout=10).stdout.split()
        return {"ip": ip, "listed": bool(out), "codes": out}
    except (OSError, subprocess.TimeoutExpired):
        return {"ip": None, "listed": None, "codes": []}
