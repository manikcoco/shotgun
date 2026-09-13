"""The reusable answer store: the boring questions every application asks.

Application forms ask the same dozen things — work authorisation, notice
period, salary expectation, whether you need sponsorship. The `answers` table
existed from the first commit but nothing ever wrote to it, so
`pipeline._stored_answers()` always returned an empty dict and every form came
back with the same questions unanswered. This module is the catalogue and the
seeding logic; `shotgun answers` is the front end.

Two honest limits on how far the auto-fill can go:

* Fillers match an answer to a field by looking for the key as a substring of
  the input's `name`/`id`. That works on Lever (`cards[...]`), Workable and
  Personio, whose fields carry semantic names. It does *not* work on Greenhouse
  custom questions, which are named `job_application[answers_attributes][0]
  [text_value]` — nothing semantic to match. For those the answer still earns
  its place: `shotgun review` prints the store next to the form so you can
  paste rather than recall.
* Answers marked sensitive are deliberately never auto-filled. Recording a
  figure is one decision; replaying it into an arbitrary third-party form is a
  different one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

SAFE_KEY = re.compile(r"[^a-z0-9_]")


def selector_key(key: str) -> str:
    """A key safe to interpolate into a CSS attribute selector.

    Fillers build `input[name*='{key}' i]`. An unsanitised key containing a
    quote breaks the selector or injects into it, so keys are restricted to
    lowercase, digits and underscore.
    """
    return SAFE_KEY.sub("", key.strip().lower())


@dataclass(frozen=True)
class StandardQuestion:
    key: str
    question: str
    hint: str = ""
    sensitive: bool = False
    numeric: bool = False


# Keys whose fields are frequently `<input type="number">` or carry a
# `pattern="[0-9]*"`. Measured against a real browser: Playwright raises
# "Cannot type text into input[type=number]" for anything non-numeric, and
# try_fill swallows that and leaves the field blank — so a value like
# "EUR 120,000 minimum" fails silently. A bare "120000" fills all three of
# number, text and pattern-constrained inputs.
_NON_NUMERIC = re.compile(r"[^\d]")


def numeric_warning(key: str, value: str) -> str | None:
    """Flag a value that will not survive a numeric input, or None."""
    question = BY_KEY.get(selector_key(key))
    if not question or not question.numeric:
        return None
    if not _NON_NUMERIC.search(value.strip()):
        return None

    digits = _NON_NUMERIC.sub("", value)
    problems = []
    if re.search(r"[a-zA-Z]", value):
        problems.append("letters")
    if "," in value or "." in value:
        problems.append("separators")
    detail = " and ".join(problems) or "non-digit characters"

    suggestion = f" Consider just {digits!r}." if digits else ""
    return (
        f"{value!r} contains {detail}. Salary and count fields are often "
        f"<input type=number> or pattern-constrained, where this cannot be "
        f"typed at all — and the filler fails silently, leaving it blank."
        + suggestion
    )


# Ordered as an interview: identity, then authorisation, then commercials.
CATALOGUE: list[StandardQuestion] = [
    StandardQuestion(
        "work_authorization",
        "Are you legally authorised to work in the role's country?",
        "Forms phrase this a dozen ways. Answer for your strongest case.",
    ),
    StandardQuestion(
        "sponsorship",
        "Will you now or in the future require visa sponsorship?",
        "Asked as a yes/no on most US and UK forms.",
    ),
    StandardQuestion(
        "relocation",
        "Are you willing to relocate for this role?",
    ),
    StandardQuestion(
        "notice_period",
        "What is your notice period?",
    ),
    StandardQuestion(
        "start_date",
        "Earliest start date / availability?",
    ),
    StandardQuestion(
        "salary",
        "Expected salary or compensation?",
        "Store the bare number — these fields are often numeric-only. "
        "Sensitive by default, so it is never auto-filled.",
        sensitive=True,
        numeric=True,
    ),
    StandardQuestion(
        "years_experience",
        "Total years of relevant experience?",
        "A bare number — this is usually a numeric field.",
        numeric=True,
    ),
    StandardQuestion(
        "current_company",
        "Current employer?",
    ),
    StandardQuestion(
        "current_title",
        "Current job title?",
    ),
    StandardQuestion(
        "location",
        "Where are you based?",
    ),
    StandardQuestion(
        "citizenship",
        "Passport country / nationality?",
        "EU and UK forms ask this constantly, separately from residence.",
    ),
    StandardQuestion(
        "linkedin",
        "LinkedIn profile URL?",
    ),
    StandardQuestion(
        "github",
        "GitHub profile URL?",
    ),
    StandardQuestion(
        "languages",
        "Languages and proficiency?",
        "Matters for DACH and EU roles.",
    ),
    StandardQuestion(
        "referral",
        "How did you hear about this role?",
    ),
    StandardQuestion(
        "pronouns",
        "Pronouns, if you want them on the application?",
        "Leave blank to skip. Many forms make this optional.",
    ),
]

BY_KEY = {q.key: q for q in CATALOGUE}


_NEEDS_SPONSOR = re.compile(r"\b(?:needs?|require\w*)\s+(?:\w+\s+){0,2}sponsor", re.I)
_NO_SPONSOR_NEEDED = re.compile(
    r"\bno\s+(?:full\s+)?(?:visa\s+)?sponsor|\bwithout\s+sponsor|\bcitizen\b"
    r"|\bpermanent\s+resident|\bblue\s?card\b|\bunrestricted\b",
    re.I,
)


def _needs_sponsorship(status: str) -> bool:
    """Does this work-authorisation status mean sponsorship is required?"""
    if _NO_SPONSOR_NEEDED.search(status):
        return False
    return bool(_NEEDS_SPONSOR.search(status))


def _total_years(profile) -> str | None:
    """Years of experience implied by the earliest role start date."""
    years = []
    for role in getattr(profile, "roles", None) or []:
        match = re.search(r"(19|20)\d{2}", str(role.start or ""))
        if match:
            years.append(int(match.group(0)))
    if not years:
        return None
    return str(max(1, date.today().year - min(years)))


def suggest(profile) -> dict[str, str]:
    """Pre-fill what the profile already knows, so `answers init` is mostly
    confirmation rather than typing."""
    contact = getattr(profile, "contact", None)
    roles = getattr(profile, "roles", None) or []
    current = roles[0] if roles else None

    auth = {k: v for k, v in (getattr(profile, "work_authorization", None) or {}).items() if v}
    salary = {k: v for k, v in (getattr(profile, "salary_expectation", None) or {}).items() if v}
    languages = getattr(profile, "languages", None) or []

    out: dict[str, str] = {}

    if auth:
        # Countries where no sponsorship is needed read as authorised. A plain
        # substring test for "sponsor" is wrong: an EU Blue Card entry reads
        # "...local permit required but no full visa sponsorship", which says
        # the opposite of what the substring implies.
        clear = [c for c, s in sorted(auth.items()) if not _needs_sponsorship(s)]
        if clear:
            out["work_authorization"] = (
                "Yes for " + ", ".join(clear) + "; sponsorship required elsewhere"
            )
        needs = [c for c in sorted(auth) if c not in clear]
        if needs:
            out["sponsorship"] = "Yes — required for " + ", ".join(needs)

    if salary:
        out["salary"] = ", ".join(f"{cur} {val}" for cur, val in sorted(salary.items()))
    if getattr(profile, "notice_period", None):
        out["notice_period"] = str(profile.notice_period)
    if languages:
        out["languages"] = "; ".join(languages)
    if contact is not None:
        if contact.location:
            out["location"] = contact.location
        if contact.linkedin:
            out["linkedin"] = contact.linkedin
        if contact.github:
            out["github"] = contact.github
    if current is not None:
        out["current_company"] = current.company
        out["current_title"] = current.title

    # A "citizen" entry in work_authorization is the passport country, which
    # forms ask for separately from residence — Ashby's "Passport Country"
    # sat empty while the profile recorded "IN: citizen".
    citizen = [c for c, status in sorted(auth.items())
               if re.search(r"\bcitizen\b", status, re.I)]
    if citizen:
        from .geo import COUNTRY_NAMES
        out["citizenship"] = ", ".join(COUNTRY_NAMES.get(c, c) for c in citizen)

    years = _total_years(profile)
    if years:
        out["years_experience"] = years

    return out


# A deliberately-blank answer. Distinct from "not answered yet": without it,
# `answers list` reports the question as missing forever and `answers init`
# keeps asking. `pipeline._stored_answers()` only auto-fills confidence
# 'confirmed', so a skipped answer stays out of every form by construction.
SKIPPED = "skipped"


def gaps(
    stored: dict[str, str],
    skipped: set[str] | frozenset[str] = frozenset(),
) -> list[StandardQuestion]:
    """Catalogue entries with nothing recorded and not deliberately skipped."""
    return [
        q for q in CATALOGUE
        if q.key not in skipped and not (stored.get(q.key) or "").strip()
    ]


def match_for(question_text: str, stored: dict[str, str]) -> tuple[str, str] | None:
    """Best stored answer for a free-text form question, or None.

    Used to annotate `unanswered_questions` in the review view — the label a
    form shows ("Do you require sponsorship to work in the UK?") rarely equals
    an answer key, so match on keywords instead.
    """
    text = question_text.lower()

    # Drafted answers are stored under a key derived from the form's own
    # question, so the question itself is the match. Checked first: a drafted
    # answer to "Tell us about your experience working async…" should never
    # lose to a keyword rule.
    for key, value in stored.items():
        if not key.startswith("q_") or not (value or "").strip():
            continue
        stem = key[2:].replace("_", " ")
        if stem and stem in text:
            return key, value

    keywords = {
        "work_authorization": ("authoris", "authoriz", "eligible to work", "right to work"),
        "sponsorship": ("sponsor", "visa"),
        "relocation": ("relocat",),
        "notice_period": ("notice",),
        "start_date": ("start date", "availab", "when can you"),
        "salary": ("salary", "compensation", "expected pay", "remuneration"),
        "years_experience": ("years of experience", "years experience"),
        "current_company": ("current employer", "current company"),
        "current_title": ("current title", "current role", "job title"),
        "location": ("where are you", "city", "based"),
        "citizenship": ("passport", "nationality", "citizenship"),
        "linkedin": ("linkedin",),
        "github": ("github",),
        "languages": ("language", "german", "fluent"),
        "referral": ("how did you hear", "referral", "referred"),
        "pronouns": ("pronoun",),
    }
    for key, needles in keywords.items():
        value = (stored.get(key) or "").strip()
        if value and any(n in text for n in needles):
            return key, value
    return None
