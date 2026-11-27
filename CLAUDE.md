# vidamuyarchi

Personal job-search outreach system. Two halves, both deployed to the Razer box (SSH alias `razer-gpu`, reached over Tailscale):

1. **Email engine** (`src/engine/`) — resolves name + company to a verified work address without paid reveals, sends via Gmail through n8n.
2. **LinkedIn automation** (`src/linkedin/linkedin_outreach.py`) — Selenium/undetected-chromedriver: harvest, connect, message, feed engagement.

This folder is **not a git repo** and has **no tests**. The live `state.db`, `.env`, logs and LinkedIn DB exist only on the Razer — nothing here shows real run data.

## Layout

| Path | What |
|---|---|
| `src/engine/pipeline.py` | CLI entry point; `sending.py` registers the send-side commands via `HANDLERS` |
| `src/engine/engine.py` | Address resolution: evidence ladder + namesake guard |
| `src/engine/resolver.py` | Name splitting, candidate patterns, pattern learning, Explorium hash match |
| `src/engine/verify.py` | Free mailbox verification (reacher binary, else raw SMTP RCPT; optional SOCKS5) |
| `src/engine/sending.py` | compose / claim / record / followups / bounce, caps and bounce breaker |
| `src/engine/supply.py` | Backlog forecast; per-company verifiability that steers LinkedIn harvesting |
| `src/engine/state.py` | SQLite schema + helpers |
| `src/engine/config.py` | Every setting, as `OUTREACH_*` env vars (also read from `src/engine/.env`) |
| `src/engine/templates.py` | Email copy by tier (A top, B mid/enterprise, C AI); neutral examples, real copy comes from `private_profile.py` |
| `src/engine/private_profile.py` | **Gitignored.** Real copy, signature, sender, resume filename, excluded companies, sheet ID |
| `src/linkedin/linkedin_private.py` | **Gitignored.** LinkedIn notes/follow-ups, home city, Sheets ID |
| `config/companies.csv` | Target-company registry: tier, domains, MX provider, verifiable, daily/monthly caps, exclude |
| `src/n8n/build_workflows.py` | **Generates** `src/n8n/workflows/*.json` |
| `scripts/deploy.sh` | One-shot deploy from the Mac |
| `scripts/linkedin_daily.sh` | Cron runner for the LinkedIn phases |
| `config/private/` | Gitignored; the resume PDF and `n8n.json` (approver email, sheet ID, server path) |

## Conventions

- **Engine is stdlib-only Python** (plus the `check_if_email_exists` binary and OSINT tools from `install_tools.sh`). Don't add pip dependencies to it.
- Modules import each other flat (`import config`) — the engine runs from its own directory (`~/outreach` on the Razer).
- **Every `pipeline.py` command prints exactly one JSON object on stdout**; exit 0 for controlled outcomes (check `"ok"`), exit 2 for crashes. n8n parses the last stdout line — never print anything else to stdout; use `log()` (stderr/file).
- **Never edit `src/n8n/workflows/*.json` by hand** — edit `build_workflows.py` and regenerate (`python3 src/n8n/build_workflows.py`; deploy does this too).
- New settings go in `config.py` via `env(...)` with a safe default; risky behaviours default off (`UNVERIFIED_PATTERN_SENDS`, `ALLOW_PAID_REVEAL`, `ALLOW_CATCH_ALL_OSINT`, `EXPLORIUM_ENABLED`).
- Email copy: every claim must trace to the resume, GitHub, or the user's live projects. Nothing invented.
- **Nothing personal in tracked files** (the repo is on GitHub): names, employer, emails, phone, sheet IDs, home paths and IPs go in the gitignored private files above; code reads them with neutral fallbacks. `src/n8n/workflows/*.json` are generated with private values, so they are gitignored too.

## Invariants — don't weaken without being asked

- Only addresses with confidence ≥ `MIN_CONFIDENCE` (0.8) are sent. Pattern guesses never send, except the opt-in `UNVERIFIED_PATTERN_SENDS` path.
- Namesake guard: formats that drop the surname (`first@`, `first.linitial@`, …) only count when that domain is known to use them; initial-only surnames cap below the send threshold.
- Preflight fails when `private_profile.py` is missing, so the placeholder copy is never sent.
- Only identity-confirmed hits (`hash_verified`, `smtp_valid`) teach a domain's pattern.
- Caps: 50 first touches/day, 25 follow-ups/day, per-company daily/monthly caps from the CSV, bounce breaker, the user's employer excluded (`EXCLUDED_COMPANIES` in `private_profile.py`), one follow-up per person ever, no send retries.
- LinkedIn account safety: 15 invites/day, 90/rolling week, send hours 08–22. The Python script enforces these; the shell runner only sequences phases.
- `deploy.sh` imports workflows **inactive** and never activates them; the user activates by hand in the n8n editor.

## Schedules (IST)

n8n: Intake & Verify every 20 min 19:00–23:40 · Find & Send 09:30 (approval email before sending) · Follow-ups 14:00 · Bounce Scan 21:00.
Cron (Razer): LinkedIn full run 09:03, harvest-only 15:40 (under `xvfb-run`, `HEADED=1`).

## Deploy & run

```bash
bash scripts/deploy.sh              # needs Tailscale up; prints cron lines
bash scripts/deploy.sh --install-cron
ssh razer-gpu 'cd ~/outreach && python3 pipeline.py preflight'
ssh razer-gpu 'cd ~/outreach && python3 pipeline.py status'
```

Deploying, running sends, or touching the Razer crontab/n8n affects real outreach — confirm with the user first. Syntax-check locally with `python3 -m py_compile src/engine/*.py`.

## Razer gotchas (not in repo)

- n8n 2.x needs `~/outreach` (absolute path) allowed in `N8N_RESTRICT_FILE_ACCESS_TO` (a systemd drop-in) to write `~/outreach/inbox`.
- n8n counts waiting time (approval, send gaps) toward the execution timeout, so Find & Send and Follow-ups set `executionTimeout = -1`.
- n8n CLI lives in `~/.npm-global/bin`.
- About half the target domains are catch-all; Adobe/Microsoft are alias-based (verifiable but not guessable).
- Only companies whose mail the SMTP check can verify yield sendable addresses (catch-all/blocked ones gave 0 of 150+), so those carry `harvest=0`. Screen new companies with `pipeline.py profile-domains --candidates <csv>` and add only `checkable` rows.
- Chrome once bloated `~/.linkedin_outreach_chrome/Default/Preferences` to 2.4 GB and crashed every launch for three weeks (harvest stopped, sends fell to 0). `linkedin_outreach.py` now deletes it when >50 MB before launch.
