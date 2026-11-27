"""Email resolver: learn a company's address pattern, build candidates from a name, and accept a
candidate only when Explorium's hashed professional email confirms it. Pattern alone never sends
(except through engine.py's opt-in OUTREACH_UNVERIFIED_PATTERN_SENDS)."""
import hashlib
import re
import unicodedata
from urllib.parse import urlparse

import config

PATTERNS = [
    "first.last", "finitial+last", "first+linitial", "linitial+first", "first_last", "firstlast",
    "first-last", "finitial.last", "first.linitial", "linitial.first", "first", "last",
    "last.first", "last+first", "last+finitial", "first.middle.last", "firstmiddlelast",
    "finitial+minitial+last", "first.middle+last",
]
INITIAL_PATTERNS = ["firstlast", "first.last", "last+first", "last.first", "first+linitial", "first.linitial",
                    "linitial+first", "linitial.first", "first_last", "first-last"]

HASH_BY_LEN = {32: hashlib.md5, 40: hashlib.sha1, 64: hashlib.sha256}
SUFFIXES = {"mba", "phd", "jr", "sr", "ii", "iii", "iv", "cpa", "pmp", "msc", "ms", "ma", "mphil", "cfa", "esq", "md",
            "dr", "mr", "mrs", "miss", "prof", "sir", "shri", "smt", "er", "engr", "csm", "cspo", "shrm", "shrmcp",
            "shrmscp", "sphr", "phr", "cipd", "ca", "acca", "pgdm", "mca", "bsc", "btech", "mtech"}
NOT_NICKNAMES = {"she", "her", "he", "him", "they", "them", "hiring", "recruiting", "recruiter"}
VOWELS = set("aeiou")
CUT_RE = re.compile(r"\s*[|•·–—]|\s-(?=\s|$)")
BRACKET_RE = re.compile(r"\(([^()]*)\)?|\[([^\[\]]*)\]?|[\"“”]([^\"“”]*)[\"“”]"
                        r"|(?<![A-Za-z])['‘’]([A-Za-z]+)['‘’](?![A-Za-z])")


def normalize(token):
    text = unicodedata.normalize("NFKD", token or "").encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z]", "", text.lower())


def _parts(name):
    """([(token, is_initial)], nickname) with honorifics, credentials, pronoun tags, emojis and anything after '|' or
    ' - ' dropped. Single letters and dotted or vowel-less 2-3 letter groups ('K.R.', 'KR') are initials."""
    text = CUT_RE.split(name or "", maxsplit=1)[0]
    nickname = ""
    for m in BRACKET_RE.finditer(text):
        inner = next((g for g in m.groups() if g is not None), "").strip(" .")
        if not nickname and re.fullmatch(r"[A-Za-z]{2,20}", inner) and normalize(inner) not in SUFFIXES | NOT_NICKNAMES:
            nickname = normalize(inner)
    text = BRACKET_RE.sub(" ", text).split(",")[0]
    text = "".join(" " if unicodedata.category(ch)[0] in "SC" else ch for ch in text)
    tokens = []
    for raw in text.split():
        if "/" in raw or raw[0] in "#@" or normalize(raw) in SUFFIXES:
            continue
        for part in raw.split("."):
            n = normalize(part)
            if not n or n in SUFFIXES:
                continue
            if len(n) == 1 or (len(n) <= 3 and not VOWELS & set(n)):
                tokens.extend((ch, True) for ch in n)
            else:
                tokens.append((n, False))
    return tokens, nickname


def clean_name(full_name):
    tokens, nickname = _parts(full_name)
    return [t for t, _ in tokens], nickname


def _split(full_name=None, first_name=None, last_name=None):
    """(first, middle, last, initial_surname). With no full-word surname the initials are the surname and the full
    word is the first name ('R. Karthik', 'Karthik R', 'K R Ramesh' -> karthik/r, karthik/r, ramesh/kr)."""
    tokens, _ = _parts(full_name)
    words = [t for t, ini in tokens if not ini]
    initials = "".join(t for t, ini in tokens if ini)
    given = "".join(t for t, ini in _parts(first_name)[0] if not ini) if first_name else ""
    family_tokens = _parts(last_name)[0] if last_name else []
    family = [t for t, ini in family_tokens if not ini]
    if not given and family == words and len(words) == 1:
        family, family_tokens = [], []
    if not family and family_tokens and len(words) >= 2:
        family_tokens = []
    first = given or (words[0] if words else "")
    if not family and (len(words) < 2 or family_tokens):
        last = initials or "".join(t for t, _ in family_tokens)
        return first, "".join(w for w in words if w != first), last, bool(last)
    family = family or words[-1:]
    names = [t for t, _ in tokens]
    start = names.index(first) if first in names else (names.index(words[0]) if words else 0)
    if family[-1] in names:
        between = tokens[start + 1:len(names) - 1 - names[::-1].index(family[-1])]
    else:
        between = [(t, ini) for t, ini in tokens[start + 1:] if not ini]
    return first, "".join(t for t, _ in between if t not in family), "".join(family), False


def split_name(full_name=None, first_name=None, last_name=None):
    first, middle, last, _ = _split(full_name, first_name, last_name)
    return first, middle, last


def split_person_name(full_name, first_name=None, last_name=None):
    """{'first', 'last', 'initials'}: 'last' is '' when the surname is missing or only initials."""
    first, _, last, initial = _split(full_name, first_name, last_name)
    initials = last if initial else "".join(t for t, ini in _parts(full_name)[0] if ini)
    return {"first": first, "last": "" if initial else last, "initials": initials}


def apply_pattern(pattern, first, middle, last):
    f, m, l = first, middle, last
    fi, mi, li = f[:1], m[:1], l[:1]
    if not f or (pattern not in ("first",) and not l):
        return None
    if "middle" in pattern or "minitial" in pattern:
        if not m:
            return None
    table = {
        "first.last": f"{f}.{l}", "finitial+last": f"{fi}{l}", "first+linitial": f"{f}{li}",
        "linitial+first": f"{li}{f}", "first_last": f"{f}_{l}", "firstlast": f"{f}{l}",
        "first-last": f"{f}-{l}", "finitial.last": f"{fi}.{l}", "first.linitial": f"{f}.{li}",
        "linitial.first": f"{li}.{f}", "first": f, "last": l, "last.first": f"{l}.{f}",
        "last+first": f"{l}{f}", "last+finitial": f"{l}{fi}", "first.middle.last": f"{f}.{m}.{l}",
        "firstmiddlelast": f"{f}{m}{l}", "finitial+minitial+last": f"{fi}{mi}{l}",
        "first.middle+last": f"{f}.{m}{l}",
    }
    return table.get(pattern)


def _splits(full_name, first_name=None, last_name=None):
    first, middle, last, initial = _split(full_name, first_name, last_name)
    kind = "initial" if initial else "full" if last else "single"
    splits = [(first, middle, last, kind)]
    if middle and kind == "full":
        splits.append((first, "", middle + last, kind))
        splits.append((first, "", middle, kind))
    _, nickname = _parts(full_name)
    if nickname and nickname != first:
        splits.append((nickname, middle, last, kind))
    return [s for s in dict.fromkeys(splits) if s[0]]


def name_splits(full_name, first_name=None, last_name=None):
    return [s[:3] for s in _splits(full_name, first_name, last_name)]


def candidates(full_name, domain, first_name=None, last_name=None, preferred=None, safe=True, prior=None):
    """[(email, pattern)] in trial order: name readings in order, and within each the domain's preferred pattern,
    then by `prior` ({pattern: score}, what verifies elsewhere), then the fixed PATTERNS order. With safe=True an
    initials-only surname gets only INITIAL_PATTERNS and a single-word name gets nothing, unless the domain's learned
    pattern is 'first'; hash matching passes safe=False."""
    out, prior = [], prior or {}
    for first, middle, last, kind in _splits(full_name, first_name, last_name):
        allowed = PATTERNS if kind == "full" or not safe else INITIAL_PATTERNS if kind == "initial" else []
        if safe and kind != "full" and preferred == "first":
            allowed = ["first", *allowed]
        order = ([preferred] if preferred in allowed else []) + sorted((p for p in allowed if p != preferred),
                                                                       key=lambda p: -prior.get(p, 0))
        for pattern in order:
            local = apply_pattern(pattern, first, middle, last)
            if local and len(local) >= 2:
                out.append((f"{local}@{domain}", pattern))
    seen, uniq = set(), []
    for email, pattern in out:
        if email not in seen:
            seen.add(email)
            uniq.append((email, pattern))
    return uniq


def infer_pattern(full_name, email, first_name=None, last_name=None):
    local = email.split("@", 1)[0].lower()
    for first, middle, last in name_splits(full_name, first_name, last_name):
        for pattern in PATTERNS:
            if apply_pattern(pattern, first, middle, last) == local:
                return pattern
    return None


def hash_matches(email, hashed):
    hashed = (hashed or "").strip().lower()
    algo = HASH_BY_LEN.get(len(hashed))
    if not algo:
        return False
    return algo(email.strip().lower().encode()).hexdigest() == hashed


def resolve(full_name, domain, hashed, first_name=None, last_name=None, preferred=None):
    if not domain or not hashed or len((hashed or "").strip()) not in HASH_BY_LEN:
        return None
    for email, pattern in candidates(full_name, domain, first_name, last_name, preferred, safe=False):
        if hash_matches(email, hashed):
            return {"email": email, "pattern": pattern, "method": "hash_verified",
                    "pattern_known": pattern == preferred}
    return None


def domain_from_website(url):
    if not url:
        return ""
    host = urlparse(url if "://" in url else "http://" + url).hostname or ""
    host = host.lower().strip(".")
    return host[4:] if host.startswith("www.") else host


def company_name_matches(target, api_name, aliases=()):
    a, b = normalize(target), normalize(api_name)
    if not a or not b:
        return True
    if a in b or b in a or a[:6] == b[:6]:
        return True
    return any(normalize(alias) and (normalize(alias) in b or b in normalize(alias)) for alias in aliases)


# Seed patterns proven by real sends; the evidence addresses are real people, so they live in private_profile.py.
KNOWN_PATTERNS = list(config._private("KNOWN_PATTERNS", []))

OUTCOME_SCORE = {"delivered": 6, "explorium": 5, "hash_verified": 5, "smtp_verified": 4, "osint": 3, "seed": 2,
                 "osint_weak": 1, "bounced": 0, "osint_example": 0, "osint_none": 0, "osint_unavailable": 0}
