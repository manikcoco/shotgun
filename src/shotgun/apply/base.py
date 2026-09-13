"""Filler protocol and the shared browser session.

A filler's contract: open the apply URL, put the candidate's data into the
form, attach the tailored resume, and stop. It never clicks the final submit —
that happens in `shotgun review`, under your eye, or in submit.py when apply
mode is `auto`.
"""

from __future__ import annotations

import logging
import re
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from ..config import Env

if TYPE_CHECKING:
    from playwright.sync_api import Page

    from ..profile import Profile

log = logging.getLogger(__name__)


@dataclass
class FillResult:
    """What a filler managed to do."""

    ok: bool
    ats: str
    filled_fields: list[str] = field(default_factory=list)
    skipped_fields: list[str] = field(default_factory=list)
    unanswered_questions: list[str] = field(default_factory=list)
    # The subset the form itself marks required. Empty means the form could
    # actually be submitted; non-empty means it would be rejected.
    required_unanswered: list[str] = field(default_factory=list)
    screenshot: Path | None = None
    error: str | None = None
    # The selector the review step should click to submit. Fillers set this so
    # review.py doesn't need per-ATS knowledge.
    submit_selector: str | None = None

    def summary(self) -> str:
        if not self.ok:
            return f"failed on {self.ats}: {self.error}"
        parts = [f"{len(self.filled_fields)} fields filled"]
        if self.unanswered_questions:
            parts.append(f"{len(self.unanswered_questions)} questions need you")
        if self.required_unanswered:
            parts.append(f"{len(self.required_unanswered)} REQUIRED still blank")
        if self.skipped_fields:
            parts.append(f"{len(self.skipped_fields)} skipped")
        return ", ".join(parts)


class Filler(Protocol):
    """Implemented once per ATS."""

    ats: str

    def fill(
        self,
        page: Page,
        profile: Profile,
        resume_pdf: Path,
        cover_letter: str | None,
        answers: dict[str, str],
    ) -> FillResult: ...


@contextmanager
def browser_session(*, headless: bool | None = None):
    """A persistent Chromium profile.

    Persistent because half these ATSes want you logged in (LinkedIn always,
    Workday per-tenant), and you should do that login by hand once rather than
    have a script handle your credentials. After the first
    `shotgun browser-login`, the session cookies live in this profile dir.
    """
    from playwright.sync_api import sync_playwright

    from ..browser import launch_persistent

    env = Env.load()
    profile_dir = env.browser_profile
    profile_dir.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        context = launch_persistent(
            p,
            str(profile_dir),
            headless=env.headless if headless is None else headless,
            viewport={"width": 1440, "height": 900},
        )
        try:
            yield context
        finally:
            context.close()


# --------------------------------------------------------------- helpers
# Shared by every filler. Each returns True if it actually did something, so
# fillers can build an accurate filled_fields list.

def try_fill(page: Page, selectors: list[str], value: str | None, *, timeout: int = 2500) -> bool:
    """Fill the first selector that resolves to a visible, editable field."""
    if not value:
        return False
    for selector in selectors:
        try:
            locator = page.locator(selector).first
            locator.wait_for(state="visible", timeout=timeout)
            locator.fill(value)
            return True
        except Exception:
            continue
    return False


def try_fill_by_label(
    page: Page, patterns: list[str], value: str | None, *, timeout: int = 2000
) -> bool:
    """Fill the first field whose visible label matches, by regex.

    Necessary because attribute matching does not work everywhere. Ashby names
    its inputs by internal system id and puts the human text in a separate
    label element, so `input[name*='linkedin']` matches nothing on a form that
    plainly shows a "Linkedin" field. visible_questions() could already read
    those labels, which is how the gap showed up: the filler reported "Github
    Profile" as unanswered while holding the answer.
    """
    if not value:
        return False
    for pattern in patterns:
        try:
            locator = page.get_by_label(re.compile(pattern, re.I)).first
            locator.wait_for(state="visible", timeout=timeout)
            locator.fill(value)
            return True
        except Exception:
            continue
    return False


def try_upload(page: Page, selectors: list[str], path: Path, *, timeout: int = 4000) -> bool:
    for selector in selectors:
        try:
            locator = page.locator(selector).first
            locator.wait_for(state="attached", timeout=timeout)
            locator.set_input_files(str(path))
            return True
        except Exception:
            continue
    return False


def try_select(page: Page, selectors: list[str], value: str | None, *, timeout: int = 2500) -> bool:
    if not value:
        return False
    for selector in selectors:
        try:
            locator = page.locator(selector).first
            locator.wait_for(state="visible", timeout=timeout)
            locator.select_option(label=value)
            return True
        except Exception:
            try:
                locator.select_option(value=value)
                return True
            except Exception:
                continue
    return False


def try_choose_by_label(
    page: Page, question: str, answer: str, *, timeout: int = 1500
) -> bool:
    """Answer a Yes/No question rendered as buttons or radios.

    Ashby renders "Are you over the age of 18?" as a pair of buttons, not a
    select or a text input, so nothing in the fill path could touch it — and
    it is a required field, which meant the form could never be completed.
    """
    if not answer:
        return False
    wanted = answer.strip().lower()
    if wanted not in {"yes", "no", "true", "false"}:
        return False
    label = "Yes" if wanted in {"yes", "true"} else "No"

    try:
        group = page.get_by_text(re.compile(re.escape(question[:48]), re.I)).first
        group.wait_for(state="visible", timeout=timeout)
        container = group.locator("xpath=./ancestor::*[self::div or self::fieldset][1]")
        for builder in (
            lambda: container.get_by_role("radio", name=label, exact=True),
            lambda: container.get_by_role("button", name=label, exact=True),
            lambda: container.get_by_label(label, exact=True),
        ):
            try:
                target = builder().first
                target.wait_for(state="visible", timeout=timeout)
                target.click()
                return True
            except Exception:
                continue
    except Exception:
        return False
    return False


def required_questions(page: Page) -> list[str]:
    """Labels of still-empty fields the form marks as required.

    Distinguishing required from optional is what tells you whether a form
    could actually be submitted. Ashby marks them with an asterisk inside the
    label element.
    """
    script = """
    () => {
      const out = [];
      const fields = document.querySelectorAll(
        'input:not([type=hidden]):not([type=submit]), textarea, select'
      );
      for (const el of fields) {
        if (el.offsetParent === null) continue;
        const filled = (el.type === 'checkbox' || el.type === 'radio')
          ? el.checked : (el.value || '').trim().length > 0;
        if (filled) continue;
        let label = '';
        if (el.labels && el.labels.length) label = el.labels[0].innerText;
        if (!label) label = el.getAttribute('aria-label') || el.placeholder || '';
        const required = el.required || el.getAttribute('aria-required') === 'true'
                         || /\*/.test(label);
        if (!required) continue;
        label = label.replace(/\*/g, '').trim().replace(/\s+/g, ' ');
        if (label && !out.includes(label)) out.push(label);
      }

      // Required questions rendered as a button pair rather than an input.
      // Ashby draws "Are you over the age of 18?*" that way, so it is absent
      // from every input/textarea/select scan — the form could not be
      // submitted while the filler reported nothing outstanding.
      for (const node of document.querySelectorAll('label, legend, p, div, span')) {
        const text = (node.childNodes.length && node.innerText || '').trim();
        if (!text || text.length > 160 || !text.includes('*')) continue;
        if (node.querySelector('input, textarea, select')) continue;
        const group = node.parentElement;
        if (!group) continue;
        const buttons = group.querySelectorAll(
          'button, [role=radio], [role=button], [role=switch]'
        );
        if (buttons.length < 2 || buttons.length > 6) continue;
        let chosen = false;
        for (const b of buttons) {
          if (b.getAttribute('aria-checked') === 'true'
              || b.getAttribute('aria-pressed') === 'true'
              || b.getAttribute('data-state') === 'checked'
              || /selected|active|checked/.test(b.className || '')) chosen = true;
        }
        if (chosen) continue;
        const label = text.replace(/\*/g, '').trim().replace(/\s+/g, ' ');
        if (label && !out.includes(label)) out.push(label);
      }
      return out.slice(0, 20);
    }
    """
    try:
        return page.evaluate(script) or []
    except Exception:
        return []


def screenshot(page: Page, name: str) -> Path:
    from ..config import PRIVATE_DIR

    out = PRIVATE_DIR / "screenshots" / f"{name}.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(out), full_page=True)
    return out


def visible_questions(page: Page) -> list[str]:
    """Collect label text for inputs still empty after a fill pass.

    Deliberately crude — it exists so the approval queue can tell you "this
    form has 3 questions I couldn't answer" rather than silently leaving them
    blank and failing validation on submit.
    """
    script = """
    () => {
      const out = [];
      const fields = document.querySelectorAll(
        'input:not([type=hidden]):not([type=file]):not([type=submit]), textarea, select'
      );
      for (const el of fields) {
        if (el.offsetParent === null) continue;
        const filled = el.type === 'checkbox' || el.type === 'radio'
          ? el.checked : (el.value || '').trim().length > 0;
        if (filled) continue;
        let label = '';
        if (el.labels && el.labels.length) label = el.labels[0].innerText;
        if (!label && el.getAttribute('aria-label')) label = el.getAttribute('aria-label');
        if (!label && el.placeholder) label = el.placeholder;
        if (!label && el.name) label = el.name;
        label = (label || '').trim().replace(/\\s+/g, ' ');
        if (label && !out.includes(label)) out.push(label);
      }
      return out.slice(0, 40);
    }
    """
    try:
        return page.evaluate(script) or []
    except Exception:
        return []
