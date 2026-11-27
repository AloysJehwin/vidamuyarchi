# LinkedIn Outreach — Job-Search Operating Procedure (Netherlands)

**Goal:** Land a software engineering job — remote, or at a company on the target list — in the Netherlands, using the `scripts/linkedin_outreach.py` tool to run a warm, referral-driven outreach funnel.

**Core truth (from research):** Referrals are ~30–50% of hires and ~72% of interviews, and referred candidates are ~4× more likely to be hired. Cold applications go into a black hole; **a warm intro from an engineer on the team is the highest-yield path.** This tool exists to *manufacture referrals* — not to spam.

**The funnel:** `found → requested → connected → messaged → replied → developing → referral → interview`. It compounds over **4–6 weeks**, not days.

---

## Golden rules (do not break these)
1. **Never ask for a job/referral in the connection note or first message.** It's the #1 conversion killer. Warmth first, ask later.
2. **Quality > volume.** 15 well-targeted, personalized touches beat 100 generic ones — especially in the direct, low-fluff Dutch market.
3. **Stay under the caps.** ~90/week invites (tool enforces `WEEKLY_LIMIT=90`), ramp gradually. The account is the asset — a restriction ends the whole effort.
4. **Comment, don't just like.** Likes are invisible; substantive comments get you noticed *before* you connect.
5. **Target engineers first**, then hiring managers, then internal recruiters. Skip agency recruiters.
6. **Disclose relocation/sponsorship early** (once in conversation) — Dutch culture rewards directness, and it saves cycles.

---

## Prerequisites (do these BEFORE any outreach)
- **Profile is the landing page.** Outreach drives traffic to your profile; if it's weak, everything leaks. Ensure: clear headline (what you build + "open to SWE roles in NL / relocating"), photo, banner, GitHub/projects, results-oriented experience. English.
- **Set "Open to work"** (recruiters-only visibility if you're currently employed).
- **Confirm target companies sponsor.** Non-EU → you need an **IND recognised-sponsor** employer (kennismigrant visa). Check the IND public register. Most on the list (Adyen, Booking.com, Mollie, bunq, Picnic, etc.) do; verify per company.
- **Reality check on "remote":** truly remote-from-abroad roles rarely come with visa sponsorship or the 30% ruling — the highly-skilled-migrant permit requires living in NL. "Remote at an NL company" usually still means relocating (hybrid, 2–3 office days is the norm). Use `--remote` for genuinely location-flexible roles, but expect most sponsored roles to be NL-based.

---

## Weekly cadence (~30–45 min/day, 5 days/week)

| Activity | Target/week | Tool command |
|---|---|---|
| Connection requests | 50–75 (ramp from ~15/week in wk 1–2) | `python3 scripts/linkedin_outreach.py` (connect flow) |
| Feed comments (warm-up) | ~30 (3–8/day) | `--engage-feed` |
| DMs to accepted people | ~30–40 in flight | `--message-connections` |
| Detect who accepted | daily | `--sync-connections` |
| Read replies / track convos | daily | `--sync-messages` |
| Direct applications on careers pages | 3–8 | *(manual — not this tool)* |
| Video/coffee chats once warm | 1–2 | *(manual)* |

---

## The daily loop (run in this order)

Run during **NL business hours (CET), Tue–Thu mornings are best**. The tool's `SEND_HOURS` gate (8:00–22:00 local) already blocks off-hours sends.

**1. Sync state first** — see who accepted and who replied:
```bash
python3 scripts/linkedin_outreach.py --sync-connections   # requested → connected
python3 scripts/linkedin_outreach.py --sync-messages       # detect replies → replied/developing
```

**2. Warm up targets** — comment on feed posts (builds name recognition before you connect/DM):
```bash
python3 scripts/linkedin_outreach.py --engage-feed --limit 5
```

**3. Send connection requests** — the connect flow searches your NL company list, geo-filtered to the Netherlands, and sends personalized (NL-aware) notes. Preview first, then send:
```bash
python3 scripts/linkedin_outreach.py --dry-run     # preview who it would target
python3 scripts/linkedin_outreach.py               # real (respects daily warm-up target + weekly cap)
```

**4. Message the newly connected** — AI-generated rapport-builders (NO ask), only to people who accepted:
```bash
python3 scripts/linkedin_outreach.py --message-connections --dry-run --limit 3   # preview
python3 scripts/linkedin_outreach.py --message-connections --limit 5             # real
```

**5. Review the workbook** — open `~/linkedin_outreach.xlsx` (Connections / Feed Engagement / Conversations tabs). Look at who's `replied`/`developing` — **those are your live conversations to nurture by hand.**

---

## The message ladder (what to say at each stage)

The tool automates stages 1–2. **Stages 3–4 you do manually** (they're relationship judgment, not automation).

- **Stage 1 — Connection note** (tool, NL-aware): short, references the company/NL context, *no ask*.
- **Stage 2 — Post-accept DM** (tool, AI via Ollama): thanks + a genuine question about their work/stack, *no ask*, ends with a question.
- **Stage 3 — Build rapport** (YOU, manual): 1–2 real exchanges. Be curious about their work, share a relevant project of yours, find common ground. Disclose you're exploring NL roles + need sponsorship here — plainly.
- **Stage 4 — The soft ask** (YOU, manual): only after a real exchange. Make referring frictionless:
  > "This has been really helpful. I saw [Company] has an open [role] — I think I'd fit given [1 specific reason]. Would you be open to referring me? I'll send a 2-line blurb + CV so it takes you 2 minutes." → then deliver exactly that (job link + resume + 2–3 tailored bullets).
  - Softer alternative (for hiring managers, or when no obvious req): ask for a 15-min informational chat instead. It surfaces roles and often ends in "let me refer you."
- **Follow-up:** ONE polite nudge ~3–5 days after silence. Then stop.

---

## Who to prioritize (edit `TARGET_ROLES` / target people)
1. **Engineers on your stack** at target companies (mid/senior ICs, tech leads) — they refer.
2. **Engineering managers / team leads** at scaleups (Mollie, bunq, Picnic) — they often own hiring directly.
3. **Internal recruiters** at recognised sponsors — they confirm sponsorship + headcount fast.
4. Skip: generic HR inboxes, agency recruiters spamming contract roles.

---

## Weekly review (every Friday)
- Open the Excel workbook. Check the funnel counts by `status`.
- **Acceptance rate** (the tool logs it): if <20%, your notes/profile need work — a low rate is also a LinkedIn restriction trigger. Improve personalization, warm targets more before connecting.
- **Withdraw stale invites** (>3 weeks pending) on LinkedIn to keep the pending pile low.
- Identify `developing` conversations → push those toward a Stage-4 ask or a call.
- Apply directly on careers pages for any role where you have a warm contact in-flight.

---

## Safety / account health (already enforced by the tool)
- Weekly invite cap 90, daily warm-up ramp, business-hours-only sends, human delays + coffee breaks, `MESSAGE_DAILY_LIMIT=10`, `COMMENT_DAILY_LIMIT=8`, dedup (never message/comment twice).
- If LinkedIn ever restricts the account: **stop all automation immediately**, do everything by hand for 1–2 weeks, let it clear.

---

## Honest caveats
- LinkedIn's User Agreement prohibits automation; pacing reduces but never eliminates account risk. The single biggest protective lever is **acceptance rate** — target relevant people.
- Referrals convert, but they require *real* conversations. The tool gets you to the conversation; **you close it.** Don't let automation replace the human stages 3–4.
- Verify visa/tax specifics (IND thresholds, 30% ruling — flat rate moving to 27% in 2027) against official sources before relying on numbers; they change yearly.

**Sources:** Zippia & Apollo Technical (referral statistics), Close.com (outreach reply rates), IND (visa/sponsor register), Expatax (30% ruling), iamexpat.nl (NL job-search norms), EF EPI (English proficiency).
