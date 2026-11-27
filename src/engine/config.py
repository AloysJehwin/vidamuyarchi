import os
from pathlib import Path

HERE = Path(__file__).resolve().parent
ENV_FILE = HERE / ".env"
DB_PATH = HERE / "state.db"
# Personal values live in private_profile.py next to this file (gitignored, copied by deploy). Without it the
# placeholders below apply and preflight fails, so nothing is sent with the example copy.
try:
    import private_profile as _P
    PRIVATE_PROFILE_LOADED = True
except ImportError:
    _P = None
    PRIVATE_PROFILE_LOADED = False


def _private(name, default):
    return getattr(_P, name, default) if _P else default


RESUME = HERE / _private("RESUME_FILE", "resume.pdf")
CSV_EXPORT = HERE / "contacted_log.csv"
PATTERNS_CSV = HERE / "patterns.csv"
LOG_FILE = HERE / "pipeline.log"

TRUTHY = {"1", "true", "yes", "on"}


def _load_env_file():
    if not ENV_FILE.exists():
        return
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_env_file()


def env(name, default):
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    if isinstance(default, bool):
        return raw.strip().lower() in TRUTHY
    if isinstance(default, int):
        try:
            return int(raw)
        except ValueError:
            return default
    if isinstance(default, float):
        try:
            return float(raw)
        except ValueError:
            return default
    return raw.strip()


def env_list(name, default):
    items = [x.strip().lower() for x in env(name, "").split(",") if x.strip()]
    return items or [str(x).lower() for x in default]


def _companies_csv():
    raw = env("OUTREACH_COMPANIES_CSV", "")
    if raw:
        return Path(raw).expanduser()
    local = HERE / "companies.csv"
    return local if local.exists() else HERE.parent.parent / "config" / "companies.csv"


API_KEY = env("EXPLORIUM_API_KEY", "")
EXPLORIUM_ENABLED = env("OUTREACH_EXPLORIUM", False)
SELF_EMAIL = env("OUTREACH_SELF_EMAIL", _private("SELF_EMAIL", "you@example.com"))
BCC_SELF = env("OUTREACH_BCC_SELF", False)
ALLOW_PERSONAL_EMAIL = env("OUTREACH_ALLOW_PERSONAL_EMAIL", False)
ALLOW_CATCH_ALL = env("OUTREACH_ALLOW_CATCH_ALL", True)
MX_CHECK = env("OUTREACH_MX_CHECK", True)

COMPANIES_CSV = _companies_csv()
LINKEDIN_DB = Path(env("OUTREACH_LINKEDIN_DB", str(Path.home() / "linkedin_outreach.db"))).expanduser()
EXCLUDED_COMPANIES = frozenset(" ".join(x.split()) for x in
                               env_list("OUTREACH_EXCLUDED_COMPANIES", _private("EXCLUDED_COMPANIES", ())))

MAX_PER_DAY = env("OUTREACH_MAX_PER_DAY", 50)
MAX_FOLLOWUPS_PER_DAY = env("OUTREACH_MAX_FOLLOWUPS_PER_DAY", 25)
BREAKER_BOUNCES_24H = env("OUTREACH_BREAKER_BOUNCES_24H", 2)
BREAKER_BOUNCES_PER_100 = env("OUTREACH_BREAKER_BOUNCES_PER_100", 3)
BREAKER_WINDOW_DAYS = env("OUTREACH_BREAKER_WINDOW_DAYS", 14)
# supply.py: first-touch sends the verified backlog should support each day, and the verify rate assumed for a
# company until enough of its imports have had three nights of probing
SUPPLY_TARGET = env("OUTREACH_SUPPLY_TARGET", 25)
SUPPLY_STALE_HOURS = env("OUTREACH_SUPPLY_STALE_HOURS", 36)
DEFAULT_VERIFY_RATE = env("OUTREACH_DEFAULT_VERIFY_RATE", 0.2)
UNVERIFIED_PATTERN_SENDS = env("OUTREACH_UNVERIFIED_PATTERN_SENDS", False)
PATTERN_BREAKER_DAYS = env("OUTREACH_PATTERN_BREAKER_DAYS", 14)
REVERIFY_AFTER_HOURS = env("OUTREACH_REVERIFY_AFTER_HOURS", 20)
MAX_CREDITS_PER_RUN = env("OUTREACH_MAX_CREDITS_PER_RUN", 60)
CREDIT_RESERVE = env("OUTREACH_CREDIT_RESERVE", 30)
CREDITS_PER_CONTACT = 3
ENRICH_BATCH = 50
DISCOVER_OVERSAMPLE = env("OUTREACH_DISCOVER_OVERSAMPLE", 1.25)
DISCOVER_MAX_COMPANIES = env("OUTREACH_DISCOVER_MAX_COMPANIES", 60)
DISCOVER_TIME_BUDGET_S = env("OUTREACH_DISCOVER_TIME_BUDGET_S", 230)
SEARCH_PAGE_SIZE = 10

FOLLOWUP_BUSINESS_DAYS = env("OUTREACH_FOLLOWUP_BUSINESS_DAYS", 5)
COMPANY_COOLDOWN_DAYS = env("OUTREACH_COMPANY_COOLDOWN_DAYS", 90)
COMPANY_RESEARCH_DAYS = env("OUTREACH_COMPANY_RESEARCH_DAYS", 14)
CLAIM_STALE_MINUTES = 30
SEND_DAYS = {int(d) for d in env_list("OUTREACH_SEND_DAYS", range(7)) if d.isdigit() and int(d) <= 6} or set(range(7))

MIN_RESUME_BYTES = 10_000
MIN_DISK_FREE_MB = 500

ALLOW_PAID_REVEAL = env("OUTREACH_ALLOW_PAID_REVEAL", False)
SEARCH_MODE = env("OUTREACH_SEARCH_MODE", "preview")
ALLOW_SEARCH_CREDITS = env("OUTREACH_ALLOW_SEARCH_CREDITS", False)
MIN_CONFIDENCE = env("OUTREACH_MIN_CONFIDENCE", 0.8)
ALLOW_CATCH_ALL_OSINT = env("OUTREACH_ALLOW_CATCH_ALL_OSINT", False)

TOOLS_BIN = Path(env("OUTREACH_TOOLS_BIN", str(HERE / "bin")))
REACHER_BIN = TOOLS_BIN / "check_if_email_exists"
REACHER_TIMEOUT_S = env("OUTREACH_REACHER_TIMEOUT_S", 45)
OSINT_ENABLED = env("OUTREACH_OSINT", True)
OSINT_DIR = HERE / "osint"
OSINT_SOURCES = env("OUTREACH_OSINT_SOURCES", "emails")
OSINT_TIMEOUT_S = env("OUTREACH_OSINT_TIMEOUT_S", 180)
OSINT_MIN_EXAMPLES = env("OUTREACH_OSINT_MIN_EXAMPLES", 3)
INBOX_DIR = HERE / "inbox"
SHEET_ID = env("OUTREACH_SHEET_ID", _private("SHEET_ID", ""))

SMTP_ENABLED = env("OUTREACH_SMTP_VERIFY", True)
SMTP_FROM = env("OUTREACH_SMTP_FROM", _private("SMTP_FROM", "you@example.com"))
SMTP_HELO = env("OUTREACH_SMTP_HELO", _private("SMTP_HELO", "example.com"))
SMTP_TIMEOUT = env("OUTREACH_SMTP_TIMEOUT", 15)
SMTP_MIN_INTERVAL_S = env("OUTREACH_SMTP_MIN_INTERVAL_S", 2.0)
SMTP_MAX_PROBES_PER_RUN = env("OUTREACH_SMTP_MAX_PROBES_PER_RUN", 80)
SMTP_MAX_PROBES_PER_CONTACT = env("OUTREACH_SMTP_MAX_PROBES_PER_CONTACT", 4)
# addresses a contact may have rejected in total across re-checks; each re-check probes the next untried patterns
SMTP_LADDER_DEPTH = env("OUTREACH_SMTP_LADDER_DEPTH", 12)
SMTP_LADDER_DEPTH_UNCONFIRMED = env("OUTREACH_SMTP_LADDER_DEPTH_UNCONFIRMED", 6)
SMTP_CACHE_DAYS = 14
DOMAIN_CACHE_DAYS = 30
SMTP_PROXY_HOST = env("OUTREACH_SMTP_PROXY_HOST", "")
SMTP_PROXY_PORT = env("OUTREACH_SMTP_PROXY_PORT", 1080)
SMTP_PROXY_USER = env("OUTREACH_SMTP_PROXY_USER", "")
SMTP_PROXY_PASS = env("OUTREACH_SMTP_PROXY_PASS", "")

ROLE_LOCAL_PARTS = {
    "careers", "jobs", "job", "hr", "noreply", "no-reply", "donotreply", "info",
    "support", "admin", "recruiting", "recruitment", "talent", "hello", "contact",
    "team", "sales", "marketing", "press", "media", "help", "office", "mail",
}
FREE_MAIL_DOMAINS = {
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.co.in", "outlook.com",
    "hotmail.com", "live.com", "icloud.com", "me.com", "protonmail.com",
    "proton.me", "rediffmail.com", "aol.com", "ymail.com", "zoho.com",
}
COMMON_SURNAMES = frozenset(env_list("OUTREACH_COMMON_SURNAMES", (
    "kumar", "singh", "sharma", "reddy", "patel", "gupta", "shah", "das", "rao", "nair", "iyer", "khan", "joshi",
    "mehta", "jain", "agarwal", "verma", "yadav", "mishra", "pandey", "chauhan", "thakur", "naidu", "pillai", "menon",
    "krishnan", "raj", "ali", "ahmed", "kumari", "devi", "prasad", "sinha", "srivastava", "tiwari", "dubey", "tripathi",
    "shukla", "pathak", "chaudhary", "choudhary", "saxena", "malhotra", "kapoor", "khanna", "arora", "bhatia", "sethi",
    "garg", "goyal", "bansal", "mittal", "aggarwal", "agrawal", "jha", "roy", "ghosh", "bose", "sen", "dutta",
    "banerjee", "mukherjee", "chatterjee", "chakraborty", "bhattacharya", "desai", "patil", "kulkarni", "deshpande",
    "shetty", "hegde", "bhat", "gowda", "babu", "murthy", "iyengar", "raman", "subramanian", "krishna", "rajan",
    "srinivasan", "venkatesh", "thomas", "george", "joseph", "mathew", "varghese", "john", "jacob", "mohammed",
    "mohammad", "hussain", "sheikh", "syed", "ansari", "siddiqui", "rahman", "gill", "sandhu", "rana", "rawat",
    "chopra", "bhatt", "trivedi", "modi", "soni", "rathore", "jadhav", "pawar", "shinde", "naik", "nayak",
    "mohanty", "sahoo", "panda", "mandal", "biswas", "sarkar", "paul", "saha", "chowdhury", "raju", "varma",
)))
