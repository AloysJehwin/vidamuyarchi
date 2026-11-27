"""Target companies come from the shared registry config/companies.csv (config.COMPANIES_CSV); exclude=1 rows are
never targeted. Tier drives the email variant: A = top tier, B = mid/enterprise/product, C = AI.
People already contacted stay excluded person by person (EXCLUDED_PEOPLE, CONTACTED_SEED)."""
import csv
import re
import unicodedata

import config
import resolver

FIELDS = ("key", "name", "tier", "linkedin_name", "aliases", "email_domains", "mx", "verifiable", "size",
          "daily_cap", "monthly_cap", "india_share", "linkedin_company_id", "exclude")
LEGAL_SUFFIXES = {"pvt", "private", "ltd", "limited", "inc", "llc", "llp", "plc", "gmbh", "corp", "corporation", "co"}

JOB_TITLES = [
    "technical recruiter",
    "senior technical recruiter",
    "engineering recruiter",
    "talent acquisition",
    "talent acquisition recruiter",
    "recruiting",
]

# People already contacted by hand (names + real addresses) live in private_profile.py (gitignored).
EXCLUDED_PEOPLE = set(config._private("EXCLUDED_PEOPLE", set()))
CONTACTED_SEED = list(config._private("CONTACTED_SEED", []))


class RegistryError(RuntimeError):
    pass


def words(text):
    plain = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode().lower()
    toks = re.sub(r"[^a-z0-9]+", " ", plain).split()
    while len(toks) > 1 and toks[-1] in LEGAL_SUFFIXES:
        toks.pop()
    return " ".join(toks)


def norm(text):
    return words(text).replace(" ", "")


def _row(raw, where):
    r = {k: (raw.get(k) or "").strip() for k in FIELDS}
    try:
        row = {**r, "key": r["key"].lower(), "tier": r["tier"].upper(),
               "aliases": [a.strip() for a in r["aliases"].split("|") if a.strip()],
               "email_domains": [d.strip().lower() for d in r["email_domains"].split("|") if d.strip()],
               "daily_cap": int(r["daily_cap"] or 0), "monthly_cap": int(r["monthly_cap"] or 0),
               "india_share": float(r["india_share"] or 0), "exclude": int(r["exclude"] or 0)}
    except ValueError as e:
        raise RegistryError(f"{where}: {e}") from None
    if not re.fullmatch(r"[a-z0-9-]+", row["key"]) or not row["name"]:
        raise RegistryError(f"{where}: key must match [a-z0-9-]+ and name must be set")
    if not row["exclude"] and (row["tier"] not in ("A", "B", "C") or row["daily_cap"] < 0 or row["monthly_cap"] < 0
                               or not 0 <= row["india_share"] <= 1):
        raise RegistryError(f"{where}: tier must be A, B or C, caps >= 0 and india_share within 0..1")
    # optional columns, read outside FIELDS so a registry without them still loads: harvest=0 keeps a company out of
    # LinkedIn harvesting (email only); template picks an alternative email copy
    row["harvest"] = (raw.get("harvest") or "").strip() or "1"
    row["template"] = (raw.get("template") or "").strip().upper()
    if row["harvest"] not in ("0", "1"):
        raise RegistryError(f"{where}: harvest must be 0 or 1")
    return row


def load(path):
    try:
        with open(path, newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            missing = [c for c in FIELDS if c not in (reader.fieldnames or [])]
            if missing:
                raise RegistryError(f"{path}: missing columns {', '.join(missing)}")
            rows = [_row(raw, f"{path}:{line}") for line, raw in enumerate(reader, 2)]
    except (OSError, UnicodeDecodeError, csv.Error) as e:
        raise RegistryError(f"cannot read company registry {path}: {e} (set OUTREACH_COMPANIES_CSV)") from None
    active, excluded = {}, {}
    for row in rows:
        if row["key"] in active or row["key"] in excluded:
            raise RegistryError(f"{path}: duplicate key {row['key']}")
        (excluded if row["exclude"] else active)[row["key"]] = row
    if not active:
        raise RegistryError(f"{path}: no active companies")
    return active, excluded


try:
    REGISTRY, EXCLUDED_REGISTRY = load(config.COMPANIES_CSV)
    ERROR = None
except RegistryError as e:
    REGISTRY, EXCLUDED_REGISTRY, ERROR = {}, {}, str(e)


def require():
    if ERROR:
        raise RegistryError(ERROR)


def lint(rows=None):
    """Problems that load() accepts but that silently hurt sending: a 0 cap blocks a company outright
    (sending.company_caps), and a label or mail domain shared by two companies makes lookup() pick one of them."""
    rows = list((rows if rows is not None else REGISTRY).values())
    problems, labels, domains = [], {}, {}
    for r in rows:
        if r["daily_cap"] == 0 or r["monthly_cap"] == 0:
            problems.append(f"{r['key']}: daily_cap {r['daily_cap']} / monthly_cap {r['monthly_cap']} blocks every send")
        if not r["email_domains"]:
            problems.append(f"{r['key']}: no email_domains, nothing can be verified")
        for label in {norm(x) for x in (r["key"], r["name"], r["linkedin_name"], *r["aliases"]) if norm(x)}:
            if labels.setdefault(label, r["key"]) != r["key"]:
                problems.append(f"{r['key']}: label '{label}' also belongs to {labels[label]}")
        for d in r["email_domains"]:
            if domains.setdefault(d, r["key"]) != r["key"]:
                problems.append(f"{r['key']}: email domain {d} also belongs to {domains[d]}")
    return problems


def _index(rows):
    out = {}
    for r in rows:
        for label in (r["key"], r["name"], r["linkedin_name"], *r["aliases"]):
            if norm(label):
                out.setdefault(norm(label), r["key"])
    return out


_INDEX = _index(REGISTRY.values())
_EXCLUDED_INDEX = _index(EXCLUDED_REGISTRY.values())
_EXCLUDED_DOMAINS = {d for r in EXCLUDED_REGISTRY.values() for d in r["email_domains"]}
_EXCLUDED_WORDS = {words(c) for c in config.EXCLUDED_COMPANIES} - {""}
_EXCLUDED_TOKENS = {w.replace(" ", "") for w in _EXCLUDED_WORDS}

COMPANIES = {r["name"]: ("", r["tier"]) for r in REGISTRY.values()}
DOMAINS = {r["name"]: list(r["email_domains"]) for r in REGISTRY.values()}
ALIASES = {r["name"]: sorted({a.lower() for a in (r["linkedin_name"], *r["aliases"]) if a and a.lower() != r["name"].lower()})
           for r in REGISTRY.values()}


def lookup(text):
    """Registry row for a company key, name, LinkedIn name or alias (case and punctuation insensitive), else None."""
    return REGISTRY.get(_INDEX.get(norm(text), ""))


def company_key(name):
    row = lookup(name)
    return row["key"] if row else "~" + norm(name)


def excluded_company(name="", email=""):
    w = words(name)
    if w and (norm(name) in _EXCLUDED_INDEX or any(w == e or w.startswith(e + " ") for e in _EXCLUDED_WORDS)):
        return True
    domain = (email or "").strip().lower().rpartition("@")[2]
    return bool(domain) and (domain in _EXCLUDED_DOMAINS or any(p in _EXCLUDED_TOKENS for p in domain.split(".")[:-1]))


def person_key(full_name):
    return "".join(resolver.clean_name(full_name)[0])


_EXCLUDED_PEOPLE_KEYS = {person_key(n) for n in EXCLUDED_PEOPLE}


def excluded_person(full_name):
    return person_key(full_name) in _EXCLUDED_PEOPLE_KEYS
