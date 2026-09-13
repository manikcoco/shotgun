"""Rewrite the resume for one specific JD.

The hard constraint is that tailoring reorders, reweights and rephrases what
the profile already says. It does not add experience. That rule lives in the
system prompt and is the single most important line in this file — a tailoring
step that invents a credential is a resume that gets you blacklisted.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from dataclasses import dataclass, field

from . import llm
from .models import CoverLetter, TailoredResume

log = logging.getLogger(__name__)

TAILOR_SYSTEM = """You rewrite the candidate's resume for one specific job \
posting. The candidate's full profile is above.

Absolute rules:
- Never invent experience, employers, dates, titles, certifications, metrics, \
or technologies. Every claim in your output must be traceable to the profile.
- You may reorder, reweight, compress, merge, and rephrase. You may surface a \
detail the profile mentions in passing. You may not add a new fact.
- If the posting wants something the candidate does not have, leave it out. Do \
not hedge it into a bullet. The gap is the reader's to judge.
- Keep every metric exactly as the profile states it. If the profile says 40%, \
you say 40%.

Style:
- Bullets lead with the outcome, then the mechanism. Past tense for past roles.
- No filler adjectives ("passionate", "results-driven", "synergistic").
- Keep bullets under 30 words. Aim for 3-5 bullets on recent roles, 2-3 on older.
- `role_bullets` must contain one entry per role in the profile, most recent \
first, with `company` copied exactly from the profile. Include every role \
unless it is genuinely irrelevant, in which case name it in `omitted` and \
leave it out of `role_bullets`.
- `highlighted_skills` should put the posting's stated requirements first, but \
only skills the profile actually claims."""

COVER_SYSTEM = """Write a cover letter for this posting from the candidate's \
profile above.

- 200-300 words. Three or four short paragraphs.
- Open with why this specific role and company, referencing something concrete \
from the posting. No "I am writing to apply for".
- Middle: two or three specific achievements from the profile that map to the \
posting's requirements. Real numbers where the profile has them.
- Close briefly. No "I look forward to hearing from you" boilerplate.
- Never invent anything not in the profile. Same rule as the resume.
- Plain text, no markdown, no salutation placeholder like [Hiring Manager] — \
if you don't know the name, address the team."""


def _posting_context(job: sqlite3.Row, score: sqlite3.Row | None) -> str:
    lines = [
        f"Company: {job['company']}",
        f"Title: {job['title']}",
        f"Location: {job['location'] or 'unstated'}",
    ]
    if score and score["key_requirements"]:
        requirements = json.loads(score["key_requirements"])
        if requirements:
            lines.append("")
            lines.append("Requirements this resume must address:")
            lines.extend(f"- {r}" for r in requirements)
    lines.extend(["", "Full description:", (job["description"] or "(none captured)")[:20000]])
    return "\n".join(lines)


def tailor_resume(
    job: sqlite3.Row,
    score: sqlite3.Row | None,
    profile_yaml: str,
) -> TailoredResume:
    return llm.parse_structured(
        [profile_yaml, TAILOR_SYSTEM],
        f"<posting>\n{_posting_context(job, score)}\n</posting>",
        TailoredResume,
        max_tokens=16000,
        label="tailor",
    )


def write_cover_letter(
    job: sqlite3.Row,
    score: sqlite3.Row | None,
    profile_yaml: str,
) -> CoverLetter:
    return llm.parse_structured(
        [profile_yaml, COVER_SYSTEM],
        f"<posting>\n{_posting_context(job, score)}\n</posting>",
        CoverLetter,
        max_tokens=4000,
        label="cover letter",
    )


# A "named thing" in bullet prose. Covers acronyms (AWS, IAM, GDPR), internal
# capitals (gVisor, PostgreSQL, GitLab), letter/digit mixes (K8s, SOC2, Log4j)
# and — importantly — plain single-capital product names, because most security
# tooling is spelled that way (Falco, Snyk, Splunk, Vault, Terraform). The
# first word of a bullet and the generic words below are exempt, which is what
# keeps ordinary sentence capitalisation from tripping it.
_NAMED_ENTITY = re.compile(r"\b[A-Za-z][A-Za-z0-9.+#-]*\b")


def _is_named_shape(token: str) -> bool:
    """True if a token looks like a name rather than a plain lowercase word."""
    if not token[:1].isupper() and not any(c.isupper() for c in token[1:]):
        return False                                  # all lowercase: prose
    return any(c.isupper() for c in token)


# Capitalised words that show up mid-bullet in ordinary resume prose and are
# not product names. Without these, "Reduced risk across Production" reads as
# a fabricated technology.
_GENERIC_CAPITALS = {
    "a", "an", "and", "the", "of", "for", "to", "in", "on", "at", "by", "with",
    "across", "from", "into", "via", "per", "as", "led", "built", "cut", "ran",
    "drove", "owned", "set", "made", "grew", "shipped", "reduced", "improved",
    "increased", "decreased", "delivered", "designed", "developed", "deployed",
    "established", "implemented", "introduced", "launched", "managed",
    "migrated", "automated", "created", "defined", "scaled", "rolled", "wrote",
    "security", "engineering", "engineer", "production", "cloud", "platform",
    "team", "teams", "company", "customer", "customers", "product", "products",
    "infrastructure", "compliance", "risk", "review", "reviews", "programme",
    "program", "policy", "policies", "process", "incident", "incidents",
    "detection", "response", "identity", "access", "data", "privacy", "audit",
    "audits", "vulnerability", "vulnerabilities", "threat", "modelling",
    "modeling", "authentication", "authorization", "authorisation", "encryption",
    "monitoring", "logging", "testing", "automation", "governance", "posture",
    "controls", "control", "framework", "frameworks", "standard", "standards",
    "first", "new", "global", "internal", "external", "critical", "high",
    "annual", "quarterly", "monthly", "weekly", "daily",
}

# Digit runs, so a metric can be compared against the profile's own numbers.
_NUMBER = re.compile(r"\d[\d,.]*")


@dataclass
class FabricationReport:
    """What the tailored resume claims that the profile does not support.

    Split deliberately. A bullet that names an employer, a technology or a
    metric absent from the profile is a fabricated *claim* and blocks the
    resume. A skill-list entry that doesn't match by substring is usually just
    a rephrase ("gVisor" surfacing as "Container isolation (gVisor)"), so it
    warns and rides along to the approval queue instead of dead-ending the job.
    """

    blockers: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.blockers

    def summary(self) -> str:
        bits = []
        if self.blockers:
            bits.append(f"{len(self.blockers)} blocker(s): " + "; ".join(self.blockers))
        if self.warnings:
            bits.append(f"{len(self.warnings)} warning(s): " + "; ".join(self.warnings))
        return " | ".join(bits) or "clean"


def _normalise_number(raw: str) -> str:
    return raw.rstrip(".,").replace(",", "")


ANSWER_SYSTEM = """You answer one free-text question on a job application, \
as the candidate, in first person. Their profile is above.

- Ground every claim in the profile. Same rule as the resume: never invent an \
employer, a technology, a metric or a role. If the profile does not support a \
claim, leave it out rather than hedging it.
- 90-150 words unless the question clearly wants less. Specific over general: \
name the company, the tool, the number.
- Plain prose, no markdown, no bullet lists, no salutation, no sign-off.
- Answer the question that was asked. Do not restate it, and do not pad with \
enthusiasm about the company."""


def draft_answer(question: str, job, profile_yaml: str) -> str:
    """A first-person answer to one application question, from the profile.

    These are the fields that stop a form being submittable and that nothing
    else can fill — "tell us about your experience working async/remote" is
    not derivable from a resume parse, but it is answerable from the profile's
    actual history.
    """
    context = "\n".join([
        f"Company: {job['company']}",
        f"Role: {job['title']}",
        "",
        "Question:",
        question,
    ])
    answer = llm.parse_structured(
        [profile_yaml, ANSWER_SYSTEM],
        context,
        CoverLetter,
        max_tokens=2000,
        label="draft_answer",
        effort="low",
    )
    return answer.body.strip()


def verify_no_fabrication(tailored: TailoredResume, profile) -> FabricationReport:
    """Check that the rewrite stayed inside the profile.

    Blocks on bullet content, because that is where an invented technology or
    an inflated metric would actually live — the previous version checked only
    the skills list and the role dict's keys, and the dict was always empty.
    """
    report = FabricationReport()

    profile_blob = " ".join([
        profile.summary or "",
        " ".join(profile.skills),
        " ".join(profile.certifications),
        " ".join(b for r in profile.roles for b in r.bullets),
    ])
    blob_lower = profile_blob.lower()
    blob_entities = {m.group(0).lower() for m in _NAMED_ENTITY.finditer(profile_blob)}
    blob_numbers = {_normalise_number(m.group(0)) for m in _NUMBER.finditer(profile_blob)}

    known_companies = {r.company.strip().lower() for r in profile.roles}

    for entry in tailored.role_bullets:
        if entry.company.strip().lower() not in known_companies:
            report.blockers.append(f"bullets for unknown employer {entry.company!r}")

        for bullet in entry.bullets:
            tokens = list(_NAMED_ENTITY.finditer(bullet))
            for index, match in enumerate(tokens):
                token = match.group(0)
                if index == 0:
                    continue                       # sentence-initial capital
                if not _is_named_shape(token):
                    continue
                if token.lower() in _GENERIC_CAPITALS:
                    continue
                if token.lower() in blob_entities or token.lower() in blob_lower:
                    continue
                report.blockers.append(
                    f"{entry.company}: bullet names {token!r}, absent from profile"
                )
            for match in _NUMBER.finditer(bullet):
                number = _normalise_number(match.group(0))
                if number and number not in blob_numbers:
                    report.blockers.append(
                        f"{entry.company}: bullet claims {match.group(0)!r}, "
                        "no such figure in profile"
                    )

    for skill in tailored.highlighted_skills:
        token = skill.strip().lower()
        if token and token not in blob_lower:
            report.warnings.append(f"skill {skill!r} not traceable to profile")

    # Dedupe, keeping order — the same invented token across several bullets is
    # one problem, not five.
    report.blockers = list(dict.fromkeys(report.blockers))
    report.warnings = list(dict.fromkeys(report.warnings))
    return report
