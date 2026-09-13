"""Board discovery via JobSpy (LinkedIn, Indeed, Glassdoor, Google).

Broad reach, but noisier than the ATS APIs: descriptions are sometimes missing,
locations are free text, and the apply URL often points at the board rather
than the employer's ATS. Treat this as a lead generator — `shotgun run`
resolves the real apply URL in the browser before filling anything.
"""

from __future__ import annotations

import collections
import logging

from ..ats import detect
from ..geo import detect_country, is_remote
from ..models import Job

log = logging.getLogger(__name__)

# Defaults only — `sources.jobspy.search_terms` and `.region_queries` in
# preferences.yaml override these, because which terms and places to sweep is
# the single biggest lever on how much of the market this sees and it should
# not require a code edit.
#
# One search per (term, location, board). Every one is a scrape and LinkedIn
# rate-limits aggressively, so the total is terms x locations x boards and
# grows fast: keep terms specific rather than adding near-duplicates.
SEARCH_TERMS = [
    "staff security engineer",
    "lead security engineer",
    "principal security engineer",
    "security engineering manager",
    "head of security",
]

# JobSpy wants a human-readable location string per search.
REGION_QUERIES = {
    "india": ["India", "Bengaluru, India", "Remote, India"],
    "europe": ["Germany", "Netherlands", "Remote, Europe"],
    "uk": ["London, United Kingdom", "Remote, United Kingdom"],
    "australia": ["Sydney, Australia", "Remote, Australia"],
    "new_zealand": ["Auckland, New Zealand"],
}


def _terms(jobspy_config: dict) -> list[str]:
    configured = jobspy_config.get("search_terms") or []
    terms = [str(t).strip() for t in configured if str(t).strip()]
    return terms or SEARCH_TERMS


def _queries(jobspy_config: dict, regions: list[str]) -> list[str]:
    """The location strings to sweep, deduplicated and order-stable.

    A region with no entry contributes nothing rather than raising, so adding
    a region to `locations.regions` without teaching this module a query for
    it is a quiet no-op rather than a crash — but it is worth knowing that is
    what happened, so it is logged.
    """
    table = {**REGION_QUERIES, **(jobspy_config.get("region_queries") or {})}
    out: list[str] = []
    for region in regions:
        found = table.get(region.strip().lower()) or []
        if not found:
            log.warning("jobspy: no search locations for region %r — add them "
                        "under sources.jobspy.region_queries", region)
        out += [str(q).strip() for q in found if str(q).strip()]
    return list(dict.fromkeys(out))


def discover(jobspy_config: dict, regions: list[str]) -> list[Job]:
    if not jobspy_config.get("enabled", True):
        return []

    try:
        from jobspy import scrape_jobs
    except ImportError:
        log.error("python-jobspy not installed — run `uv sync`")
        return []

    boards = jobspy_config.get("boards", ["linkedin", "indeed"])
    per_search = int(jobspy_config.get("results_per_search", 50))
    hours_old = int(jobspy_config.get("hours_old", 168))
    # Off by default, and the economics are the reason. LinkedIn omits
    # descriptions from search results, so this option refetches each posting
    # individually: measured at ~12 minutes for one term in one location, or
    # roughly 11 hours for the full 5-term, 11-location sweep, to fetch 2,750
    # descriptions of which the title filter discards over 99%.
    #
    # Sweeping on titles first and hydrating only the survivors is the same
    # information for a fraction of the requests. Turn this on for a narrow
    # run (few terms, one region) where you want descriptions immediately.
    fetch_descriptions = bool(jobspy_config.get("fetch_descriptions", False))

    terms = _terms(jobspy_config)
    queries = _queries(jobspy_config, regions)
    log.info("jobspy: %d terms x %d locations x %d boards = %d searches",
             len(terms), len(queries), len(boards),
             len(terms) * len(queries) * len(boards))

    jobs: list[Job] = []
    failures: collections.Counter[str] = collections.Counter()

    for term in terms:
        for location in queries:
            # One board per call, deliberately. Passing the whole list means a
            # single site's failure raises out of scrape_jobs and loses the
            # others for that query too: Glassdoor answers non-US locations
            # with "location not parsed" and a connection reset, which was
            # taking LinkedIn, Indeed and Google down with it on two thirds of
            # queries. Isolated, a broken board costs only its own results.
            for board in boards:
                try:
                    frame = scrape_jobs(
                        site_name=[board],
                        search_term=term,
                        location=location,
                        results_wanted=per_search,
                        hours_old=hours_old,
                        description_format="markdown",
                        linkedin_fetch_description=(
                            fetch_descriptions and board == "linkedin"
                        ),
                    )
                except Exception as exc:  # JobSpy raises a wide variety of errors
                    failures[board] += 1
                    log.debug("jobspy %s %r @ %r failed: %s", board, term, location, exc)
                    continue

                if frame is None or frame.empty:
                    continue

                log.info("jobspy %s %r @ %r -> %d rows", board, term, location, len(frame))
                jobs.extend(_rows_to_jobs(frame))

    for board, count in failures.most_common():
        log.warning("jobspy %s failed %d of %d queries",
                    board, count, len(terms) * len(queries))

    return jobs


def _rows_to_jobs(frame) -> list[Job]:
    out: list[Job] = []
    for row in frame.to_dict("records"):
        title = _clean(row.get("title"))
        company = _clean(row.get("company"))
        url = _clean(row.get("job_url"))
        if not (title and company and url):
            continue

        location = _clean(row.get("location"))
        description = _clean(row.get("description"))
        apply_url = _clean(row.get("job_url_direct")) or url

        out.append(Job(
            source=f"jobspy:{row.get('site', 'unknown')}",
            ats=str(detect(apply_url)),
            company=company,
            title=title,
            url=url,
            apply_url=apply_url,
            location=location,
            country=detect_country(location),
            remote=bool(row.get("is_remote")) or is_remote(location),
            description=description,
            salary_min=_num(row.get("min_amount")),
            salary_max=_num(row.get("max_amount")),
            salary_currency=_clean(row.get("currency")),
            posted_at=_clean(row.get("date_posted")),
            source_id=_clean(row.get("id")),
        ))
    return out


def _clean(value) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text if text and text.lower() not in ("nan", "none") else None


def _num(value) -> float | None:
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    return None if num != num else num  # drop NaN
