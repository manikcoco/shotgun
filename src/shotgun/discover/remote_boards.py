"""Remote-first job boards.

Verified live against each API before this was written:

- Remotive   `GET https://remotive.com/api/remote-jobs` — clean JSON, full
             descriptions, a `job_type` field (full_time / contract) and a
             `candidate_required_location` field naming the regions the
             employer will actually hire into. The best of the three.
- RemoteOK   `GET https://remoteok.com/api` — JSON array whose **first element
             is a legal-notice object, not a job**. Skipped explicitly below.
- WeWorkRemotely  RSS per category. Titles and links only; descriptions are
             partial, so scoring leans on the title.

remote.com is deliberately handled separately in `remotecom.py`: it is a React
Server Components app with no public JSON API, so it needs a rendered browser.
"""

from __future__ import annotations

import datetime
import logging
import re
import time
from xml.etree import ElementTree

import httpx

from ..ats import detect
from ..geo import detect_countries, is_remote
from ..models import Job

log = logging.getLogger(__name__)

TIMEOUT = httpx.Timeout(25.0)
UA = {"User-Agent": "shotgun/0.1 (personal job search tool)"}

# Remotive supports a free-text `search`; run one query per term rather than
# pulling the whole board.
SEARCH_TERMS = [
    "security engineer",
    "staff security",
    "lead security",
    "security manager",
    "application security",
    "cloud security",
]

WWR_FEEDS = [
    "https://weworkremotely.com/categories/remote-devops-sysadmin-jobs.rss",
    "https://weworkremotely.com/categories/remote-programming-jobs.rss",
]


def _strip_html(html: str | None) -> str | None:
    if not html:
        return None
    import html as html_mod

    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.S | re.I)
    text = re.sub(r"<br\s*/?>|</p>|</div>|</li>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html_mod.unescape(text)
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def fetch_remotive(client: httpx.Client, terms: list[str] | None = None) -> list[Job]:
    jobs: list[Job] = []
    seen: set[str] = set()

    for term in terms or SEARCH_TERMS:
        try:
            resp = client.get(
                "https://remotive.com/api/remote-jobs",
                params={"search": term, "limit": 100},
            )
            resp.raise_for_status()
            payload = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("remotive %r failed: %s", term, exc)
            continue

        for item in payload.get("jobs", []):
            url = item.get("url") or ""
            if not url or url in seen:
                continue
            seen.add(url)

            # candidate_required_location is the useful geo field here —
            # "LATAM, Europe, USA, Canada, APAC" tells us where they'll hire.
            location = item.get("candidate_required_location") or "Remote"
            countries = detect_countries(location)
            description = _strip_html(item.get("description"))

            # Remotive's job_type maps straight onto our employment type, and
            # contract roles are exactly what makes a US posting viable.
            job_type = (item.get("job_type") or "").lower()
            if "contract" in job_type or "freelance" in job_type:
                description = (
                    f"[Remotive job_type: {job_type}] contract role."
                    f"\n\n{description or ''}"
                )

            jobs.append(Job(
                source="remotive",
                ats=str(detect(url)),
                company=item.get("company_name") or "unknown",
                title=item.get("title") or "",
                url=url,
                apply_url=url,
                location=location,
                country=countries[0] if countries else None,
                remote=True,
                description=description,
                posted_at=item.get("publication_date"),
                source_id=str(item.get("id")),
            ))

    log.info("remotive -> %d postings", len(jobs))
    return jobs


def fetch_remoteok(client: httpx.Client) -> list[Job]:
    try:
        resp = client.get("https://remoteok.com/api")
        resp.raise_for_status()
        payload = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("remoteok failed: %s", exc)
        return []

    jobs: list[Job] = []
    for item in payload:
        # First element is {"legal": ..., "last_updated": ...}, not a job.
        if not isinstance(item, dict) or "position" not in item:
            continue

        title = item.get("position") or ""
        if not re.search(r"security|appsec|infosec", title, re.IGNORECASE):
            continue

        url = item.get("url") or ""
        location = item.get("location") or "Remote"
        countries = detect_countries(location)

        salary_min = item.get("salary_min") or None
        salary_max = item.get("salary_max") or None

        jobs.append(Job(
            source="remoteok",
            ats=str(detect(item.get("apply_url") or url)),
            company=item.get("company") or "unknown",
            title=title,
            url=url,
            apply_url=item.get("apply_url") or url,
            location=location,
            country=countries[0] if countries else None,
            remote=True,
            description=_strip_html(item.get("description")),
            salary_min=float(salary_min) if salary_min else None,
            salary_max=float(salary_max) if salary_max else None,
            salary_currency="USD" if salary_max else None,
            posted_at=item.get("date"),
            source_id=str(item.get("id")),
        ))

    log.info("remoteok -> %d security postings", len(jobs))
    return jobs


def fetch_weworkremotely(client: httpx.Client) -> list[Job]:
    jobs: list[Job] = []

    for feed in WWR_FEEDS:
        try:
            resp = client.get(feed)
            resp.raise_for_status()
            root = ElementTree.fromstring(resp.content)
        except (httpx.HTTPError, ElementTree.ParseError) as exc:
            log.warning("wwr %s failed: %s", feed, exc)
            continue

        for item in root.iterfind(".//item"):
            raw_title = (item.findtext("title") or "").strip()
            if not re.search(r"security|appsec|infosec", raw_title, re.IGNORECASE):
                continue

            # WWR titles are "Company: Job Title".
            company, _, title = raw_title.partition(":")
            if not title:
                company, title = "unknown", raw_title

            link = (item.findtext("link") or "").strip()
            region = (item.findtext("region") or "").strip()
            location = region or "Remote"

            jobs.append(Job(
                source="weworkremotely",
                ats=str(detect(link)),
                company=company.strip(),
                title=title.strip(),
                url=link,
                apply_url=link,
                location=location,
                country=(detect_countries(location) or [None])[0],
                remote=is_remote(location) or True,
                description=_strip_html(item.findtext("description")),
                posted_at=(item.findtext("pubDate") or "").strip() or None,
                source_id=link,
            ))

    log.info("weworkremotely -> %d security postings", len(jobs))
    return jobs


def fetch_arbeitnow(client: httpx.Client, max_pages: int = 25) -> list[Job]:
    """`GET https://www.arbeitnow.com/api/job-board-api` — free, no key.

    The only aggregator here that is Germany- and EU-weighted rather than
    US-remote, which is the gap the other three leave. Full descriptions
    inline, a real `remote` boolean, and `location` as free text.

    Paginated with a `links.next` URL and no search parameter, so this walks
    pages and filters client-side. `max_pages` is a stop so a board that
    grows does not turn into an unbounded crawl; at 100 postings a page the
    default covers 2,500.
    """
    jobs: list[Job] = []
    url = "https://www.arbeitnow.com/api/job-board-api"
    seen: set[str] = set()

    for page in range(max_pages):
        # Per-page, because a paginated crawl must keep what it already has.
        # Raising out of here discarded every page fetched so far: the board
        # answered five pages and then a sixth failed, and the run recorded
        # zero arbeitnow postings rather than five pages' worth.
        try:
            resp = client.get(url)
            resp.raise_for_status()
            payload = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("arbeitnow page %d failed, keeping %d postings: %s",
                        page + 1, len(jobs), exc)
            break

        items = payload.get("data") or []
        if not items:
            break

        for item in items:
            title = (item.get("title") or "").strip()
            slug = item.get("slug")
            if not title or not slug or slug in seen:
                continue
            seen.add(slug)

            description = _strip_html(item.get("description"))

            # `job_types` is structured where the prose is not, and
            # visa.analyse only ever reads the title and the description — so
            # the signal has to reach the text to count for anything.
            #
            # It also has to reach it in a form the patterns recognise. A bare
            # "contract" is deliberately not a contract signal, because the
            # word turns up constantly in ordinary prose, so echoing the tag
            # verbatim reads as nothing at all. Phrased the way Remotive's
            # adapter already does it, which the patterns do match.
            types = [str(t).lower() for t in (item.get("job_types") or [])]
            if any("contract" in t or "freelance" in t for t in types):
                description = (
                    f"[arbeitnow job_types: {', '.join(types)}] contract role."
                    f"\n\n{description or ''}"
                )
            tags = [str(t) for t in (item.get("tags") or []) if str(t).strip()]
            if tags:
                description = f"{description or ''}\n\n[arbeitnow tags: {', '.join(tags)}]".strip()

            location = (item.get("location") or "").strip() or None
            remote = bool(item.get("remote"))
            if remote:
                location = f"{location}, Remote" if location else "Remote"

            created = item.get("created_at")
            posted = None
            if isinstance(created, int):
                posted = datetime.datetime.fromtimestamp(
                    created, datetime.UTC).isoformat()

            url = (item.get("url") or "").strip()
            countries = detect_countries(location)
            jobs.append(Job(
                source="arbeitnow",
                ats=str(detect(url)),
                company=(item.get("company_name") or "unknown").strip(),
                title=title,
                url=url,
                apply_url=url,
                location=location,
                country=countries[0] if countries else None,
                remote=remote or is_remote(location),
                description=description,
                posted_at=posted,
                source_id=str(slug),
            ))

        nxt = (payload.get("links") or {}).get("next")
        if not nxt or nxt == url:
            break
        url = nxt
        # Unpaced, the crawl gets a 429 at page 13. A short wait between
        # pages is the difference between 12 pages and the whole board, and
        # costs seconds on a source that needs no key and no token list.
        time.sleep(0.5)

    log.info("arbeitnow -> %d postings", len(jobs))
    return jobs


FETCHERS = {
    "remotive": fetch_remotive,
    "remoteok": fetch_remoteok,
    "weworkremotely": fetch_weworkremotely,
    "arbeitnow": fetch_arbeitnow,
}


def discover(config: dict) -> list[Job]:
    if not config.get("enabled", True):
        return []

    wanted = config.get("boards", list(FETCHERS))
    jobs: list[Job] = []

    with httpx.Client(timeout=TIMEOUT, headers=UA, follow_redirects=True) as client:
        for name in wanted:
            fetcher = FETCHERS.get(name)
            if not fetcher:
                log.warning("unknown remote board %r", name)
                continue
            try:
                jobs.extend(fetcher(client))
            except Exception as exc:  # a board being down must not kill the run
                log.warning("%s failed: %s", name, exc)

    return jobs
