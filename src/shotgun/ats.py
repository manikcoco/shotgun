"""Identify which ATS an apply URL belongs to.

Every filler in shotgun/apply/ registers against one of these names. Detection
is URL-pattern based because that is the only signal available before we open
a browser.
"""

from __future__ import annotations

import re
from enum import StrEnum


class ATS(StrEnum):
    GREENHOUSE = "greenhouse"
    LEVER = "lever"
    ASHBY = "ashby"
    WORKDAY = "workday"
    ICIMS = "icims"
    RIPPLING = "rippling"
    DEEL = "deel"
    HIBOB = "hibob"
    SMARTRECRUITERS = "smartrecruiters"
    WORKABLE = "workable"
    TEAMTAILOR = "teamtailor"
    RECRUITEE = "recruitee"
    PERSONIO = "personio"
    JOBVITE = "jobvite"
    BAMBOOHR = "bamboohr"
    TALEO = "taleo"
    SUCCESSFACTORS = "successfactors"
    LINKEDIN = "linkedin"
    UNKNOWN = "unknown"


# Ordered most-specific-first. Matched against the full apply URL.
PATTERNS: list[tuple[re.Pattern[str], ATS]] = [
    (re.compile(r"boards\.greenhouse\.io|job-boards\.greenhouse\.io"
                r"|greenhouse\.io/embed"), ATS.GREENHOUSE),
    (re.compile(r"jobs\.lever\.co|hire\.lever\.co"), ATS.LEVER),
    (re.compile(r"jobs\.ashbyhq\.com|ashbyhq\.com/[^/]+/jobs"), ATS.ASHBY),
    (re.compile(r"\.wd\d+\.myworkdayjobs\.com|myworkdayjobs\.com|workday\.com"), ATS.WORKDAY),
    (re.compile(r"\.icims\.com"), ATS.ICIMS),
    (re.compile(r"ats\.rippling\.com|rippling\.com/.*/jobs"), ATS.RIPPLING),
    (re.compile(r"jobs\.deel\.com|deel\.com/careers"), ATS.DEEL),
    (re.compile(r"\.hibob\.com|app\.hibob\.com"), ATS.HIBOB),
    (re.compile(r"jobs\.smartrecruiters\.com|careers\.smartrecruiters\.com"), ATS.SMARTRECRUITERS),
    (re.compile(r"apply\.workable\.com|\.workable\.com"), ATS.WORKABLE),
    (re.compile(r"\.teamtailor\.com"), ATS.TEAMTAILOR),
    (re.compile(r"\.recruitee\.com"), ATS.RECRUITEE),
    (re.compile(r"\.personio\.(de|com)|jobs\.personio"), ATS.PERSONIO),
    (re.compile(r"jobs\.jobvite\.com|\.jobvite\.com"), ATS.JOBVITE),
    (re.compile(r"\.bamboohr\.com"), ATS.BAMBOOHR),
    (re.compile(r"taleo\.net"), ATS.TALEO),
    (re.compile(r"successfactors\.(com|eu)|jobs\.sap\.com"), ATS.SUCCESSFACTORS),
    (re.compile(r"linkedin\.com/jobs"), ATS.LINKEDIN),
]


def detect(url: str | None) -> ATS:
    if not url:
        return ATS.UNKNOWN
    for pattern, ats in PATTERNS:
        if pattern.search(url):
            return ats
    return ATS.UNKNOWN


def board_token(url: str, ats: ATS) -> str | None:
    """Pull the company slug out of a job-board URL, for the public APIs."""
    patterns = {
        ATS.GREENHOUSE: r"greenhouse\.io/(?:embed/job_board\?for=)?([\w-]+)",
        ATS.LEVER: r"lever\.co/([\w-]+)",
        ATS.ASHBY: r"ashbyhq\.com/([\w-]+)",
    }
    pattern = patterns.get(ats)
    if not pattern:
        return None
    match = re.search(pattern, url)
    return match.group(1) if match else None
