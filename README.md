# shotgun

Find the senior security roles actually open to you, score them against your
own profile, and pre-fill the application. Nothing is ever submitted
automatically.

```
discover  →  filter (free)  →  score (paid)  →  tailor  →  fill  →  you submit
 44,000        ~600 match        ~60 worth it    resume    form     review step
```

## Why

Job boards omit the three things that decide whether a security role is worth
an evening. Measured across a 44,000-posting corpus, of 1,017 security roles:

- **Sponsorship** — 80 state a visa or relocation offer, 10 explicitly refuse,
  **603 say nothing at all.** Silence is not refusal, and no board tells you
  which bucket you're in.
- **Remote** — "Remote", "Remote (United States)", "Remote - EMEA" and "Any
  location, Germany" are four different offers. Only **25** name no country,
  state or region.
- **Level** — a "Senior Cloud Security Engineer" at a large company is often
  Staff scope elsewhere. Titles don't settle it; responsibilities do.

shotgun reads the postings themselves and answers those before you read a word.

## Quick start

```bash
uv sync
uv run pytest                                             # 499 tests, no network, no model calls
cp .env.example .env                                      # ANTHROPIC_API_KEY or OPENAI_API_KEY
cp config/preferences.example.yaml config/preferences.yaml
uv run shotgun profile init ~/your-resume.pdf
uv run shotgun sweep --anywhere                           # free: what's open right now
```

`config/preferences.yaml` is gitignored — it holds your salary floors, regions
and blocked companies. Edit the copy.

## Commands

Only four cost a model call. Everything else is free.

| | |
|---|---|
| `sweep` | Ask 400+ boards what security roles are open. ~30s, nothing stored. |
| `sweep --anywhere` | Only location-neutral: remote, naming no country or region. |
| `sweep -c canonical -c duck-duck-go` | Specific companies, by board token. |
| `mobility` | Who states visa or relocation. `--stance all\|refuses\|silent\|unreadable` |
| `probe` | Discover boards you don't have, from companies already in the corpus. |
| `status --filter --why` | How many roles match, how many unscored, why the rest were rejected. |
| `list --in-region` | Roles you could take without sponsorship. Also `--anywhere`, `--near-miss`. |
| `show <id> --jd` | One posting: score, priority breakdown, reasoning, history. |
| `report` | Write `reports/security-roles.{json,md}` — every open role, with what changed since the last run at the top. Free. |
| `queue` · `ui` | The apply queue; local web dashboard (binds 127.0.0.1, no auth — don't expose it). |
| `discover` | Fetch and store. `-s ats_boards\|remote_boards\|jobspy` |
| `rank` | **paid** — scores what survived the free filter. `--rules-only` doesn't. |
| `rank --refilter\|--rebucket\|--reprioritise` | Re-apply config changes to work already paid for. |
| `prepare --job-id N` | **paid** — tailors resume + cover letter, fills the form, stops. |
| `review` | Reopens each filled form so you press submit. |
| `answers init\|list\|set\|skip` | The dozen questions every form asks, answered once. |
| `mark 42 interview` · `retry` | Track replies; requeue failures. |

`prepare` never submits. `apply.mode: auto` is accepted in config and
deliberately unimplemented.

## The standing report

```bash
uv run shotgun report
```

Writes `reports/security-roles.json` — the record the next run diffs against —
and `reports/security-roles.md`, which opens with **New since last run**, then
**Changed**, then **No longer listed**, then every open role by company.

Built for a sweep every few weeks, so the useful property is that it does not
repeat itself. A role is identified by **company + title + country**, not by
job id (per-database) and not by URL (changes whenever a board reposts), so a
role you saw last month does not come back as new because someone reposted it,
and `first_seen` survives. Only title, company, country, location, URL, remote
and salary count as a change — a reworded description does not, because boards
reword constantly and it is not a new job.

Commit the files and `git diff reports/` is a second view of the same thing.

## How it works

**The filter is two-stage, and the ordering is the economics.** A free rule
pass — title regex, location, comp floor, dealbreakers — rejects ~98% before
anything reaches a model. Within it, the title check runs *before* the
description is read: reading descriptions for mobility signals cost 22 of
every 24 seconds, and the title check rejects 94% on its own. Reordering took
a filter pass from 24s to 2s.

**Location parsing fails expensively.** Two-letter codes that are both a US
state and a target country (`Berlin, DE` → Delaware; `Bengaluru, IN` →
Indiana). Spelled-out states (`Remote - California` parsed as naming no
country, so a US-only role passed as in-region). Leading codes (`IN-Pune`,
`UK - Remote`). And `Any location, United States` reads as location-neutral
until you check the country first.

**Sponsorship patterns come from the corpus, not from guesses.** The 612
distinct sentences containing "sponsor" decide them. Refusals match *before*
offers, because the offer patterns are substrings of the refusals —
`not eligible for sponsorship` contains `eligible for sponsorship`. Two things
the data forbids: `not guaranteed` is a hedge, not a refusal; and the
contraction in "we aren't able to sponsor every role" must not match, because
the 560 postings carrying it also say "we do sponsor visas".

**Board discovery is company-first, which is the ceiling.** `probe` derives
candidate tokens from company names already seen and tries the spellings
boards use — squashed, hyphenated, first word, and case preserved, because
`jobs.lever.co/Ubiminds` is live and `ubiminds` is a 404. Mining apply URLs
instead looks obvious and doesn't work: every aggregator keeps the URL on its
own domain, so 26,000 postings yielded two tokens.

**Tailoring can't invent anything.** It reorders and rephrases what your
profile already says. A bullet naming an employer, technology or figure absent
from the profile blocks the resume outright — including an inflated metric, so
"cut risk 40%" can't become 60%.

## Sources

| Source | Notes |
|---|---|
| Greenhouse, Lever, Ashby, Personio, SmartRecruiters, Workable, Recruitee, Teamtailor | Public board APIs. Highest signal: full JDs, direct apply URLs, no anti-bot. You supply company tokens. SmartRecruiters publishes no description on its list endpoint, so those are hydrated per-posting after the title filter. Teamtailor serves a JSON Feed — title, URL and body, no location or salary. Which part of the URL carries the token differs per board: path for Workable and SmartRecruiters, subdomain for Recruitee, Teamtailor and Personio. |
| Remotive, RemoteOK, WeWorkRemotely, arbeitnow | Public JSON/RSS. arbeitnow is the only Germany/EU-weighted one and needs no token list, which is how it surfaces companies running no public ATS at all. |
| LinkedIn, Indeed, Google | Via JobSpy. Broad, noisy, descriptions usually missing. |

Form filling: dedicated fillers for Greenhouse, Lever and LinkedIn Easy Apply;
a semantic filler for Ashby, Workable, SmartRecruiters, Personio and others
that matches on field type, `autocomplete` token and label text, so it
degrades rather than breaks. Workday and iCIMS are refused outright —
multi-step wizards needing per-tenant accounts.

## Contributing

Pull requests and forks are very welcome. This started as one person's tool
for one person's job search, and most of what would make it genuinely useful
to others is work I haven't done.

The most valuable places to help:

- **The form fillers have never been run against a live application.**
  Discovery, ranking, tailoring and rendering are exercised end to end; the
  fillers are not. If you run one and it breaks, that bug report is worth more
  than a feature.
- **New board adapters.** Workday and iCIMS are where a lot of enterprise
  security roles live and neither is supported. The six existing adapters in
  `discover/ats_boards.py` are the pattern to follow — please verify the
  response shape against a live call rather than a remembered one, as the
  existing ones do.
- **Location and sponsorship coverage.** `geo.py` and `visa.py` are lookup
  tables and regexes built from one corpus, so they have a long tail. A
  location string or a sponsorship phrasing they get wrong, with a test, is an
  ideal first contribution.
- **Your own `preferences.yaml` shape.** The tool assumes security roles and
  five target regions. If you bend it to another discipline or geography and
  something breaks, say so.

Two things to know before opening a PR: tests must pass without network access
or model calls (`uv run pytest`), and `ruff check src tests` must be clean.
Please keep comments explaining *why* a decision was made rather than what the
code does — that convention is most of the value in this repo.

MIT.
