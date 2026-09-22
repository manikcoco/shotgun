"""ATS job-board feed parsing.

Personio is the only XML source among the board APIs, and it has three shapes
the JSON ones don't: a `<workzag-jobs>` root, descriptions split across several
`<jobDescription>` sections whose `<value>` is HTML, and multiple offices per
posting. All fixtures below are trimmed from live responses.
"""

from __future__ import annotations

import httpx
import pytest

from shotgun.ats import ATS, detect
from shotgun.discover.ats_boards import (
    FETCHERS,
    fetch_personio,
    fetch_recruitee,
    fetch_teamtailor,
)

FEED = """<?xml version="1.0" encoding="UTF-8"?>
<workzag-jobs>
<position>
    <id>84027</id>
    <subcompany>Alasco GmbH</subcompany>
    <office>M&#252;nchen</office>
    <additionalOffices>
        <office>Berlin</office>
    </additionalOffices>
    <department>Engineering</department>
    <name>Staff Security Engineer</name>
    <jobDescriptions>
        <jobDescription>
            <name>Your tasks</name>
            <value><![CDATA[<ul><li>Own cloud security posture</li></ul>]]></value>
        </jobDescription>
        <jobDescription>
            <name>Your profile</name>
            <value><![CDATA[<ul><li>Kubernetes &amp; Terraform</li></ul>]]></value>
        </jobDescription>
    </jobDescriptions>
    <employmentType>permanent</employmentType>
    <createdAt>2026-08-01T10:00:00+00:00</createdAt>
</position>
<position>
    <id>99999</id>
    <office>Vienna</office>
    <name>Contract Security Consultant</name>
    <jobDescriptions></jobDescriptions>
    <employmentType>temporary</employmentType>
    <createdAt>2026-07-01T10:00:00+00:00</createdAt>
</position>
<position>
    <id></id>
    <office>Nowhere</office>
    <name></name>
</position>
</workzag-jobs>
"""


def client_returning(body: str, *, status: int = 200) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, content=body.encode())

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_parses_positions() -> None:
    with client_returning(FEED) as c:
        jobs = fetch_personio("alasco", c)

    # The third position has no id and no title, so it is dropped.
    assert len(jobs) == 2
    assert [j.title for j in jobs] == [
        "Staff Security Engineer",
        "Contract Security Consultant",
    ]


def test_multiple_offices_are_all_kept() -> None:
    """A Munich role that also hires into Berlin must show both to the
    location filter, not just the primary office."""
    with client_returning(FEED) as c:
        job = fetch_personio("alasco", c)[0]
    assert job.location == "München, Berlin"
    assert job.country == "DE"


def test_description_sections_are_joined_and_stripped() -> None:
    with client_returning(FEED) as c:
        job = fetch_personio("alasco", c)[0]
    assert "Your tasks" in job.description
    assert "Own cloud security posture" in job.description
    assert "Kubernetes & Terraform" in job.description, "HTML entities decoded"
    assert "<ul>" not in job.description, "HTML stripped"


def test_non_permanent_employment_type_is_surfaced_for_visa_analysis() -> None:
    """Personio publishes employmentType as a real field, so contract roles
    don't have to be inferred from prose — but visa.analyse() reads text, so
    it has to reach the description."""
    with client_returning(FEED) as c:
        job = fetch_personio("alasco", c)[1]
    assert "employmentType: temporary" in job.description


def test_permanent_roles_get_no_marker() -> None:
    with client_returning(FEED) as c:
        job = fetch_personio("alasco", c)[0]
    assert "employmentType" not in job.description


def test_apply_url_and_metadata() -> None:
    with client_returning(FEED) as c:
        job = fetch_personio("alasco", c)[0]
    assert job.url == "https://alasco.jobs.personio.de/job/84027"
    assert job.apply_url == job.url
    assert job.source == "personio:alasco"
    assert job.ats == str(ATS.PERSONIO)
    assert job.source_id == "84027"
    assert job.posted_at == "2026-08-01T10:00:00+00:00"


def test_company_falls_back_to_the_token() -> None:
    """`subcompany` is optional; without it the token is the best label."""
    with client_returning(FEED) as c:
        jobs = fetch_personio("alasco", c)
    assert jobs[0].company == "Alasco GmbH"
    assert jobs[1].company == "alasco"


@pytest.mark.parametrize("body", ["", "not xml at all", "<html><body>nope</body></html>"])
def test_a_non_feed_response_yields_nothing(body: str) -> None:
    """Unknown tenants 307 to personio.com, so the body is HTML, not a feed.
    That must return empty rather than raise — one bad token cannot abort a run."""
    with client_returning(body) as c:
        assert fetch_personio("nosuchtenant", c) == []


def test_http_error_yields_nothing() -> None:
    with client_returning(FEED, status=404) as c:
        assert fetch_personio("gone", c) == []


def test_personio_urls_route_to_the_personio_filler() -> None:
    for url in (
        "https://alasco.jobs.personio.de/job/84027",
        "https://personio.jobs.personio.com/job/1834171",
        "https://alasco.jobs.personio.de/job/84027?language=en",
    ):
        assert detect(url) is ATS.PERSONIO


# ------------------------------------------------- Ashby compensation

from shotgun.discover.ats_boards import _ashby_salary  # noqa: E402


def comp(*components: dict) -> dict:
    return {"compensationTiers": [{"components": list(components)}]}


def salary(minv, maxv, currency="EUR", interval="1 YEAR") -> dict:
    return {"compensationType": "Salary", "interval": interval,
            "currencyCode": currency, "minValue": minv, "maxValue": maxv}


def test_reads_an_annual_salary_range() -> None:
    assert _ashby_salary(comp(salary(120000, 160000))) == (120000.0, 160000.0, "EUR")


def test_a_single_figure_becomes_a_degenerate_range() -> None:
    assert _ashby_salary(comp(salary(80000, 80000, "USD"))) == (80000.0, 80000.0, "USD")


def test_equity_and_commission_are_ignored() -> None:
    """An equity component has no currency and no interval; letting it through
    would put a nonsense figure in salary_min."""
    payload = comp(
        {"compensationType": "EquityPercentage", "interval": "NONE",
         "currencyCode": None, "minValue": None, "maxValue": None},
        {"compensationType": "Commission", "interval": "1 YEAR",
         "currencyCode": "USD", "minValue": 20000, "maxValue": 20000},
        salary(150000, 180000, "USD"),
    )
    assert _ashby_salary(payload) == (150000.0, 180000.0, "USD")


def test_monthly_intervals_are_annualised() -> None:
    assert _ashby_salary(comp(salary(10000, 12000, "EUR", "1 MONTH"))) == (
        120000.0, 144000.0, "EUR")


def test_the_highest_paying_currency_wins_across_tiers() -> None:
    """Geographic tiers are common. The comp filter rejects on
    `salary_max < floor`, so picking a lower tier would reject a role whose top
    tier clears it."""
    payload = {"compensationTiers": [
        {"components": [salary(90000, 110000, "EUR")]},
        {"components": [salary(180000, 240000, "USD")]},
    ]}
    assert _ashby_salary(payload) == (180000.0, 240000.0, "USD")


def test_widest_range_within_the_chosen_currency() -> None:
    payload = {"compensationTiers": [
        {"components": [salary(120000, 150000, "EUR")]},
        {"components": [salary(100000, 170000, "EUR")]},
    ]}
    assert _ashby_salary(payload) == (100000.0, 170000.0, "EUR")


@pytest.mark.parametrize("payload", [
    None, {}, {"compensationTiers": []},
    {"compensationTiers": [{"components": []}]},
    comp({"compensationType": "Salary", "interval": "NONE",
          "currencyCode": "EUR", "minValue": 1, "maxValue": 2}),
    comp(salary(None, None)),
])
def test_no_usable_salary_returns_none(payload) -> None:
    assert _ashby_salary(payload) is None


# ------------------------------------- SmartRecruiters, Workable, arbeitnow
#
# Fixtures trimmed from live responses. All three adapters were written
# against a real payload, not a remembered one; Recruitee has no adapter
# because its `/api/offers/` answered 404 for every tenant tried, and a
# guessed mapping fails silently and looks like a company that isn't hiring.

def json_client(pages: list[dict], *, status: int = 200) -> httpx.Client:
    """Returns each payload in turn, so pagination can be exercised."""
    seq = list(pages)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=seq.pop(0) if seq else {})

    return httpx.Client(transport=httpx.MockTransport(handler))


SR_PAGE = {
    "offset": 0, "limit": 100, "totalFound": 2,
    "content": [
        {
            "id": "744000148969740",
            "name": "Cloud Security Engineer (f/m/div.)",
            "company": {"identifier": "BoschGroup", "name": "Bosch Group"},
            "location": {"city": "Lisboa", "country": "pt", "remote": False},
            "releasedDate": "2026-09-01T09:25:25.859Z",
        },
        {
            "id": "744000148969522",
            "name": "Staff Security Engineer",
            "company": {"identifier": "BoschGroup", "name": "Bosch Group"},
            "location": {"city": "Berlin", "region": "BE", "country": "de",
                         "remote": True},
            "releasedDate": "2026-09-02T09:24:33.163Z",
        },
    ],
}


def test_smartrecruiters_maps_a_posting() -> None:
    from shotgun.discover.ats_boards import fetch_smartrecruiters

    jobs = fetch_smartrecruiters("BoschGroup", json_client([SR_PAGE]))

    assert len(jobs) == 2
    first = jobs[0]
    assert first.company == "Bosch Group"        # display name, not the token
    assert first.title == "Cloud Security Engineer (f/m/div.)"
    assert first.location == "Lisboa, PT"        # code upper-cased
    assert first.url == (
        "https://jobs.smartrecruiters.com/BoschGroup/744000148969740"
    )
    assert first.ats == str(ATS.SMARTRECRUITERS)
    assert detect(first.url) is ATS.SMARTRECRUITERS
    assert first.posted_at == "2026-09-01T09:25:25.859Z"


def test_smartrecruiters_marks_a_remote_posting() -> None:
    """`location.remote` is a real boolean here, so it does not have to be
    inferred from prose the way most sources need."""
    from shotgun.discover.ats_boards import fetch_smartrecruiters

    jobs = fetch_smartrecruiters("BoschGroup", json_client([SR_PAGE]))
    assert jobs[1].location == "Berlin, BE, DE, Remote"
    assert jobs[1].remote is True


def test_smartrecruiters_carries_no_description() -> None:
    """The list endpoint has none — it needs a GET per posting — so scoring
    leans on the title. Asserted so it is a known limitation, not a surprise."""
    from shotgun.discover.ats_boards import fetch_smartrecruiters

    assert fetch_smartrecruiters("BoschGroup", json_client([SR_PAGE]))[0].description \
        is None


def test_smartrecruiters_stops_when_totalfound_is_reached() -> None:
    """Bosch publishes 4,492 postings, so the walk has to terminate on the
    count rather than on an empty page."""
    from shotgun.discover.ats_boards import fetch_smartrecruiters

    page = {**SR_PAGE, "totalFound": 2}
    jobs = fetch_smartrecruiters("BoschGroup", json_client([page, page, page]))
    assert len(jobs) == 2      # one page only; a second would double them


def test_smartrecruiters_handles_an_empty_board() -> None:
    """Visa answers 200 with content: [] — a live token with nothing on it,
    which must not look like a failure."""
    from shotgun.discover.ats_boards import fetch_smartrecruiters

    empty = {"offset": 0, "limit": 100, "totalFound": 0, "content": []}
    assert fetch_smartrecruiters("Visa", json_client([empty])) == []


WORKABLE_PAGE = {
    "name": "Typeform",
    "jobs": [{
        "title": "Security Engineer",
        "shortcode": "ABC123",
        "url": "https://apply.workable.com/typeform/j/ABC123/",
        "location": {"city": "Barcelona", "country": "es"},
        "description": "<p>Own our <b>AppSec</b> programme.</p>",
        "telecommuting": True,
    }],
}


def test_workable_maps_a_posting_with_its_description() -> None:
    """The widget endpoint with `details=true` is used precisely because the
    v3 list API omits the description."""
    from shotgun.discover.ats_boards import fetch_workable

    job = fetch_workable("typeform", json_client([WORKABLE_PAGE]))[0]
    assert job.company == "Typeform"
    assert job.title == "Security Engineer"
    assert job.location == "Barcelona, ES, Remote"
    assert job.description == "Own our AppSec programme."
    assert job.ats == str(ATS.WORKABLE)
    assert job.source_id == "ABC123"


def test_place_leaves_a_spelled_out_country_alone() -> None:
    from shotgun.discover.ats_boards import _place

    assert _place({"city": "Berlin", "country": "de"}) == "Berlin, DE"
    assert _place({"city": "Berlin", "country": "Germany"}) == "Berlin, Germany"
    assert _place({}) == ""


ARBEITNOW_P1 = {
    "data": [
        {
            "slug": "head-of-security-berlin-1komma5-123",
            "company_name": "1KOMMA5°",
            "title": "Head of Security (f/m/d)",
            "description": "<p>Own security for a <b>scaling</b> energy platform.</p>",
            "remote": True,
            "url": "https://www.arbeitnow.com/jobs/companies/1komma5/head-of-security",
            "tags": ["Security"],
            "job_types": ["full-time"],
            "location": "Berlin",
            "created_at": 1757500000,
        },
        {
            "slug": "appsec-awin-456",
            "company_name": "Awin",
            "title": "Application Security Engineer (f/m/d)",
            "description": "<p>Six month contract.</p>",
            "remote": False,
            "url": "https://www.arbeitnow.com/jobs/companies/awin/appsec",
            "tags": [],
            "job_types": ["contract"],
            "location": "Berlin",
            "created_at": 1757400000,
        },
    ],
    "links": {"next": "https://www.arbeitnow.com/api/job-board-api?page=2"},
}


def test_arbeitnow_maps_a_posting() -> None:
    from shotgun.discover.remote_boards import fetch_arbeitnow

    jobs = fetch_arbeitnow(json_client([ARBEITNOW_P1, {"data": []}]), max_pages=2)

    assert len(jobs) == 2
    first = jobs[0]
    assert first.company == "1KOMMA5°"
    assert first.location == "Berlin, Remote"
    assert first.remote is True
    assert "scaling energy platform" in first.description
    assert first.posted_at == "2025-09-10T10:26:40+00:00"


def test_arbeitnow_surfaces_job_types_into_the_description() -> None:
    """`visa.analyse` only ever reads the title and the description, so a
    `contract` job_type has to reach the text or the contract bonus — one of
    only three signals that gets an out-of-region role through — never fires."""
    from shotgun.discover.remote_boards import fetch_arbeitnow
    from shotgun.visa import Employment, analyse

    jobs = fetch_arbeitnow(json_client([ARBEITNOW_P1, {"data": []}]), max_pages=2)
    awin = next(j for j in jobs if j.company == "Awin")
    assert "contract" in awin.description
    assert analyse(awin.description, awin.title).employment is Employment.CONTRACT


def test_arbeitnow_keeps_the_pages_it_already_fetched() -> None:
    """The board 429s partway through — at page 13 unpaced, 17 paced. Raising
    out of the walk discarded every page fetched so far and recorded zero."""
    from shotgun.discover.remote_boards import fetch_arbeitnow

    def handler(request: httpx.Request) -> httpx.Response:
        if "page=2" in str(request.url):
            return httpx.Response(429, json={"message": "Too Many Requests"})
        return httpx.Response(200, json=ARBEITNOW_P1)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    jobs = fetch_arbeitnow(client, max_pages=5)

    assert len(jobs) == 2      # page one survived the failure on page two


def test_arbeitnow_deduplicates_by_slug() -> None:
    """Walking pages re-sees postings when the board reorders under you."""
    from shotgun.discover.remote_boards import fetch_arbeitnow

    jobs = fetch_arbeitnow(
        json_client([ARBEITNOW_P1, ARBEITNOW_P1, {"data": []}]), max_pages=3,
    )
    assert len(jobs) == 2


def test_arbeitnow_stops_at_max_pages() -> None:
    """A stop so a growing board cannot turn into an unbounded crawl."""
    from shotgun.discover.remote_boards import fetch_arbeitnow

    def handler(request: httpx.Request) -> httpx.Response:
        # Always a full page with a next link, and always new slugs.
        page = str(request.url).split("page=")[-1]
        return httpx.Response(200, json={
            "data": [{**ARBEITNOW_P1["data"][0], "slug": f"job-{page}"}],
            "links": {"next": f"https://www.arbeitnow.com/api/job-board-api?page={page}x"},
        })

    client = httpx.Client(transport=httpx.MockTransport(handler))
    assert len(fetch_arbeitnow(client, max_pages=3)) == 3


# ------------------------------------------------- recruitee and teamtailor

def _client(payload: dict) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json=payload)))


def test_recruitee_prefers_the_stated_location_over_the_office() -> None:
    """The two disagree more often than not. Time Doctor files
    "Argentina - Remote" under an office whose city is New York."""
    jobs = fetch_recruitee("acme", _client({"offers": [{
        "id": 7, "title": "Security Engineer",
        "careers_url": "https://acme.recruitee.com/o/security-engineer",
        "locations": [{"name": "Argentina - Remote", "country_code": "ar",
                       "city": "New York", "country": "United States"}],
        "description": "<p>text</p>",
    }]}))
    assert jobs[0].location == "Argentina - Remote"
    assert jobs[0].country == "AR"
    assert jobs[0].ats == "recruitee"


def test_recruitee_falls_back_to_the_country_code() -> None:
    """When the stated name carries no country the structured code stands in."""
    jobs = fetch_recruitee("acme", _client({"offers": [{
        "id": 8, "title": "Security Engineer", "careers_url": "https://x",
        "locations": [{"name": "Head office", "country_code": "nl"}],
    }]}))
    assert jobs[0].country == "NL"


def test_teamtailor_json_feed_carries_no_structured_location() -> None:
    """JSON Feed is a blogging format — there are no job fields at all."""
    jobs = fetch_teamtailor("acme", _client({"items": [{
        "id": "x1", "title": "Security Engineer",
        "url": "https://acme.teamtailor.com/jobs/1",
        "date_published": "2026-09-18T14:38:46+02:00",
        "content_html": "<p>Join <i>us</i></p>",
    }]}))
    assert jobs[0].title == "Security Engineer"
    assert jobs[0].country is None
    assert jobs[0].posted_at == "2026-09-18"
    assert "Join us" in jobs[0].description


def test_both_new_boards_are_dispatchable() -> None:
    assert "recruitee" in FETCHERS
    assert "teamtailor" in FETCHERS
