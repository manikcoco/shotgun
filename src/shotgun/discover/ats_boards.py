"""Pull postings straight from public ATS job-board APIs.

These are the highest-signal source in shotgun: no scraping, no anti-bot, full
job descriptions, and a direct apply URL. The catch is that you have to know
which companies to ask, so maintain the token lists in preferences.yaml.

Greenhouse, Lever, Ashby, Personio, SmartRecruiters and Workable are
implemented here — all six publish documented, unauthenticated job-board
endpoints, and every one was verified against a live response before its
adapter was written. Workday and iCIMS do not; for those, discovery happens
through JobSpy and the apply URL is handled by a browser filler.

Recruitee is deliberately absent. The subdomains exist but
`/{token}/api/offers/` answered 404 for every tenant tried, so there is
nothing here to write an adapter against — a guessed mapping is worse than no
adapter, because it fails silently and looks like a company that isn't hiring.
"""

from __future__ import annotations

import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from xml.etree import ElementTree

import httpx

from ..ats import ATS
from ..geo import detect_country, is_remote
from ..models import Job

log = logging.getLogger(__name__)

TIMEOUT = httpx.Timeout(20.0)
UA = {"User-Agent": "shotgun/0.1 (personal job search tool)"}


def _strip_html(html: str | None) -> str | None:
    if not html:
        return None
    import html as html_mod
    import re

    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.S | re.I)
    text = re.sub(r"<br\s*/?>|</p>|</div>|</li>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html_mod.unescape(text)
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _job(source: str, ats: ATS, company: str, title: str, url: str,
         location: str | None, description: str | None, source_id: str | None) -> Job:
    return Job(
        source=source,
        ats=str(ats),
        company=company,
        title=title,
        url=url,
        apply_url=url,
        location=location,
        country=detect_country(location),
        remote=is_remote(location, description[:500] if description else None),
        description=description,
        source_id=source_id,
    )


def fetch_greenhouse(token: str, client: httpx.Client) -> list[Job]:
    """https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true"""
    url = f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs"
    resp = client.get(url, params={"content": "true"})
    resp.raise_for_status()
    payload = resp.json()
    company = payload.get("name") or token

    jobs: list[Job] = []
    for item in payload.get("jobs", []):
        jobs.append(_job(
            source=f"greenhouse:{token}",
            ats=ATS.GREENHOUSE,
            company=company,
            title=item.get("title", ""),
            url=item.get("absolute_url", ""),
            location=(item.get("location") or {}).get("name"),
            description=_strip_html(item.get("content")),
            source_id=str(item.get("id")),
        ))
    return jobs


def fetch_lever(token: str, client: httpx.Client) -> list[Job]:
    """https://api.lever.co/v0/postings/{token}?mode=json"""
    url = f"https://api.lever.co/v0/postings/{token}"
    resp = client.get(url, params={"mode": "json"})
    resp.raise_for_status()

    jobs: list[Job] = []
    for item in resp.json():
        categories = item.get("categories") or {}
        jobs.append(_job(
            source=f"lever:{token}",
            ats=ATS.LEVER,
            company=token,
            title=item.get("text", ""),
            url=item.get("hostedUrl", ""),
            location=categories.get("location"),
            description=_strip_html(item.get("descriptionPlain") or item.get("description")),
            source_id=item.get("id"),
        ))
    return jobs


# Annualisation factors for Ashby's `interval` field. "NONE" is equity, which
# carries no interval and is skipped.
_INTERVAL_TO_YEAR = {
    "1 YEAR": 1, "1 MONTH": 12, "2 WEEKS": 26, "1 WEEK": 52,
    "1 DAY": 260, "1 HOUR": 2080,
}


def _ashby_salary(compensation: dict | None) -> tuple[float, float, str] | None:
    """Annual (min, max, currency) from an Ashby compensation object.

    The board API returns structured components, not just the "$224K – $263K"
    display string, so this reads `compensationType == "Salary"` and ignores
    equity, commission and bonus — otherwise an equity line with no currency
    would land in salary_min.

    Postings often carry several geographic tiers. The highest-paying currency
    wins, and within it the widest range, because the comp filter rejects on
    `salary_max < floor` — taking a lower tier would reject a role whose top
    tier clears the floor.
    """
    tiers = (compensation or {}).get("compensationTiers") or []

    by_currency: dict[str, list[tuple[float, float]]] = {}
    for tier in tiers:
        for component in tier.get("components") or []:
            if component.get("compensationType") != "Salary":
                continue
            currency = component.get("currencyCode")
            factor = _INTERVAL_TO_YEAR.get(component.get("interval") or "")
            if not currency or not factor:
                continue
            low, high = component.get("minValue"), component.get("maxValue")
            values = [float(v) * factor for v in (low, high) if isinstance(v, (int, float))]
            if values:
                by_currency.setdefault(currency, []).append((min(values), max(values)))

    if not by_currency:
        return None

    currency = max(by_currency, key=lambda c: max(high for _, high in by_currency[c]))
    ranges = by_currency[currency]
    return min(low for low, _ in ranges), max(high for _, high in ranges), currency


def fetch_ashby(token: str, client: httpx.Client) -> list[Job]:
    """https://api.ashbyhq.com/posting-api/job-board/{token}"""
    url = f"https://api.ashbyhq.com/posting-api/job-board/{token}"
    resp = client.get(url, params={"includeCompensation": "true"})
    resp.raise_for_status()
    payload = resp.json()

    jobs: list[Job] = []
    for item in payload.get("jobs", []):
        job = _job(
            source=f"ashby:{token}",
            ats=ATS.ASHBY,
            company=token,
            title=item.get("title", ""),
            url=item.get("jobUrl", ""),
            location=item.get("location"),
            description=_strip_html(item.get("descriptionHtml") or item.get("descriptionPlain")),
            source_id=item.get("id"),
        )
        # `includeCompensation=true` was already being requested and the answer
        # thrown away, so every Ashby posting looked like it published no
        # salary and the comp floor never fired.
        salary = _ashby_salary(item.get("compensation"))
        if salary:
            job.salary_min, job.salary_max, job.salary_currency = salary
        jobs.append(job)
    return jobs


def fetch_personio(token: str, client: httpx.Client) -> list[Job]:
    """https://{token}.jobs.personio.de/xml — the public job-board feed.

    XML, not JSON: root is `<workzag-jobs>` with one `<position>` per posting.
    Verified live against personio, alasco, prewave, holidu and others.

    Three things worth knowing:
      * Tenants sit on either the .de or the .com domain, so both are tried.
      * The description arrives as several `<jobDescription>` sections whose
        `<value>` is HTML; they are concatenated and stripped. A fair number of
        tenants publish an empty `<jobDescriptions>`, in which case scoring
        falls back to the title, same as the RSS-only boards.
      * `employmentType` is a real structured field, so unlike most sources we
        don't have to infer contract-vs-permanent from prose. It is surfaced
        into the description text where visa.analyse() will read it.
    """
    jobs: list[Job] = []

    for tld in ("de", "com"):
        url = f"https://{token}.jobs.personio.{tld}/xml"
        try:
            resp = client.get(url)
            resp.raise_for_status()
            root = ElementTree.fromstring(resp.content)
        except (httpx.HTTPError, ElementTree.ParseError):
            continue

        if root.tag != "workzag-jobs":
            continue

        for position in root.iter("position"):
            job_id = (position.findtext("id") or "").strip()
            title = (position.findtext("name") or "").strip()
            if not (job_id and title):
                continue

            # Primary office plus any additional ones — a Munich role that also
            # hires into Berlin should surface both to the location filter.
            offices = [(position.findtext("office") or "").strip()]
            extra = position.find("additionalOffices")
            if extra is not None:
                offices += [(o.text or "").strip() for o in extra.iter("office")]
            location = ", ".join(dict.fromkeys(o for o in offices if o)) or None

            sections = []
            for section in position.iter("jobDescription"):
                heading = (section.findtext("name") or "").strip()
                body = _strip_html(section.findtext("value"))
                if body:
                    sections.append(f"{heading}\n{body}" if heading else body)
            description = "\n\n".join(sections) or None

            employment = (position.findtext("employmentType") or "").strip()
            if employment and employment != "permanent":
                marker = f"[Personio employmentType: {employment}]"
                description = f"{marker}\n\n{description or ''}".strip()

            job_url = f"https://{token}.jobs.personio.{tld}/job/{job_id}"
            job = _job(
                source=f"personio:{token}",
                ats=ATS.PERSONIO,
                company=(position.findtext("subcompany") or token).strip() or token,
                title=title,
                url=job_url,
                location=location,
                description=description,
                source_id=job_id,
            )
            job.posted_at = (position.findtext("createdAt") or "").strip() or None
            jobs.append(job)

        return jobs

    log.warning("personio:%s — no feed on either .de or .com; check the token", token)
    return []


def _place(loc: dict) -> str:
    """city, region, COUNTRY from a SmartRecruiters/Workable location object.

    The country arrives as a lowercase code — "Hanoi, vn" — which reads as a
    typo in a listing and is not what the rest of this codebase writes. Upper
    only the two-letter code; a spelled-out country is left alone.
    """
    parts = [str(loc.get(k)).strip() for k in ("city", "region", "country")
             if loc.get(k)]
    if parts and len(parts[-1]) == 2:
        parts[-1] = parts[-1].upper()
    return ", ".join(parts)


def _shape(board: str, token: str, payload, item) -> None:
    """Log the keys a board actually returned.

    Temporary scaffolding while an adapter is being written against a live
    response rather than a remembered one. Left at DEBUG so it costs nothing
    once the mapping is settled.
    """
    log.debug("%s:%s payload keys=%s", board, token,
              list(payload) if isinstance(payload, dict) else type(payload).__name__)
    if isinstance(item, dict):
        log.debug("%s:%s item keys=%s", board, token, list(item))


def fetch_smartrecruiters(token: str, client: httpx.Client) -> list[Job]:
    """https://api.smartrecruiters.com/v1/companies/{token}/postings

    Public, unauthenticated, and paginated 100 at a time. The catch is that
    the list endpoint carries no description — that needs one extra GET per
    posting — so this fetches the list and hydrates only what the title rules
    would keep. The caller does not know the title rules, so hydration is
    left to a second pass and postings arrive description-less.
    """
    jobs: list[Job] = []
    offset, limit = 0, 100
    company = token

    while True:
        resp = client.get(
            f"https://api.smartrecruiters.com/v1/companies/{token}/postings",
            params={"limit": limit, "offset": offset},
        )
        resp.raise_for_status()
        payload = resp.json()
        items = payload.get("content") or []
        if offset == 0:
            _shape("smartrecruiters", token, payload, items[0] if items else None)

        for item in items:
            company = (item.get("company") or {}).get("name") or token
            loc = item.get("location") or {}
            where = _place(loc) or None
            if loc.get("remote"):
                where = f"{where}, Remote" if where else "Remote"

            job = _job(
                source=f"smartrecruiters:{token}",
                ats=ATS.SMARTRECRUITERS,
                company=company,
                title=item.get("name", ""),
                url=(f"https://jobs.smartrecruiters.com/{token}/"
                     f"{item.get('id')}"),
                location=where,
                description=None,
                source_id=str(item.get("id")),
            )
            job.posted_at = item.get("releasedDate")
            jobs.append(job)

        offset += limit
        if offset >= int(payload.get("totalFound") or 0) or not items:
            break

    return jobs


def fetch_workable(token: str, client: httpx.Client) -> list[Job]:
    """https://apply.workable.com/api/v1/widget/accounts/{token}?details=true

    The widget endpoint is used rather than the v3 `jobs` POST API: it is a
    plain GET, needs no body, and `details=true` returns the description
    inline, which the v3 list does not.
    """
    resp = client.get(
        f"https://apply.workable.com/api/v1/widget/accounts/{token}",
        params={"details": "true"},
    )
    resp.raise_for_status()
    payload = resp.json()
    items = payload.get("jobs") or []
    _shape("workable", token, payload, items[0] if items else None)

    jobs: list[Job] = []
    for item in items:
        loc = item.get("location") or {}
        where = _place(loc) or None
        if item.get("telecommuting") or loc.get("telecommuting"):
            where = f"{where}, Remote" if where else "Remote"

        jobs.append(_job(
            source=f"workable:{token}",
            ats=ATS.WORKABLE,
            company=payload.get("name") or token,
            title=item.get("title", ""),
            url=item.get("url") or item.get("application_url") or "",
            location=where,
            description=_strip_html(item.get("description")),
            source_id=str(item.get("shortcode") or item.get("id") or ""),
        ))
    return jobs


def _smartrecruiters_detail(job: Job, client: httpx.Client) -> str | None:
    """https://api.smartrecruiters.com/v1/companies/{token}/postings/{id}

    The list endpoint carries no description, so this is the only way to read
    one — and the description is where every visa and relocation statement
    lives. One GET per posting, which is why it is only ever called for
    postings something else already decided to keep.
    """
    token = job.source.partition(":")[2]
    resp = client.get(
        f"https://api.smartrecruiters.com/v1/companies/{token}/postings/"
        f"{job.source_id}"
    )
    resp.raise_for_status()
    payload = resp.json()
    _shape("smartrecruiters-detail", token, payload, payload.get("jobAd"))

    ad = payload.get("jobAd") or {}
    sections = ad.get("sections") or {}
    parts = []
    for name in ("companyDescription", "jobDescription", "qualifications",
                 "additionalInformation"):
        section = sections.get(name) or {}
        text = _strip_html(section.get("text"))
        if text:
            title = section.get("title") or name
            parts.append(f"{title}\n{text}")
    return "\n\n".join(parts) or None


# Per-source description fetchers, for sources whose list endpoint omits it.
HYDRATORS = {"smartrecruiters": _smartrecruiters_detail}


def hydrate_descriptions(jobs: list[Job], keep, *, max_workers: int = 8) -> int:
    """Fill in descriptions for the postings `keep` says are worth reading.

    SmartRecruiters publishes 4,492 postings for Bosch alone and no
    descriptions with them, which made every one of those roles unreadable
    for visa and relocation — the signals this tool exists to surface. The
    fix is not to fetch 4,492 descriptions, it is to fetch the 39 that the
    title rules already kept.

    `keep` is a predicate over a Job, passed in because this module has no
    business knowing what the title rules are. A posting that already has a
    description is skipped, and a failure leaves that one posting as it was.
    """
    todo = [
        j for j in jobs
        if not j.description
        and j.source.partition(":")[0] in HYDRATORS
        and keep(j)
    ]
    if not todo:
        return 0

    log.info("hydrating %d of %d postings that need a description",
             len(todo), len(jobs))

    def one(job: Job) -> bool:
        hydrator = HYDRATORS[job.source.partition(":")[0]]
        try:
            with httpx.Client(timeout=TIMEOUT, headers=UA,
                              follow_redirects=True) as client:
                job.description = hydrator(job, client)
        except Exception as exc:
            log.debug("hydrate %s %s failed: %s", job.source, job.source_id, exc)
            return False
        return bool(job.description)

    with ThreadPoolExecutor(max_workers=min(max_workers, len(todo))) as pool:
        filled = sum(pool.map(one, todo))

    log.info("hydrated %d descriptions", filled)
    return filled


FETCHERS = {
    "greenhouse": fetch_greenhouse,
    "lever": fetch_lever,
    "ashby": fetch_ashby,
    "personio": fetch_personio,
    "smartrecruiters": fetch_smartrecruiters,
    "workable": fetch_workable,
}


@dataclass
class BoardResult:
    """One board's outcome. `error` is set instead of `jobs` on failure."""
    board: str
    token: str
    jobs: list[Job] = field(default_factory=list)
    error: str | None = None

    @property
    def company(self) -> str:
        return self.jobs[0].company if self.jobs else self.token


def _fetch_one(board: str, token: str, *, attempts: int = 3) -> BoardResult:
    """One board, its own client, never raises.

    A client per task rather than one shared across the pool: httpx.Client is
    thread-safe, but a connection pool shared across boards means one slow
    host can hold connections the others are waiting on.

    Transient failures are retried, because at this concurrency they are
    common and expensive. Fetched on its own every board here answers in
    under a second, but running 90 of them at once reliably starved one or
    two into a read timeout — a different one each run. Losing a board that
    way costs its entire posting list, and the run before this reported
    Datadog as unreachable while the one before blamed Grafana. A bad token
    is not retried: an HTTP status is a real answer and asking again cannot
    change it.
    """
    last = ""
    for attempt in range(1, attempts + 1):
        try:
            with httpx.Client(timeout=TIMEOUT, headers=UA,
                              follow_redirects=True) as client:
                return BoardResult(board, token, jobs=FETCHERS[board](token, client))
        except httpx.HTTPStatusError as exc:
            return BoardResult(board, token, error=f"HTTP {exc.response.status_code}")
        except httpx.TransportError as exc:
            last = str(exc) or exc.__class__.__name__
            if attempt < attempts:
                log.debug("%s:%s attempt %d/%d failed (%s) — retrying",
                          board, token, attempt, attempts, last)
                time.sleep(0.4 * attempt)
        except (httpx.HTTPError, ValueError, KeyError, ElementTree.ParseError) as exc:
            return BoardResult(board, token, error=str(exc) or exc.__class__.__name__)

    return BoardResult(board, token, error=f"{last} (after {attempts} attempts)")


_COMPANY_NOISE = re.compile(
    r"\b(inc|llc|ltd|limited|gmbh|bv|nv|ag|sa|se|plc|corp|corporation|co|"
    r"holding|holdings|technologies|technology|labs|laboratories|group|"
    r"solutions|services|consulting|international|global|the)\b",
    re.IGNORECASE,
)


def token_variants(company: str | None) -> list[str]:
    """Plausible board tokens for a company name.

    A company advertising on a remote-job board is remote-friendly by
    definition, which is the profile worth chasing — but the aggregators keep
    every apply URL on their own domain, so the board token is never
    published and the name is all there is to go on.

    Three spellings cover most of what the boards actually use: squashed
    ("duckduckgo"), hyphenated ("duck-duck-go" — DuckDuckGo's real Ashby
    token) and the first word alone, for the many two-word names whose board
    is under the first half. Legal suffixes and punctuation are stripped
    because no board token has ever contained "GmbH".
    """
    if not company:
        return []
    cleaned = _COMPANY_NOISE.sub(" ", company)
    cleaned = re.sub(r"[^A-Za-z0-9 ]+", " ", cleaned).lower().strip()
    words = cleaned.split()
    if not words:
        return []

    out = ["".join(words), "-".join(words)]
    if len(words) > 1:
        out.append(words[0])

    # Case preserved, because board tokens are case-sensitive and lowercasing
    # everything silently excluded every capitalised one. Ubiminds publishes
    # at jobs.lever.co/Ubiminds and answers 404 for "ubiminds", so a role
    # this list should have found was invisible. The already-configured
    # SmartRecruiters tokens — BoschGroup, Visa, IKEA, AveryDennison — could
    # never have been discovered by this function either.
    cased = re.sub(r"[^A-Za-z0-9]+", "", _COMPANY_NOISE.sub(" ", company))
    if cased and cased.lower() != cased:
        out.append(cased)

    return [v for v in dict.fromkeys(out) if 2 < len(v) < 30]


def probe_tokens(
    candidates: list[str],
    boards: tuple[str, ...] = ("greenhouse", "lever", "ashby"),
    *,
    max_workers: int = 24,
) -> list[BoardResult]:
    """Try each candidate against each board; return only the live ones.

    Deliberately not every board in FETCHERS. Personio needs a tenant
    subdomain rather than a slug, and SmartRecruiters answers 200 with an
    empty list for names it does not know — which is indistinguishable from a
    real board with nothing on it, so probing it produces false positives.
    """
    work = [(board, token) for token in candidates for board in boards]
    if not work:
        return []

    with ThreadPoolExecutor(max_workers=min(max_workers, len(work))) as pool:
        results = pool.map(lambda pair: _fetch_one(*pair, attempts=1), work)

    return [r for r in results if not r.error and r.jobs]


def board_tokens(ats_config: dict, only: list[str] | None = None) -> list[tuple[str, str]]:
    """The (board, token) pairs to fetch, optionally narrowed to `only`.

    `only` matches the token, so `--company canonical` does not require
    knowing that Canonical happens to be on Greenhouse.
    """
    wanted = {c.strip().lower() for c in (only or []) if c.strip()}
    return [
        (board, token)
        for board in FETCHERS
        for token in ats_config.get(board, []) or []
        if not wanted or str(token).strip().lower() in wanted
    ]


def fetch_boards(
    ats_config: dict,
    only: list[str] | None = None,
    *,
    max_workers: int = 16,
) -> list[BoardResult]:
    """Fetch boards concurrently. One failing never affects the others.

    Concurrent because these are ~90 independent HTTP calls to different
    hosts, and sequentially that is minutes of almost pure waiting.
    """
    pairs = board_tokens(ats_config, only)
    if not pairs:
        return []

    with ThreadPoolExecutor(max_workers=min(max_workers, len(pairs))) as pool:
        return list(pool.map(lambda pair: _fetch_one(*pair), pairs))


def discover(ats_config: dict) -> list[Job]:
    """Fetch every configured board. A single board failing never aborts the run."""
    if not ats_config.get("enabled", True):
        return []

    jobs: list[Job] = []
    for result in fetch_boards(ats_config):
        if result.error:
            log.warning("%s:%s failed: %s — check the token",
                        result.board, result.token, result.error)
            continue
        log.info("%s:%s -> %d postings", result.board, result.token, len(result.jobs))
        jobs.extend(result.jobs)
    return jobs
