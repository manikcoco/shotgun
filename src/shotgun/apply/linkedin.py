"""LinkedIn Easy Apply filler.

Easy Apply is a multi-step modal, not a page: you click "Easy Apply", then walk
Next -> Next -> Review, and only the last step has Submit. Each step renders a
different fieldset, so this filler loops — fill what's visible, advance, repeat
— rather than filling once.

Two things worth knowing before you use this:

1. It requires a logged-in session. Run `shotgun browser-login` once; the
   cookies live in the persistent Chromium profile and this filler reuses
   them. shotgun never handles your password.
2. LinkedIn's terms prohibit automated interaction, and they enforce it with
   account restrictions. This filler therefore stops at the Review step and
   never clicks Submit — you press the button. Keep volumes low and keep the
   browser visible (`SHOTGUN_HEADLESS` unset) so you can see what it does.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from ..answers import selector_key
from ..ats import ATS
from .base import FillResult, screenshot, try_fill, try_upload

log = logging.getLogger(__name__)

MAX_STEPS = 8   # Easy Apply flows are 2-5 steps; 8 means something looped.

EASY_APPLY_BUTTON = [
    "button.jobs-apply-button",
    "button:has-text('Easy Apply')",
    "button[aria-label*='Easy Apply']",
]
MODAL = "div.jobs-easy-apply-modal, div[role='dialog']"
NEXT_BUTTON = [
    "button[aria-label='Continue to next step']",
    "button:has-text('Next')",
    "button:has-text('Continue')",
]
REVIEW_BUTTON = [
    "button[aria-label='Review your application']",
    "button:has-text('Review')",
]
SUBMIT_BUTTON = [
    "button[aria-label='Submit application']",
    "button:has-text('Submit application')",
]

# Easy Apply's own fields use unstable generated ids, so match on the label
# text that sits next to them.
LABEL_FIELDS: list[tuple[str, list[str]]] = [
    ("email", [r"email"]),
    ("phone", [r"mobile phone number", r"phone"]),
    ("first_name", [r"first name"]),
    ("last_name", [r"last name"]),
    ("city", [r"city", r"location"]),
    ("linkedin", [r"linkedin profile"]),
    ("website", [r"website", r"portfolio"]),
]


class LinkedInFiller:
    ats = str(ATS.LINKEDIN)

    def fill(self, page, profile, resume_pdf: Path, cover_letter, answers) -> FillResult:
        result = FillResult(ok=False, ats=self.ats)

        try:
            page.wait_for_load_state("domcontentloaded", timeout=30_000)
            page.wait_for_timeout(1500)

            if self._looks_logged_out(page):
                result.error = (
                    "not logged into LinkedIn — run `shotgun browser-login` first"
                )
                return result

            if not self._click_any(page, EASY_APPLY_BUTTON, timeout=4000):
                result.error = (
                    "no Easy Apply button — this posting routes to an external "
                    "ATS, so let the normal filler handle its apply URL"
                )
                return result

            page.wait_for_selector(MODAL, timeout=15_000)
            page.wait_for_timeout(800)

            reached_review = False
            for step in range(MAX_STEPS):
                filled = self._fill_visible_step(page, profile, resume_pdf, cover_letter, answers)
                result.filled_fields.extend(f"step{step}:{f}" for f in filled)

                # Anything still empty on this step needs a human.
                unanswered = self._unanswered_in_modal(page)
                result.unanswered_questions.extend(unanswered)

                if self._find_any(page, SUBMIT_BUTTON):
                    reached_review = True
                    result.submit_selector = SUBMIT_BUTTON[0]
                    break

                advanced = (
                    self._click_any(page, REVIEW_BUTTON, timeout=2000)
                    or self._click_any(page, NEXT_BUTTON, timeout=2000)
                )
                if not advanced:
                    result.error = (
                        f"stuck on step {step}: no Next/Review/Submit button. "
                        f"{len(unanswered)} unanswered field(s) probably block it"
                    )
                    break
                page.wait_for_timeout(1200)

            # Dedupe while preserving order.
            result.unanswered_questions = list(dict.fromkeys(result.unanswered_questions))
            result.screenshot = screenshot(page, "linkedin-easyapply")

            if reached_review:
                result.ok = True
                result.error = None
            elif not result.error:
                result.error = f"did not reach Review within {MAX_STEPS} steps"

        except Exception as exc:
            result.error = f"{type(exc).__name__}: {exc}"
            try:
                result.screenshot = screenshot(page, "linkedin-error")
            except Exception:
                pass

        return result

    # -------------------------------------------------------------- helpers

    @staticmethod
    def _looks_logged_out(page) -> bool:
        if "/authwall" in page.url or "/login" in page.url:
            return True
        try:
            signin = "a:has-text('Sign in'), button:has-text('Sign in')"
            return page.locator(signin).first.is_visible(timeout=1500)
        except Exception:
            return False

    @staticmethod
    def _click_any(page, selectors: list[str], *, timeout: int = 2500) -> bool:
        for selector in selectors:
            try:
                locator = page.locator(selector).first
                locator.wait_for(state="visible", timeout=timeout)
                locator.click()
                return True
            except Exception:
                continue
        return False

    @staticmethod
    def _find_any(page, selectors: list[str]) -> bool:
        for selector in selectors:
            try:
                if page.locator(selector).first.is_visible(timeout=800):
                    return True
            except Exception:
                continue
        return False

    def _fill_visible_step(self, page, profile, resume_pdf, cover_letter, answers) -> list[str]:
        """Fill whatever this step of the modal is showing."""
        filled: list[str] = []
        name_parts = profile.contact.name.split()

        values = {
            "email": profile.contact.email,
            "phone": profile.contact.phone,
            "first_name": name_parts[0] if name_parts else "",
            "last_name": " ".join(name_parts[1:]) if len(name_parts) > 1 else "",
            "city": profile.contact.location,
            "linkedin": profile.contact.linkedin,
            "website": profile.contact.website or profile.contact.github,
        }

        for field, label_patterns in LABEL_FIELDS:
            value = values.get(field)
            if not value:
                continue
            for pattern in label_patterns:
                selector = f"{MODAL} input:below(:text-matches('{pattern}', 'i'))"
                if try_fill(page, [selector], value, timeout=1200):
                    filled.append(field)
                    break

        # Resume: LinkedIn usually offers a stored resume plus an upload input.
        if try_upload(page, [f"{MODAL} input[type='file']"], resume_pdf, timeout=2000):
            filled.append("resume")
            page.wait_for_timeout(1500)

        if cover_letter:
            selector = f"{MODAL} textarea"
            if try_fill(page, [selector], cover_letter, timeout=1200):
                filled.append("cover_letter")

        # Screening questions LinkedIn asks constantly. Answered from the
        # answer store so they're consistent across applications.
        for key, value in (answers or {}).items():
            safe = selector_key(key)
            if not safe:
                continue
            selector = f"{MODAL} input:below(:text-matches('{re.escape(safe)}', 'i'))"
            if try_fill(page, [selector], value, timeout=1000):
                filled.append(f"answer:{safe}")

        return filled

    @staticmethod
    def _unanswered_in_modal(page) -> list[str]:
        """Label text for still-empty fields inside the modal."""
        script = """
        () => {
          const modal = document.querySelector(
            "div.jobs-easy-apply-modal, div[role='dialog']"
          );
          if (!modal) return [];
          const out = [];
          const fields = modal.querySelectorAll(
            'input:not([type=hidden]):not([type=file]), textarea, select'
          );
          for (const el of fields) {
            if (el.offsetParent === null) continue;
            const filled = (el.type === 'checkbox' || el.type === 'radio')
              ? el.checked : (el.value || '').trim().length > 0;
            if (filled) continue;
            let label = '';
            if (el.labels && el.labels.length) label = el.labels[0].innerText;
            if (!label) label = el.getAttribute('aria-label') || el.placeholder || el.name || '';
            label = label.trim().replace(/\\s+/g, ' ');
            if (label && !out.includes(label)) out.push(label);
          }
          return out.slice(0, 20);
        }
        """
        try:
            return page.evaluate(script) or []
        except Exception:
            return []
