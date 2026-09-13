"""Shared data shapes. Pydantic where Claude fills them in, dataclasses elsewhere."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class Stage(StrEnum):
    """Where an application is in the pipeline."""

    DISCOVERED = "discovered"
    FILTERED_OUT = "filtered_out"      # failed the cheap rule filter
    SCORED_LOW = "scored_low"          # Claude scored it under the threshold
    QUEUED = "queued"                  # passed scoring, not yet prepared
    TAILORING = "tailoring"
    AWAITING_APPROVAL = "awaiting_approval"
    SUBMITTED = "submitted"
    ACKNOWLEDGED = "acknowledged"
    SCREENING = "screening"
    INTERVIEW = "interview"
    OFFER = "offer"
    REJECTED = "rejected"
    WITHDRAWN = "withdrawn"
    FAILED = "failed"                  # filler blew up


TERMINAL_STAGES = {Stage.REJECTED, Stage.WITHDRAWN, Stage.OFFER}


@dataclass
class Job:
    source: str
    company: str
    title: str
    url: str
    location: str | None = None
    country: str | None = None
    remote: bool = False
    description: str | None = None
    apply_url: str | None = None
    ats: str | None = None
    salary_min: float | None = None
    salary_max: float | None = None
    salary_currency: str | None = None
    posted_at: str | None = None
    source_id: str | None = None
    id: int | None = None
    discovered_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))

    @property
    def fingerprint(self) -> str:
        """Stable identity across sources, so the same role found on LinkedIn
        and on the company's Greenhouse board dedupes to one row."""
        key = f"{self.company.strip().lower()}|{self.title.strip().lower()}"
        return hashlib.sha256(key.encode()).hexdigest()[:32]


class JobScore(BaseModel):
    """Claude's verdict on one posting. Filled via messages.parse()."""

    score: int = Field(ge=0, le=100, description="Fit against the candidate profile, 0-100")
    level: str = Field(description="One of: staff, lead, manager, other")
    reasoning: str = Field(description="Two sentences at most on why this score")
    # Only genuine hard blockers belong here — anything in this list sends the
    # posting to scored_low regardless of score. Ordinary friction goes in
    # `concerns`. Conflating the two cost 23 of 24 qualified roles on the first
    # real run, most of them to invented work-permit objections about countries
    # the candidate is actively targeting.
    dealbreakers: list[str] = Field(
        default_factory=list,
        description="Disqualifying blockers only: clearance, citizenship, "
                    "sponsorship explicitly ruled out, a hard requirement the "
                    "candidate plainly lacks",
    )
    concerns: list[str] = Field(
        default_factory=list,
        description="Friction worth knowing that does NOT disqualify: "
                    "relocation inside the target regions, onsite/hybrid, "
                    "seniority stretch, missing preferred skills",
    )
    key_requirements: list[str] = Field(
        default_factory=list,
        description="The 5-8 requirements the resume should speak to",
    )
    # Claude's read on mobility, cross-checked against the regex signals in
    # visa.py. The regexes catch explicit statements; Claude catches the
    # implied ones ("open to candidates across EMEA").
    visa_sponsorship: str = Field(
        default="unknown",
        description="Does the posting sponsor a work visa? yes | no | unknown",
    )
    relocation_support: str = Field(
        default="unknown",
        description="Does it offer relocation assistance? yes | no | unknown",
    )
    employment_type: str = Field(
        default="unknown",
        description="permanent | contract | unknown",
    )
    hiring_countries: list[str] = Field(
        default_factory=list,
        description="ISO country codes the posting will actually hire into, if stated",
    )


class RoleBullets(BaseModel):
    """Rewritten bullets for one role from the profile."""

    company: str = Field(description="Company name, copied exactly from the profile")
    bullets: list[str] = Field(description="Rewritten achievement bullets for that role")


class TailoredResume(BaseModel):
    """The JD-specific rewrite. Only reorders and rephrases what the profile
    already contains — see the system prompt in tailor.py."""

    headline: str = Field(description="One-line professional headline for this role")
    summary: str = Field(description="3-4 sentence summary aimed at this JD")
    highlighted_skills: list[str] = Field(description="8-14 skills, most relevant first")
    # A list, not a dict keyed by company. Structured outputs compile a
    # `dict[str, ...]` down to `{"properties": {}, "additionalProperties":
    # false}` — a schema that admits only the empty object — so the model
    # could never emit a single role and every resume silently fell back to
    # untailored profile bullets. Verified against the API before changing it.
    role_bullets: list[RoleBullets] = Field(
        description="One entry per role from the profile, most recent first"
    )
    omitted: list[str] = Field(
        default_factory=list,
        description="Profile items deliberately left out as irrelevant to this JD",
    )

    def bullets_for(self, company: str) -> list[str] | None:
        """Bullets for one profile role, or None if the rewrite skipped it.

        Matched case- and whitespace-insensitively: the model returning
        "GitLab Inc." for a profile's "GitLab" should not cost the role its
        tailored bullets.
        """
        key = company.strip().lower()
        for entry in self.role_bullets:
            if entry.company.strip().lower() == key:
                return entry.bullets or None
        return None


class CoverLetter(BaseModel):
    body: str = Field(description="Plain-text cover letter, 200-300 words")


@dataclass
class Answer:
    """A reusable application-form answer."""

    key: str
    question: str
    value: str
    confidence: str = "confirmed"   # confirmed | inferred | missing
    sensitive: bool = False
    remember: bool = True
