# -*- coding: utf-8 -*-
"""
templates.py - outreach copy, kept separate from the code so you can edit
wording without touching logic.

The real copy lives in private_profile.py (gitignored, deployed next to this
file). When that module defines SIGNATURE / SUBJECTS / BODIES / FOLLOWUP_*,
those are used; otherwise the neutral examples below apply. Preflight fails
without private_profile.py, so the examples are never sent.

Short, plain, written the way a person types an email: no headings, no
bullet lists, no marketing lines. Every claim must trace to your resume,
GitHub, or your own live projects. Nothing invented.

Tiers (from config/companies.csv):
  A  top tier
  B  mid / product / enterprise
  C  AI

Each tier has a few subjects and a few bodies; compose rotates through them so
a batch of 50 is not 50 identical emails. Placeholders: {first_name}, {company}.
"""

try:
    import private_profile as _P
except ImportError:
    _P = None

SIGNATURE = """Your Name
github.com/your-handle | +00 00000 00000"""

SUBJECTS = {
    "A": [
        "Software engineer roles at {company}",
        "Quick question about engineering roles at {company}",
        "Backend engineer interested in {company}",
    ],
    "B": [
        "Backend roles at {company}?",
        "Software engineer interested in {company}",
        "Quick question about hiring at {company}",
    ],
    "C": [
        "Engineering roles at {company}",
        "Applied LLM engineer interested in {company}",
        "Quick question about roles at {company}",
    ],
}

_EXAMPLE = """Hi {first_name},

I'm a software engineer working on backend systems, and I've shipped a feature used daily by thousands of people. On the side I built and run a small SaaS product end to end.

I'd love to be considered for engineering roles at {company}. If that's not you, could you point me to the right person?

Resume attached.

"""

BODIES = {
    "A": [_EXAMPLE + SIGNATURE, _EXAMPLE + SIGNATURE],
    "B": [_EXAMPLE + SIGNATURE, _EXAMPLE + SIGNATURE],
    "C": [_EXAMPLE + SIGNATURE, _EXAMPLE + SIGNATURE],
}

# Sent in the SAME thread, 5 business days after the original. Once only.
FOLLOWUP_SUBJECT = "Re: {original_subject}"
FOLLOWUP_BODY = """Hi {first_name},

Just bumping this once in case it got buried. If there's no fit at {company} right now, no worries at all.

Thanks,
Your Name"""

if _P is not None:
    SIGNATURE = getattr(_P, "SIGNATURE", SIGNATURE)
    SUBJECTS = getattr(_P, "SUBJECTS", SUBJECTS)
    BODIES = getattr(_P, "BODIES", BODIES)
    FOLLOWUP_SUBJECT = getattr(_P, "FOLLOWUP_SUBJECT", FOLLOWUP_SUBJECT)
    FOLLOWUP_BODY = getattr(_P, "FOLLOWUP_BODY", FOLLOWUP_BODY)


def greeting_name(first_name):
    """A first name fit for 'Hi ...': title-cased, or 'there' when missing or only an initial."""
    name = (first_name or "").strip()
    if len(name.rstrip(".")) < 2:
        return "there"
    return name if any(c.isupper() for c in name) else name.title()


def render(variant, index, first_name, company):
    """(subject, body) for the index-th email of a batch; subjects and bodies rotate independently."""
    variant = variant if variant in BODIES else "B"
    subjects, bodies = SUBJECTS[variant], BODIES[variant]
    fields = {"first_name": greeting_name(first_name), "company": company or "your team"}
    subject = subjects[index % len(subjects)].format(**fields)
    body = bodies[(index // len(subjects)) % len(bodies)].format(**fields)
    return subject, body


def render_followup(first_name, company, original_subject):
    fields = {"first_name": greeting_name(first_name), "company": company or "your team"}
    return FOLLOWUP_SUBJECT.format(original_subject=original_subject), FOLLOWUP_BODY.format(**fields)
