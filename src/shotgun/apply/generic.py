"""Best-effort filler for any ATS without a dedicated implementation.

Works by matching on field semantics rather than per-site selectors: input
type, `autocomplete` token, `name`/`id` substrings, and associated label text.
That covers the common name/email/phone/resume shape on most ATSes, which is
enough to get a form to the review queue with the boring parts done.

It will not handle multi-step wizards (Workday, iCIMS), custom React comboboxes,
or per-employer screening questions. Those come back in
`unanswered_questions` for you to finish in the browser.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from ..answers import match_for, selector_key
from ..geo import country_name
from .base import (
    FillResult,
    required_questions,
    screenshot,
    try_choose_by_label,
    try_fill,
    try_fill_by_label,
    try_upload,
    visible_questions,
)

log = logging.getLogger(__name__)

# (field, [selectors]) — ordered from most to least reliable.
SEMANTIC_FIELDS: list[tuple[str, list[str]]] = [
    ("first_name", [
        "input[autocomplete='given-name']",
        "input[name*='first' i]", "input[id*='first' i]",
        "input[aria-label*='first name' i]",
    ]),
    ("last_name", [
        "input[autocomplete='family-name']",
        "input[name*='last' i]", "input[id*='last' i]",
        "input[name*='surname' i]", "input[aria-label*='last name' i]",
    ]),
    ("full_name", [
        "input[autocomplete='name']",
        "input[name='name']", "input[id='name']",
        "input[name*='fullname' i]", "input[aria-label*='full name' i]",
    ]),
    ("email", [
        "input[type='email']", "input[autocomplete='email']",
        "input[name*='email' i]", "input[id*='email' i]",
    ]),
    ("phone", [
        "input[type='tel']", "input[autocomplete='tel']",
        "input[name*='phone' i]", "input[id*='phone' i]",
        "input[name*='mobile' i]",
    ]),
    ("location", [
        "input[name*='location' i]", "input[id*='location' i]",
        "input[name*='city' i]", "input[autocomplete='address-level2']",
    ]),
    ("linkedin", [
        "input[name*='linkedin' i]", "input[id*='linkedin' i]",
        "input[aria-label*='linkedin' i]",
    ]),
    ("country", [
        "input[name*='country' i]", "input[id*='country' i]",
    ]),
    ("github", [
        "input[name*='github' i]", "input[id*='github' i]",
        "input[aria-label*='github' i]",
    ]),
    ("website", [
        "input[name*='website' i]", "input[name*='portfolio' i]",
        "input[id*='website' i]",
    ]),
]

# Visible-label patterns, tried when attribute matching finds nothing. Keyed
# the same as SEMANTIC_FIELDS. Anchored loosely because forms phrase these a
# dozen ways ("Linkedin", "LinkedIn Profile", "LinkedIn URL").
LABEL_PATTERNS: dict[str, list[str]] = {
    "first_name": [r"^\s*first name"],
    "last_name": [r"^\s*last name", r"^\s*surname"],
    "full_name": [r"^\s*name\s*\*?\s*$", r"^\s*full name"],
    "email": [r"^\s*e-?mail"],
    "phone": [r"^\s*(mobile |cell )?phone", r"^\s*telephone"],
    "location": [r"^\s*(city|location|where are you based)"],
    "country": [r"country of residence", r"^\s*country\s*\*?\s*$",
                r"^\s*(residing|resident) country"],
    "linkedin": [r"linked-?in"],
    "github": [r"git-?hub"],
    "website": [r"^\s*(website|portfolio|personal site)"],
}

RESUME_INPUTS = [
    "input[type='file'][name*='resume' i]",
    "input[type='file'][id*='resume' i]",
    "input[type='file'][name*='cv' i]",
    "input[type='file'][accept*='pdf']",
    "input[type='file']",
]

COVER_INPUTS = [
    "textarea[name*='cover' i]", "textarea[id*='cover' i]",
    "textarea[aria-label*='cover letter' i]",
]

APPLY_TRIGGERS = [
    "button:has-text('Apply for this job')",
    "button:has-text('Apply now')",
    "a:has-text('Apply now')",
    "button:has-text('Apply')",
    "a:has-text('Apply')",
]

SUBMIT = (
    "button[type='submit'], input[type='submit'], "
    "button:has-text('Submit application'), button:has-text('Submit')"
)

# Control labels that mean "send the application". _open_form refuses to click
# anything matching this, however apply-like the rest of the label looks.
_SUBMIT_TEXT = re.compile(
    r"\b(submit|send\s+application|finish|complete\s+application)\b", re.I
)


class GenericFiller:
    def __init__(self, ats: str = "unknown"):
        self.ats = ats

    def fill(self, page, profile, resume_pdf: Path, cover_letter, answers,
             questions_by_key: dict[str, str] | None = None) -> FillResult:
        result = FillResult(ok=False, ats=self.ats, submit_selector=SUBMIT)
        questions_by_key = questions_by_key or {}

        try:
            page.wait_for_load_state("domcontentloaded", timeout=30_000)
            page.wait_for_timeout(1200)   # let client-rendered forms mount

            self._open_form(page)

            name_parts = profile.contact.name.split()
            values = {
                "first_name": name_parts[0] if name_parts else "",
                "last_name": " ".join(name_parts[1:]) if len(name_parts) > 1 else "",
                "full_name": profile.contact.name,
                "email": profile.contact.email,
                "phone": profile.contact.phone,
                "location": profile.contact.location,
                "country": country_name(profile.contact.location),
                "linkedin": profile.contact.linkedin,
                "github": profile.contact.github,
                "website": profile.contact.website or profile.contact.github,
            }

            # Don't fill both a split name and a combined one.
            filled_split = False
            for field, selectors in SEMANTIC_FIELDS:
                if field == "full_name" and filled_split:
                    continue
                value = values.get(field)
                placed = try_fill(page, selectors, value)
                if not placed:
                    # Fall back to the visible label. Ashby fills nothing by
                    # attribute, so without this the core fields stay empty on
                    # every Ashby form.
                    placed = try_fill_by_label(
                        page, LABEL_PATTERNS.get(field, []), value
                    )
                if placed:
                    result.filled_fields.append(field)
                    if field == "last_name":
                        filled_split = True

            if try_upload(page, RESUME_INPUTS, resume_pdf):
                result.filled_fields.append("resume")
                page.wait_for_timeout(1500)
            else:
                result.skipped_fields.append("resume")

            if cover_letter and try_fill(page, COVER_INPUTS, cover_letter):
                result.filled_fields.append("cover_letter")

            for key, value in (answers or {}).items():
                safe = selector_key(key)
                if not safe:
                    continue
                selectors = [
                    f"input[name*='{safe}' i]",
                    f"textarea[name*='{safe}' i]",
                    f"input[id*='{safe}' i]",
                ]
                if try_fill(page, selectors, value):
                    result.filled_fields.append(f"answer:{safe}")

            # Drive from the page's own labels, not from our phrasing of the
            # question. Filling by our stored text failed on "Passport
            # Country": the answer is keyed to "Passport country /
            # nationality?", which does not match the form's label at all.
            # It was also slow, trying every stored answer against the page
            # with multi-second timeouts; there are far fewer blank fields
            # than stored answers.
            for label in visible_questions(page):
                hit = match_for(label, answers or {})
                if not hit:
                    continue
                key, value = hit
                if try_fill_by_label(page, [re.escape(label[:60])], value):
                    result.filled_fields.append(f"answer:{key}")
                elif try_choose_by_label(page, label, value):
                    result.filled_fields.append(f"choice:{key}")

            result.unanswered_questions = visible_questions(page)
            result.required_unanswered = required_questions(page)
            result.screenshot = screenshot(page, f"{self.ats}-generic")

            # Success bar: resume attached and an email in place. Anything less
            # isn't worth a human's review slot.
            result.ok = "resume" in result.filled_fields and "email" in result.filled_fields
            if not result.ok:
                result.error = (
                    "generic filler could not place the core fields — "
                    f"this ATS ({self.ats}) probably needs a dedicated filler"
                )

        except Exception as exc:
            result.error = f"{type(exc).__name__}: {exc}"
            try:
                result.screenshot = screenshot(page, f"{self.ats}-error")
            except Exception:
                pass

        return result

    @staticmethod
    def _form_present(page) -> bool:
        """Is an application form already on the page?"""
        try:
            return page.locator(
                "input[type='file'], input[type='email'], input[autocomplete='email']"
            ).count() > 0
        except Exception:
            return False

    @classmethod
    def _open_form(cls, page) -> None:
        """Click through to the form if the URL lands on a job description.

        Guarded, because this runs *before* any field is filled and on several
        ATSes (Personio, Teamtailor) the final submit control also reads
        "Apply" — clicking it blindly fires an empty application, which can
        burn the posting with an "already applied" record. Two protections:
        never click when a form is already present, and treat a click that
        reveals no new fields as the wrong button.
        """
        if cls._form_present(page):
            log.debug("form already on the page; not clicking any apply trigger")
            return

        for selector in APPLY_TRIGGERS:
            try:
                locator = page.locator(selector).first
                label = (locator.inner_text(timeout=1000) or "").strip()
                if _SUBMIT_TEXT.search(label):
                    log.debug("skipping %r — looks like a submit control", label)
                    continue
                locator.click(timeout=1500)
                page.wait_for_timeout(1500)
            except Exception:
                continue

            if cls._form_present(page):
                return
            log.debug("clicking %r revealed no form fields; trying the next trigger",
                      selector)
