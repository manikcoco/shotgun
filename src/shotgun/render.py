"""Render a tailored resume to an ATS-safe PDF.

HTML -> PDF through Chromium's print engine, which we already have because
Playwright drives the fillers. That avoids the usual WeasyPrint/LaTeX system
dependency mess and produces a real text layer, which is what ATS parsers need.

ATS-safety rules baked into the template: single column, no tables for layout,
no icons or images, standard fonts, real headings, no text in headers/footers.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from .config import PRIVATE_DIR
from .models import TailoredResume
from .profile import Profile

log = logging.getLogger(__name__)

TEMPLATE_DIR = Path(__file__).parent / "templates"
OUTPUT_DIR = PRIVATE_DIR / "generated"


def _env() -> Environment:
    return Environment(
        loader=FileSystemLoader(TEMPLATE_DIR),
        autoescape=select_autoescape(["html"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60]


def build_html(profile: Profile, tailored: TailoredResume) -> str:
    """Merge the profile's factual record with the JD-specific rewrite."""
    omitted = {c.strip().lower() for c in tailored.omitted}

    roles = []
    for role in profile.roles:
        if role.company.strip().lower() in omitted:
            continue
        # Prefer tailored bullets; fall back to the profile's own if the rewrite
        # skipped this role. The fallback is logged rather than silent — it
        # means the resume is going out untailored for that role.
        bullets = tailored.bullets_for(role.company)
        if bullets is None:
            log.warning(
                "no tailored bullets for %r — falling back to the profile's own",
                role.company,
            )
            bullets = role.bullets
        roles.append({
            "company": role.company,
            "title": role.title,
            "location": role.location,
            "dates": f"{role.start} – {role.end or 'Present'}",
            "bullets": bullets,
        })

    return _env().get_template("resume.html").render(
        contact=profile.contact,
        headline=tailored.headline,
        summary=tailored.summary,
        skills=tailored.highlighted_skills,
        roles=roles,
        certifications=profile.certifications,
        education=profile.education,
    )


def html_to_pdf(html: str, out_path: Path) -> Path:
    """Print HTML to PDF with headless Chromium."""
    from playwright.sync_api import sync_playwright

    from .browser import launch

    out_path.parent.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        browser = launch(p, headless=True)
        try:
            page = browser.new_page()
            page.set_content(html, wait_until="load")
            page.pdf(
                path=str(out_path),
                format="A4",
                print_background=False,
                margin={"top": "14mm", "bottom": "14mm", "left": "14mm", "right": "14mm"},
            )
        finally:
            browser.close()

    return out_path


def render_resume(
    profile: Profile,
    tailored: TailoredResume,
    company: str,
    title: str,
) -> tuple[Path, Path]:
    """Write both the HTML and the PDF. Returns (pdf_path, html_path)."""
    stem = f"{slug(profile.contact.name)}-{slug(company)}-{slug(title)}"
    html = build_html(profile, tailored)

    html_path = OUTPUT_DIR / f"{stem}.html"
    html_path.parent.mkdir(parents=True, exist_ok=True)
    html_path.write_text(html)

    pdf_path = html_to_pdf(html, OUTPUT_DIR / f"{stem}.pdf")
    log.info("rendered %s", pdf_path)
    return pdf_path, html_path


def render_cover_letter(body: str, name: str, company: str, title: str) -> Path:
    stem = f"{slug(name)}-{slug(company)}-{slug(title)}-cover"
    path = OUTPUT_DIR / f"{stem}.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    return path
