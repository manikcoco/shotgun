"""Lever application filler.

Lever's hosted forms are consistent across employers: predictable `name`
attributes (`name`, `email`, `phone`, `resume`) on a single-page form. Custom
questions are rendered as `cards[...]` fields and vary per employer, so they
land in `unanswered_questions`.
"""

from __future__ import annotations

import logging
from pathlib import Path

from ..answers import selector_key
from ..ats import ATS
from .base import FillResult, screenshot, try_fill, try_upload, visible_questions

log = logging.getLogger(__name__)


class LeverFiller:
    ats = str(ATS.LEVER)

    FIELDS = {
        "full_name": ["input[name='name']", "#name"],
        "email": ["input[name='email']", "#email"],
        "phone": ["input[name='phone']", "#phone"],
        "location": ["input[name='location']", "#location"],
        "org": ["input[name='org']", "#org"],
    }
    URL_FIELDS = {
        "linkedin": ["input[name='urls[LinkedIn]']", "input[name*='LinkedIn']"],
        "github": ["input[name='urls[GitHub]']", "input[name*='GitHub']"],
        "portfolio": ["input[name='urls[Portfolio]']", "input[name*='Portfolio']"],
    }
    RESUME_INPUTS = [
        "input[name='resume']",
        "input[type='file'][name*='resume']",
        "input[type='file']",
    ]
    COVER_INPUTS = [
        "textarea[name='comments']",
        "textarea[name*='cover']",
        "#additional-information",
    ]
    SUBMIT = "button[type='submit'], .template-btn-submit, button:has-text('Submit application')"

    def fill(self, page, profile, resume_pdf: Path, cover_letter, answers) -> FillResult:
        result = FillResult(ok=False, ats=self.ats, submit_selector=self.SUBMIT)

        try:
            page.wait_for_load_state("domcontentloaded", timeout=30_000)

            # A Lever job URL shows the JD; /apply shows the form.
            if "/apply" not in page.url:
                try:
                    page.locator("a:has-text('Apply for this job'), a.postings-btn").first.click(
                        timeout=2000
                    )
                    page.wait_for_load_state("domcontentloaded", timeout=15_000)
                except Exception:
                    log.debug("no apply link found; assuming already on the form")

            current = profile.roles[0] if profile.roles else None
            values = {
                "full_name": profile.contact.name,
                "email": profile.contact.email,
                "phone": profile.contact.phone,
                "location": profile.contact.location,
                "org": current.company if current else None,
            }
            for field, selectors in self.FIELDS.items():
                if try_fill(page, selectors, values.get(field)):
                    result.filled_fields.append(field)
                else:
                    result.skipped_fields.append(field)

            urls = {
                "linkedin": profile.contact.linkedin,
                "github": profile.contact.github,
                "portfolio": profile.contact.website,
            }
            for field, selectors in self.URL_FIELDS.items():
                if try_fill(page, selectors, urls.get(field)):
                    result.filled_fields.append(field)

            if try_upload(page, self.RESUME_INPUTS, resume_pdf):
                result.filled_fields.append("resume")
                page.wait_for_timeout(1500)
            else:
                result.skipped_fields.append("resume")

            if cover_letter and try_fill(page, self.COVER_INPUTS, cover_letter):
                result.filled_fields.append("cover_letter")

            for key, value in (answers or {}).items():
                safe = selector_key(key)
                if not safe:
                    continue
                if try_fill(page, [f"input[name*='{safe}' i]",
                                   f"textarea[name*='{safe}' i]"], value):
                    result.filled_fields.append(f"answer:{safe}")

            result.unanswered_questions = visible_questions(page)
            result.screenshot = screenshot(page, "lever-form")
            result.ok = "resume" in result.filled_fields and "email" in result.filled_fields
            if not result.ok:
                result.error = f"core fields missing — skipped: {', '.join(result.skipped_fields)}"

        except Exception as exc:
            result.error = f"{type(exc).__name__}: {exc}"
            try:
                result.screenshot = screenshot(page, "lever-error")
            except Exception:
                pass

        return result
