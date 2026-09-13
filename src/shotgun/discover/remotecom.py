"""remote.com discovery.

remote.com has no public JSON API. It is a Next.js App Router site whose
listing data arrives in a React Server Components flight payload — there is no
`__NEXT_DATA__` blob to parse and no `/api/` route referenced in the HTML
(both checked). So this adapter renders the page in Chromium and reads the
job cards out of the DOM.

That makes it the most fragile source in shotgun: a markup change on their
side breaks it, and it costs a browser launch per run. It is therefore
disabled by default in preferences.yaml. The selector list below is tried in
order, so when the markup shifts you add a selector rather than rewrite this.
"""

from __future__ import annotations

import logging
import re

from ..ats import detect
from ..geo import detect_countries
from ..models import Job

log = logging.getLogger(__name__)

LISTING_URL = "https://remote.com/jobs/all"

# Tried in order until one yields cards. Broad-to-specific.
CARD_SELECTORS = [
    "a[href^='/jobs/']:has(h3)",
    "a[href^='/jobs/'][class*='card']",
    "[data-testid*='job-card']",
    "article a[href^='/jobs/']",
]

SEARCH_TERMS = ["security engineer", "staff security", "security manager"]


def _extract_cards(page, selector: str) -> list[dict]:
    """Pull title / company / location / href out of whatever matched."""
    script = """
    (sel) => {
      const out = [];
      for (const el of document.querySelectorAll(sel)) {
        const href = el.getAttribute('href') || '';
        if (!/^\\/jobs\\/\\d/.test(href) && !/^\\/jobs\\/[a-z0-9-]+$/i.test(href)) continue;
        const text = (el.innerText || '').split('\\n').map(s => s.trim()).filter(Boolean);
        if (!text.length) continue;
        out.push({ href, lines: text.slice(0, 6) });
      }
      return out;
    }
    """
    try:
        return page.evaluate(script, selector) or []
    except Exception:
        return []


def discover(config: dict) -> list[Job]:
    if not config.get("enabled", False):
        return []

    from playwright.sync_api import sync_playwright

    from ..browser import launch

    terms = config.get("search_terms") or SEARCH_TERMS
    jobs: list[Job] = []
    seen: set[str] = set()

    with sync_playwright() as p:
        browser = launch(p, headless=True)
        try:
            page = browser.new_page(
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/131.0.0.0 Safari/537.36"
                )
            )

            for term in terms:
                url = f"{LISTING_URL}?search={term.replace(' ', '+')}"
                try:
                    page.goto(url, wait_until="networkidle", timeout=45_000)
                    page.wait_for_timeout(2000)
                except Exception as exc:
                    log.warning("remote.com %r failed to load: %s", term, exc)
                    continue

                cards: list[dict] = []
                for selector in CARD_SELECTORS:
                    cards = _extract_cards(page, selector)
                    if cards:
                        log.debug("remote.com matched %d cards via %s", len(cards), selector)
                        break

                if not cards:
                    log.warning(
                        "remote.com returned no cards for %r — their markup has "
                        "probably changed; add a selector to CARD_SELECTORS", term
                    )
                    continue

                for card in cards:
                    href = card["href"]
                    if href in seen:
                        continue
                    seen.add(href)

                    lines = card["lines"]
                    title = lines[0]
                    if not re.search(r"security|appsec|infosec", title, re.IGNORECASE):
                        continue

                    company = lines[1] if len(lines) > 1 else "unknown"
                    location = lines[2] if len(lines) > 2 else "Remote"
                    full_url = f"https://remote.com{href}"

                    jobs.append(Job(
                        source="remote.com",
                        ats=str(detect(full_url)),
                        company=company,
                        title=title,
                        url=full_url,
                        apply_url=full_url,
                        location=location,
                        country=(detect_countries(location) or [None])[0],
                        remote=True,
                        # Listing cards carry no description. `shotgun rank`
                        # will score on the title; the detail page is fetched
                        # at prepare time.
                        description=None,
                        source_id=href,
                    ))
        finally:
            browser.close()

    log.info("remote.com -> %d security postings", len(jobs))
    return jobs
