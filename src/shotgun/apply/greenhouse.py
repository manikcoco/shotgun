"""Greenhouse application filler.

Greenhouse is the friendliest target: server-rendered form, stable `id`
attributes on the core fields, a real file input for the resume. Custom
questions vary per employer and land in `unanswered_questions`.
"""

from __future__ import annotations

import logging
from pathlib import Path

from ..answers import selector_key
from ..ats import ATS
from .base import (
    FillResult,
    screenshot,
    try_fill,
    try_upload,
    visible_questions,
)

log = logging.getLogger(__name__)


class GreenhouseFiller:
    ats = str(ATS.GREENHOUSE)

    # Greenhouse has two generations of markup live at once (boards.greenhouse.io
    # and the newer job-boards.greenhouse.io), so each field lists both.
    FIELDS = {
        "first_name": [
            "#first_name", "input[name='first_name']",
            "input[autocomplete='given-name']",
        ],
        "last_name": ["#last_name", "input[name='last_name']", "input[autocomplete='family-name']"],
        "email": ["#email", "input[name='email']", "input[type='email']"],
        "phone": ["#phone", "input[name='phone']", "input[type='tel']"],
    }
    RESUME_INPUTS = [
        "input[type='file'][id*='resume']",
        "input[type='file'][name*='resume']",
        "#resume_fieldset input[type='file']",
        "input[type='file']",
    ]
    COVER_INPUTS = [
        "textarea[id*='cover_letter']",
        "textarea[name*='cover_letter']",
        "#cover_letter_text",
    ]
    LINKEDIN_INPUTS = [
        "input[id*='linkedin']",
        "input[name*='linkedin']",
        "input[id*='urls']",
    ]
    SUBMIT = "#submit_app, button[type='submit'], input[type='submit']"

    def fill(self, page, profile, resume_pdf: Path, cover_letter, answers) -> FillResult:
        result = FillResult(ok=False, ats=self.ats, submit_selector=self.SUBMIT)

        try:
            page.wait_for_load_state("domcontentloaded", timeout=30_000)

            # Some boards render the form behind an "Apply for this job" toggle.
            for label in ["text=Apply for this job", "text=Apply Now", "button:has-text('Apply')"]:
                try:
                    page.locator(label).first.click(timeout=1500)
                    page.wait_for_timeout(600)
                    break
                except Exception:
                    continue

            name_parts = profile.contact.name.split()
            first = name_parts[0] if name_parts else ""
            last = " ".join(name_parts[1:]) if len(name_parts) > 1 else ""

            values = {
                "first_name": first,
                "last_name": last,
                "email": profile.contact.email,
                "phone": profile.contact.phone,
            }
            for field, selectors in self.FIELDS.items():
                if try_fill(page, selectors, values.get(field)):
                    result.filled_fields.append(field)
                else:
                    result.skipped_fields.append(field)

            if try_upload(page, self.RESUME_INPUTS, resume_pdf):
                result.filled_fields.append("resume")
                page.wait_for_timeout(1200)   # let the upload settle
            else:
                result.skipped_fields.append("resume")

            if cover_letter and try_fill(page, self.COVER_INPUTS, cover_letter):
                result.filled_fields.append("cover_letter")

            if profile.contact.linkedin:
                if try_fill(page, self.LINKEDIN_INPUTS, profile.contact.linkedin):
                    result.filled_fields.append("linkedin")

            # Reuse any answers we've already confirmed for this kind of question.
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

            result.unanswered_questions = visible_questions(page)
            result.screenshot = screenshot(page, f"greenhouse-{page.url.split('/')[-1][:40]}")
            result.ok = "resume" in result.filled_fields and "email" in result.filled_fields

            if not result.ok:
                result.error = (
                    "core fields missing — "
                    f"skipped: {', '.join(result.skipped_fields)}"
                )

        except Exception as exc:
            result.error = f"{type(exc).__name__}: {exc}"
            try:
                result.screenshot = screenshot(page, "greenhouse-error")
            except Exception:
                pass

        return result
