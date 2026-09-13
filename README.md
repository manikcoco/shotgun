# shotgun

Find the senior security roles that are actually open to you, score them
against your own profile, and pre-fill the application. Nothing is ever
submitted automatically.

```
discover  →  filter (free)  →  score (paid)  →  tailor  →  fill  →  you press submit
 44,000        ~600 match       ~60 worth it    resume    form      review step
```

## The idea

Job boards are built for recruiters, not candidates, and they omit the three
things that decide whether a security role is worth an evening of your time:

- **Will they sponsor, or pay to relocate you?** Measured across a
  44,000-posting corpus: of 1,017 security roles, **80 state a visa or
  relocation offer, 10 explicitly refuse, and 603 say nothing at all.**
  Silence is not refusal, and no board will tell you which bucket you're in.
- **Is "remote" actually remote?** "Remote", "Remote (United States)",
  "Remote - EMEA" and "Any location, Germany" are four different offers. Only
  **25 of those 1,017 roles** name no country, state or region at all.
- **Is it really the level it says?** A "Senior Cloud Security Engineer" at a
  large company is frequently the same scope as Staff elsewhere. Titles don't
  settle it; responsibilities do.

So shotgun reads the postings themselves — 416 company ATS boards, four
remote-first aggregators, and the big scrapers — and answers those questions
before you've read a word.

It is a personal tool, published because the interesting parts are the ones
nobody writes down: which job-board APIs are usable, how badly free-text
locations parse, and why a sponsorship regex needs its negatives before its
positives.

## Quick start

```bash
uv sync
cp .env.example .env                                    # add ANTHROPIC_API_KEY or OPENAI_API_KEY
cp config/preferences.example.yaml config/preferences.yaml
uv run shotgun profile init ~/your-resume.pdf
uv run shotgun answers init                             # the dozen questions every form asks
```

Then, without spending anything:

```bash
uv run shotgun sweep --anywhere        # what location-neutral security roles are open, right now
```

`config/preferences.yaml` is gitignored — it holds your salary floors, target
regions and blocked companies. Edit the copy, not the example. `uv sync` puts
the `shotgun` entry point in `.venv/bin` without adding it to `PATH`, so either
prefix with `uv run` or `source .venv/bin/activate` once.

## Commands

Everything except four commands is free. Only scoring, tailoring, resume
parsing and answer-drafting call a model.

### Looking — free

| Command | What it does |
|---|---|
| `shotgun sweep` | Ask all 416 boards what security roles are open. ~30s, nothing stored. |
| `shotgun sweep --anywhere` | Only location-neutral roles: remote, naming no country or region. |
| `shotgun sweep -c canonical -c duck-duck-go` | Specific companies, by board token. |
| `shotgun mobility` | Who states visa sponsorship or relocation. `--stance all\|refuses\|silent\|unreadable` |
| `shotgun probe` | Discover ATS boards you don't have yet, from companies already in the corpus. |
| `shotgun status --filter --why` | How many stored roles match, how many are unscored, and why the rest were rejected. |
| `shotgun list --in-region` | Roles you could take without sponsorship. Also `--anywhere`, `--near-miss`, `--min-score`. |
| `shotgun show <id> --jd` | One posting in full: score, priority breakdown, reasoning, history. |
| `shotgun queue` | The apply queue in the order `prepare` will work it. |
| `shotgun ui --port 8791` | Local web dashboard. |

### Running the pipeline

| Command | Costs a model call? |
|---|---|
| `shotgun discover` · `-s ats_boards\|remote_boards\|jobspy` | no |
| `shotgun rank` | **yes** — scores the survivors of the free filter |
| `shotgun rank --rules-only` | no — queues everything matching, unranked |
| `shotgun rank --refilter` / `--rebucket` / `--reprioritise` | no — re-apply config changes to work already paid for |
| `shotgun prepare --job-id N` | **yes** — tailors a resume and cover letter, then fills the form |
| `shotgun prepare --job-id N --reuse-docs` | no — re-fill using documents already on disk |
| `shotgun review` | no — reopens each filled form so you submit it |
| `shotgun profile init` · `shotgun draft N` | **yes** |

### Tracking

```bash
shotgun mark 42 interview --note "screen booked"
shotgun retry --stage failed
shotgun answers list | set | skip | rm
```

`shotgun prepare` stops before submit, every time. `shotgun review` reopens
the filled form and a human presses the button. `apply.mode: auto` is accepted
in config and deliberately not implemented.

### Answers

`shotgun answers init` walks the dozen questions every application form asks —
work authorisation, sponsorship, notice period, salary — pre-filled from your
profile, so it is mostly pressing Enter. Answers then auto-fill on any ATS whose
fields carry semantic names (Lever, Workable, Personio).

`answers skip <key>` records a question as deliberately blank. That is a
different state from unanswered: nothing auto-fills it, and neither
`answers list` nor `answers init` asks again — `answers init --all` revisits
everything.

Store numbers bare. Salary and years-of-experience fields are frequently
`<input type="number">` or `pattern="[0-9]*"`, where "EUR 120,000 minimum"
cannot be typed at all — Playwright raises, `try_fill` swallows it, and the
field is left blank with nothing to explain why. `answers set` warns and
suggests the digits.

Greenhouse names its custom questions
`job_application[answers_attributes][0][text_value]`, so there is nothing for a
selector to match. Those still come back in `unanswered_questions` — but
`shotgun review` prints your stored answer beside each one, so you paste rather
than recall. Answers marked `--sensitive` are recorded and shown, never typed
into a form automatically.

## How it works

**The filter is two-stage, and the ordering is the whole economics.** A free
rule pass — title regex, location, compensation floor, dealbreaker phrases —
rejects about 98% of a corpus. Only survivors reach a model. Within that pass,
the title check runs *before* the description is read, because reading
descriptions for mobility signals costs 22 of every 24 seconds and the title
check rejects 94% of postings on its own. Getting that order right took a
filter pass from 24 seconds to two.

**Location parsing is the hard part, and it fails in expensive ways.**
`geo.py` handles two-letter codes that are simultaneously a US state and a
target country (`Berlin, DE` resolved to Delaware; `Bengaluru, IN` to
Indiana), spelled-out states (`Remote - California` parsed as naming no
country, so a US-only role sailed through as in-region), leading codes
(`IN-Pune`, `KR - Seoul`, `UK - Remote`), endonyms, and the difference between
"anywhere" and a continent. `Any location, United States` reads as
location-neutral until you check the country first.

**Sponsorship detection is written from the corpus, not from guesses.** The
612 distinct sentences containing "sponsor" across 15,000 descriptions decide
the patterns. Refusals are matched *before* offers, because the offer patterns
are substrings of the refusals — `not eligible for sponsorship` contains
`eligible for sponsorship`, and read in the wrong order a flat refusal becomes
an offer. Two things the corpus forbids: `not guaranteed` is a hedge rather
than a refusal, and the contraction in "we aren't able to sponsor every role"
must not match, because the 560 postings carrying it also say "we do sponsor
visas".

**Board discovery is company-first, and that's the ceiling.** `sweep` can only
ask boards it has been told about, so `probe` derives candidate tokens from
company names already in the corpus and tries the spellings boards actually
use — squashed, hyphenated, first-word-only, and case preserved, because
`jobs.lever.co/Ubiminds` is live and `ubiminds` is a 404. Mining apply URLs for
tokens seems obvious and does not work: every aggregator keeps the URL on its
own domain, so across 26,000 postings it yielded two.

**Tailoring cannot invent anything.** See Design notes below.

## Sources

| Source | How | Notes |
|---|---|---|
| Greenhouse, Lever, Ashby, Personio, SmartRecruiters, Workable | public job-board APIs | Highest signal: full JDs, direct apply URLs, no anti-bot. Add company tokens to `preferences.yaml`. Personio is an XML feed and publishes `employmentType` as a real field, but its tenants skew small German-speaking firms — low yield for staff security roles unless you add your own tokens. |
| Remotive, RemoteOK, WeWorkRemotely, arbeitnow | public JSON / RSS | Remotive is best — publishes `job_type` and the regions an employer will hire into. arbeitnow is the only Germany/EU-weighted one and needs no token list, which is how it surfaces companies that run no public ATS at all. |
| LinkedIn, Indeed, Glassdoor, Google | JobSpy | Broad but noisy; descriptions often missing. |
| remote.com | headless browser | No public API (RSC app), so this scrapes the DOM. Fragile, off by default. |
## ATS coverage

| ATS | Status |
|---|---|
| Greenhouse, Lever, LinkedIn Easy Apply | dedicated filler |
| Ashby, Rippling, Deel, HiBob, Workable, SmartRecruiters, Teamtailor, Personio | generic semantic filler — expect partial fills |
| Workday, iCIMS, Taleo, SuccessFactors | **refuses** — multi-step wizards needing per-tenant accounts |

The generic filler matches on field semantics (input type, `autocomplete`
token, label text) rather than per-site selectors, so it degrades rather than
breaks. Anything it can't answer comes back in `unanswered_questions`.
## Design notes

**Tailoring never invents anything.** It reorders, reweights and rephrases
what `profile.yaml` already says. `verify_no_fabrication` splits its findings:
a *bullet* naming an employer, a technology, or a figure absent from the
profile is a fabricated claim and blocks the resume outright — including an
inflated metric, so "cut risk 40%" cannot become 60%. An untraceable entry in
the skills list only warns, because that is usually a legitimate rephrase
("gVisor" surfacing as "container isolation (gVisor)") and hard-blocking those
dead-ended every run.

**`role_bullets` is a list, not a map.** Structured outputs compile a
`dict[str, list[str]]` down to `{"properties": {}, "additionalProperties":
false}` — a schema that admits only the empty object — so a map keyed by
company silently came back empty and every resume shipped with untailored
bullets. Verified against the API; there is a regression test.

**Resumes render single-column with real text**, no tables or graphics, via
Chromium's print engine. Keep a designed PDF for humans; let this one go to
ATS parsers.

**The profile sits in a cached system block** so scoring and tailoring reuse
the same prefix. `load_yaml()` returns the file bytes verbatim rather than
re-serialising, because a key reorder would silently blow the cache.
## Behind a TLS-intercepting proxy

Plenty of managed machines run one, and it breaks Python HTTPS in a way that
is annoying to diagnose — `curl` works, every `certifi`-based client fails.
Two workarounds are built in and are no-ops otherwise:

- **Trust store.** `shotgun/__init__.py` points `ssl` at the system trust
  store, so a root CA installed in the OS keychain but absent from `certifi`
  is picked up.
- **Playwright download.** `playwright install chromium` fetches its driver
  from a CDN the proxy may block, so `shotgun/browser.py` falls back to an
  installed Chrome. Force it with `SHOTGUN_BROWSER_CHANNEL=chrome`.
## Privacy

`private/` and `data/` are gitignored. Your profile, generated resumes,
screenshots and the SQLite database stay local. The web UI binds to 127.0.0.1
and has no auth — don't expose it.

**A fresh clone starts empty.** That is the point of the gitignores, but it
means a second checkout shares no state with the first: no `private/profile.yaml`,
no `data/shotgun.db`, no stored answers, no discovered postings, no `.env`.
Cloning is how you move the *code* to another machine; to move the state, copy
`private/`, `data/` and `.env` across by hand.

## Limitations

Worth knowing before you trust it:

- **No filler has ever run against a live form.** Discovery, ranking,
  tailoring and rendering are exercised end to end; the four form fillers are
  not. Nothing has been submitted by this tool.
- **No Workday or iCIMS filler**, which is where a lot of enterprise security
  roles live. Both need per-tenant accounts and multi-step wizards, so the
  filler refuses rather than half-completing a form.
- **Descriptions are hydrated only for SmartRecruiters.** Its list endpoint
  publishes none, so security roles there were unreadable for the visa and
  relocation signals that matter most; the fix fetches descriptions for
  postings the title rules already kept, not for all 4,000. LinkedIn omits
  descriptions from search results and remains the larger gap — those
  postings are scored on the title alone.
- **`Job.fingerprint` is `company|title`, and `upsert_job` never updates
  `location`.** On one measured fetch, 427 of 6,958 postings merged into a row
  holding a different location, so alternative hiring countries are silently
  dropped.
- **Location parsing is a lookup table, so it has a long tail.** City and
  country coverage is driven by what a real corpus emitted; an unrecognised
  city means the posting counts as "names nowhere", which is the forgiving
  direction but still wrong.
- **SQLite runs without WAL**, and `prepare` holds a write transaction across
  each browser session, so `shotgun ui` and `shotgun prepare` can lock each
  other out.
- **The web dashboard has no pagination** — filtered-out postings also get an
  `applications` row, so it renders thousands.
- `apply.mode: auto` is accepted in config and not implemented, on purpose.

## Tests

```bash
uv run pytest        # 499 tests, no network, no model calls
uv run ruff check src tests
```

The tests are worth a look if you're evaluating the approach rather than using
the tool. Most of them encode a specific failure found against real data —
`test_visa.py` quotes the phrasings the corpus actually uses and asserts the
ones that must *not* match; `test_geo.py` is a table of location strings real
boards emit.

## Licence

MIT. See `LICENSE`.
