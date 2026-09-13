"""Chromium launch helpers.

`playwright install chromium` fails behind a TLS-intercepting proxy, which
breaks the driver's download from the Playwright CDN. Chrome is
installed locally though, and Playwright can drive it via `channel="chrome"`,
so every launch here tries the bundled Chromium first and falls back to system
Chrome rather than dying.

Set SHOTGUN_BROWSER_CHANNEL=chrome to skip straight to system Chrome.
"""

from __future__ import annotations

import logging
import os

log = logging.getLogger(__name__)

# Reduces the most obvious automation tell. Not a bypass — LinkedIn and
# friends still detect scripted behaviour; this just avoids failing on the
# trivial navigator.webdriver check.
DEFAULT_ARGS = ["--disable-blink-features=AutomationControlled"]


def _channels() -> list[str | None]:
    forced = os.environ.get("SHOTGUN_BROWSER_CHANNEL")
    if forced:
        return [forced]
    return [None, "chrome"]   # bundled Chromium, then system Chrome


def launch(playwright, *, headless: bool, **kwargs):
    """Launch a browser, trying each available channel in turn."""
    last: Exception | None = None
    for channel in _channels():
        try:
            browser = playwright.chromium.launch(
                headless=headless,
                channel=channel,
                args=kwargs.pop("args", DEFAULT_ARGS),
                **kwargs,
            )
            if channel:
                log.debug("launched via channel=%s", channel)
            return browser
        except Exception as exc:
            last = exc
            log.debug("channel=%s unavailable: %s", channel, exc)

    raise RuntimeError(
        "No usable Chromium. `playwright install chromium` is blocked by the "
        "TLS proxy in the way and system Chrome could not be launched "
        "either. Install Chrome, or set SHOTGUN_BROWSER_CHANNEL to a channel "
        f"that works. Last error: {last}"
    ) from last


def launch_persistent(playwright, user_data_dir: str, *, headless: bool, **kwargs):
    """Persistent-profile launch, same channel fallback.

    The persistent profile is what holds your logged-in sessions, so shotgun
    never handles a password.
    """
    last: Exception | None = None
    for channel in _channels():
        try:
            context = playwright.chromium.launch_persistent_context(
                user_data_dir=user_data_dir,
                headless=headless,
                channel=channel,
                args=kwargs.pop("args", DEFAULT_ARGS),
                **kwargs,
            )
            if channel:
                log.debug("launched persistent context via channel=%s", channel)
            return context
        except Exception as exc:
            last = exc
            log.debug("channel=%s unavailable: %s", channel, exc)

    raise RuntimeError(
        "No usable Chromium for a persistent profile. See shotgun/browser.py. "
        f"Last error: {last}"
    ) from last
