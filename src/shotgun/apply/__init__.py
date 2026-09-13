"""Filler registry and the prepare-one-application entry point."""

from __future__ import annotations

import logging
from pathlib import Path

from ..ats import ATS, detect
from .base import FillResult, browser_session
from .generic import GenericFiller
from .greenhouse import GreenhouseFiller
from .lever import LeverFiller
from .linkedin import LinkedInFiller

log = logging.getLogger(__name__)

# ATSes with a dedicated filler. Everything else falls through to
# GenericFiller, which handles the common single-page form shape.
#
# Not yet specialised (generic filler will attempt, expect partial results):
#   ashby           — React form, needs combobox handling
#   workday         — multi-step wizard, per-tenant account creation
#   icims           — iframe-heavy, multi-step
#   rippling, deel, hibob, smartrecruiters, workable, teamtailor,
#   personio, jobvite, bamboohr, taleo, successfactors
FILLERS = {
    str(ATS.GREENHOUSE): GreenhouseFiller,
    str(ATS.LEVER): LeverFiller,
    str(ATS.LINKEDIN): LinkedInFiller,
}

# These need a logged-in session and/or multi-step navigation the generic
# filler cannot do. We refuse rather than pretend.
NEEDS_DEDICATED_FILLER = {
    str(ATS.WORKDAY),
    str(ATS.ICIMS),
    str(ATS.TALEO),
    str(ATS.SUCCESSFACTORS),
}


def filler_for(url: str | None):
    """Pick a filler for an apply URL."""
    ats = detect(url)
    cls = FILLERS.get(str(ats))
    if cls:
        return cls()
    return GenericFiller(ats=str(ats))


def prepare(
    apply_url: str,
    profile,
    resume_pdf: Path,
    cover_letter: str | None = None,
    answers: dict[str, str] | None = None,
    *,
    keep_open: bool = False,
    questions_by_key: dict[str, str] | None = None,
) -> FillResult:
    """Open the form, fill it, stop before submit.

    With `keep_open=True` the browser stays up so you can finish the form by
    hand — that's what `shotgun review` uses.
    """
    ats = detect(apply_url)

    if str(ats) in NEEDS_DEDICATED_FILLER:
        return FillResult(
            ok=False,
            ats=str(ats),
            error=(
                f"{ats} needs a dedicated filler (multi-step wizard or login "
                f"required). Open it yourself: {apply_url}"
            ),
        )

    filler = filler_for(apply_url)
    log.info("filling %s with %s", apply_url, type(filler).__name__)

    with browser_session() as context:
        page = context.new_page()
        try:
            page.goto(apply_url, wait_until="domcontentloaded", timeout=45_000)
            try:
                result = filler.fill(page, profile, resume_pdf, cover_letter,
                                     answers or {}, questions_by_key or {})
            except TypeError:
                # Dedicated fillers do not take the question map yet.
                result = filler.fill(page, profile, resume_pdf, cover_letter,
                                     answers or {})
            if keep_open:
                input("\n  Form is open in the browser. Finish it, then press Enter here… ")
            return result
        finally:
            if not keep_open:
                page.close()
