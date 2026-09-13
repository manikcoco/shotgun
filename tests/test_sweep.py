"""Asking the boards directly what security roles they have open.

`discover` + `rank` answers this too, but only after storing thousands of
postings and paying to score the survivors. A sweep is the short question, and
it has to stay cheap: no database writes unless asked, and no model calls ever.
"""

from __future__ import annotations

import httpx
import pytest

from shotgun import db, pipeline
from shotgun.config import Preferences
from shotgun.discover import ats_boards
from shotgun.discover.ats_boards import BoardResult, board_tokens
from shotgun.models import Job

PREFS = Preferences(raw={
    "titles": {
        "include": [r"\b(security|appsec)\b.*\bengineer\b"],
        "exclude": [r"\bintern\b", r"\bproduct manager\b"],
        "any_level": {
            "companies": ["canonical"],
            "include": [r"\b(security|privacy|detection)\b"],
        },
    },
    "sources": {"ats_boards": {
        "greenhouse": ["canonical", "mozilla"],
        "ashby": ["duck-duck-go"],
    }},
})


def job(company: str, title: str, **over) -> Job:
    base = dict(source="ats", company=company, title=title,
                url=f"https://example.com/{title}", location="Remote")
    return Job(**(base | over))


# --------------------------------------------------------- token selection

def test_every_configured_board_by_default() -> None:
    assert set(board_tokens(PREFS.ats_boards)) == {
        ("greenhouse", "canonical"), ("greenhouse", "mozilla"),
        ("ashby", "duck-duck-go"),
    }


def test_narrowing_does_not_require_knowing_the_ats() -> None:
    """`-c canonical` should work without knowing Canonical is on Greenhouse."""
    assert board_tokens(PREFS.ats_boards, ["canonical"]) == [("greenhouse", "canonical")]


def test_narrowing_is_case_insensitive_and_ignores_blanks() -> None:
    assert board_tokens(PREFS.ats_boards, ["CANONICAL", "  ", ""]) \
        == [("greenhouse", "canonical")]


def test_an_unknown_company_selects_nothing() -> None:
    assert board_tokens(PREFS.ats_boards, ["nope"]) == []


# ---------------------------------------------------------------- the sweep

@pytest.fixture
def boards(monkeypatch):
    """Stand in for the network. Each test sets `boards.results`."""
    class Fake:
        results: list[BoardResult] = []

    fake = Fake()
    monkeypatch.setattr(
        ats_boards, "fetch_boards",
        lambda config, only=None, **kw: fake.results,
    )
    return fake


def test_it_reports_the_security_roles_and_reads_past_the_rest(boards) -> None:
    boards.results = [BoardResult("greenhouse", "canonical", jobs=[
        job("canonical", "Ubuntu Security Engineer"),
        job("canonical", "Senior Backend Engineer"),
        job("canonical", "Detection Engineer"),
    ])]

    found = pipeline.sweep(PREFS)

    assert [r.title for r in found.roles] == ["Detection Engineer",
                                              "Ubuntu Security Engineer"]
    assert found.boards == 1
    assert found.postings == 3


def test_results_are_sorted_by_company_then_title(boards) -> None:
    boards.results = [
        BoardResult("ashby", "duck-duck-go",
                    jobs=[job("duck-duck-go", "Web Security Engineer")]),
        BoardResult("greenhouse", "canonical",
                    jobs=[job("canonical", "Ubuntu Security Engineer"),
                          job("canonical", "Cloud Security Engineer")]),
    ]
    found = pipeline.sweep(PREFS)
    assert [(r.company, r.title) for r in found.roles] == [
        ("canonical", "Cloud Security Engineer"),
        ("canonical", "Ubuntu Security Engineer"),
        ("duck-duck-go", "Web Security Engineer"),
    ]


def test_any_level_widens_beyond_the_configured_list(boards) -> None:
    """Fastly is not on titles.any_level, so its Detection Engineer is only
    visible when the flag is passed."""
    boards.results = [BoardResult("greenhouse", "fastly", jobs=[
        job("fastly", "Threat Detection Analyst"),
        job("fastly", "Senior Security Engineer"),
    ])]

    assert len(pipeline.sweep(PREFS).roles) == 1
    assert len(pipeline.sweep(PREFS, any_level=True).roles) == 2


def test_exclusions_still_apply_under_any_level(boards) -> None:
    boards.results = [BoardResult("greenhouse", "canonical", jobs=[
        job("canonical", "Security Engineering Intern"),
        job("canonical", "Product Manager - Security"),
    ])]
    assert pipeline.sweep(PREFS, any_level=True).roles == []


def test_a_live_board_with_nothing_open_is_reported_as_quiet(boards) -> None:
    """Distinct from a board that failed: nothing to fix, just nothing open."""
    boards.results = [
        BoardResult("greenhouse", "mozilla", jobs=[job("mozilla", "Data Engineer")]),
        BoardResult("greenhouse", "canonical",
                    jobs=[job("canonical", "Ubuntu Security Engineer")]),
    ]
    found = pipeline.sweep(PREFS)
    assert found.quiet == ["greenhouse:mozilla"]
    assert found.boards == 2


def test_a_failed_board_is_surfaced_not_swallowed(boards) -> None:
    """A stale token is worth seeing — silently returning nothing for it is
    indistinguishable from the company having no security openings."""
    boards.results = [
        BoardResult("greenhouse", "grafanalabs", error="HTTP 404"),
        BoardResult("greenhouse", "canonical",
                    jobs=[job("canonical", "Ubuntu Security Engineer")]),
    ]
    found = pipeline.sweep(PREFS)
    assert found.failed == [("greenhouse:grafanalabs", "HTTP 404")]
    assert found.boards == 1          # the failure is not counted as read
    assert len(found.roles) == 1


def test_a_sweep_writes_nothing_unless_asked(boards, monkeypatch) -> None:
    """The whole point is that looking is free."""
    called = []
    monkeypatch.setattr(pipeline, "_store", lambda jobs, stats: called.append(jobs))

    boards.results = [BoardResult("greenhouse", "canonical",
                                  jobs=[job("canonical", "Ubuntu Security Engineer")])]

    pipeline.sweep(PREFS)
    assert called == []

    pipeline.sweep(PREFS, store=True)
    assert len(called) == 1


def test_save_stores_every_posting_not_just_the_matches(boards, monkeypatch) -> None:
    """`--save` then `rank` has to see the same corpus a discover would have
    given it, or the rule filter's own reasons never get recorded."""
    stored = []
    monkeypatch.setattr(pipeline, "_store",
                        lambda jobs, stats: stored.extend(jobs))

    boards.results = [BoardResult("greenhouse", "canonical", jobs=[
        job("canonical", "Ubuntu Security Engineer"),
        job("canonical", "Senior Backend Engineer"),
    ])]
    pipeline.sweep(PREFS, store=True)
    assert len(stored) == 2


# ------------------------------------------------------ location neutrality

def test_anywhere_keeps_only_the_roles_that_name_nowhere(boards) -> None:
    boards.results = [BoardResult("greenhouse", "canonical", jobs=[
        job("canonical", "Ubuntu Security Engineer", location="Home based - Worldwide"),
        job("canonical", "Cloud Security Engineer", location="Remote (United States)"),
        job("canonical", "Platform Security Engineer", location="Remote - EMEA"),
        job("canonical", "Product Security Engineer", location="Remote"),
    ])]

    found = pipeline.sweep(PREFS, neutral_only=True)

    assert [r.title for r in found.roles] == ["Product Security Engineer",
                                              "Ubuntu Security Engineer"]
    assert found.tied_down == 2


def test_neutrality_is_reported_even_when_not_filtering_on_it(boards) -> None:
    boards.results = [BoardResult("greenhouse", "canonical", jobs=[
        job("canonical", "Ubuntu Security Engineer", location="Remote"),
        job("canonical", "Cloud Security Engineer", location="Remote (United States)"),
    ])]

    found = pipeline.sweep(PREFS)
    assert {r.title: r.neutral for r in found.roles} == {
        "Ubuntu Security Engineer": True, "Cloud Security Engineer": False,
    }
    assert found.tied_down == 0   # nothing was dropped, so nothing to report


def test_a_board_with_only_pinned_roles_is_not_called_quiet(boards) -> None:
    """It has security openings — they are just not location-neutral. Calling
    that "nothing open" would be a different and wrong claim."""
    boards.results = [BoardResult("greenhouse", "canonical", jobs=[
        job("canonical", "Cloud Security Engineer", location="Remote (United States)"),
    ])]
    found = pipeline.sweep(PREFS, neutral_only=True)
    assert found.roles == []
    assert found.quiet == []
    assert found.tied_down == 1


# ------------------------------------------------------------------ retries

def fetcher_that_fails(times: int, exc: Exception):
    """A fetcher that raises `exc` the first `times` calls, then succeeds."""
    calls = []

    def fetch(token, client):
        calls.append(token)
        if len(calls) <= times:
            raise exc
        return [job(token, "Security Engineer")]

    return fetch, calls


def test_a_transient_failure_is_retried(monkeypatch) -> None:
    """At this concurrency a read timeout is normal, and losing the board
    costs its whole posting list — a different one went missing every run."""
    fetch, calls = fetcher_that_fails(2, httpx.ReadTimeout("timed out"))
    monkeypatch.setitem(ats_boards.FETCHERS, "greenhouse", fetch)
    monkeypatch.setattr(ats_boards.time, "sleep", lambda _: None)

    result = ats_boards._fetch_one("greenhouse", "datadog")

    assert result.error is None
    assert len(result.jobs) == 1
    assert len(calls) == 3


def test_retries_are_bounded_and_the_failure_is_reported(monkeypatch) -> None:
    fetch, calls = fetcher_that_fails(99, httpx.ConnectError("no route"))
    monkeypatch.setitem(ats_boards.FETCHERS, "greenhouse", fetch)
    monkeypatch.setattr(ats_boards.time, "sleep", lambda _: None)

    result = ats_boards._fetch_one("greenhouse", "datadog", attempts=3)

    assert len(calls) == 3
    assert "after 3 attempts" in result.error


def test_a_bad_token_is_not_retried(monkeypatch) -> None:
    """An HTTP status is a real answer. Asking again cannot change a 404, and
    retrying every stale token would trebled the cost of finding out."""
    response = httpx.Response(404, request=httpx.Request("GET", "https://x"))
    fetch, calls = fetcher_that_fails(
        99, httpx.HTTPStatusError("nope", request=response.request, response=response)
    )
    monkeypatch.setitem(ats_boards.FETCHERS, "greenhouse", fetch)

    result = ats_boards._fetch_one("greenhouse", "nosuchcompany")

    assert calls == ["nosuchcompany"]
    assert result.error == "HTTP 404"


def test_malformed_payloads_are_not_retried(monkeypatch) -> None:
    fetch, calls = fetcher_that_fails(99, ValueError("not json"))
    monkeypatch.setitem(ats_boards.FETCHERS, "greenhouse", fetch)

    result = ats_boards._fetch_one("greenhouse", "weird")

    assert calls == ["weird"]
    assert result.error == "not json"


# ------------------------------------------------- jobspy search configuration

def test_search_terms_come_from_config_when_set() -> None:
    from shotgun.discover import jobspy_source as js
    assert js._terms({"search_terms": ["cloud security engineer", " ai security "]}) \
        == ["cloud security engineer", "ai security"]


def test_search_terms_fall_back_to_the_defaults() -> None:
    """An empty or absent list must not silently sweep for nothing."""
    from shotgun.discover import jobspy_source as js
    assert js._terms({}) == js.SEARCH_TERMS
    assert js._terms({"search_terms": []}) == js.SEARCH_TERMS
    assert js._terms({"search_terms": ["  ", ""]}) == js.SEARCH_TERMS


def test_configured_locations_extend_rather_than_replace() -> None:
    """Overriding one region leaves the built-in queries for the others, so
    adding Poland does not quietly drop India."""
    from shotgun.discover import jobspy_source as js
    q = js._queries({"region_queries": {"europe": ["Poland", "Warsaw, Poland"]}},
                    ["europe", "india"])
    assert q[:2] == ["Poland", "Warsaw, Poland"]
    assert "Bengaluru, India" in q


def test_duplicate_locations_are_swept_once() -> None:
    from shotgun.discover import jobspy_source as js
    q = js._queries(
        {"region_queries": {"uk": ["London, United Kingdom"],
                            "europe": ["London, United Kingdom", "Berlin, Germany"]}},
        ["uk", "europe"],
    )
    assert q == ["London, United Kingdom", "Berlin, Germany"]


def test_a_region_with_no_locations_is_a_no_op_not_a_crash(caplog) -> None:
    """Adding a region to locations.regions without a query for it should be
    survivable — but not silent, or the sweep quietly skips a whole market."""
    from shotgun.discover import jobspy_source as js
    with caplog.at_level("WARNING"):
        assert js._queries({}, ["atlantis"]) == []
    assert "atlantis" in caplog.text


# ----------------------------------------------- discovering new boards

@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(db, "db_path", lambda: tmp_path / "test.db")
    db.init()
    with db.connect() as c:
        yield c


def test_token_variants_covers_the_spellings_boards_use() -> None:
    from shotgun.discover.ats_boards import token_variants

    # DuckDuckGo's real Ashby token is the hyphenated form, which is exactly
    # why the squashed spelling alone is not enough.
    assert "duck-duck-go" in token_variants("Duck Duck Go")
    assert "supabase" in token_variants("Supabase")


def test_token_variants_keeps_the_original_casing() -> None:
    """Board tokens are case-sensitive. Ubiminds publishes at
    jobs.lever.co/Ubiminds and 404s for "ubiminds", so lowercasing
    everything silently excluded every capitalised board — including the
    already-configured BoschGroup, Visa, IKEA and AveryDennison, none of
    which this function could ever have discovered."""
    from shotgun.discover.ats_boards import token_variants

    assert "Ubiminds" in token_variants("Ubiminds")
    assert "ubiminds" in token_variants("Ubiminds")
    # An all-lowercase name needs no second spelling.
    assert token_variants("offchainlabs") == ["offchainlabs"]


def test_token_variants_strips_legal_suffixes_and_punctuation() -> None:
    """No board token has ever contained "GmbH"."""
    from shotgun.discover.ats_boards import token_variants

    assert token_variants("Codesphere GmbH")[0] == "codesphere"
    assert "GmbH" not in " ".join(token_variants("Codesphere GmbH"))
    assert "apmollermaersk" in token_variants("A.P. Moller - Maersk")
    assert "a-p-moller-maersk" in token_variants("A.P. Moller - Maersk")


def test_token_variants_includes_the_first_word_alone() -> None:
    """Many two-word companies run their board under the first half."""
    from shotgun.discover.ats_boards import token_variants

    assert "thinking" in token_variants("Thinking Machines Lab")


def test_token_variants_survives_a_name_with_nothing_usable() -> None:
    from shotgun.discover.ats_boards import token_variants

    assert token_variants("") == []
    assert token_variants(None) == []
    assert token_variants("°°°") == []


def test_candidate_tokens_excludes_what_is_already_configured(conn) -> None:
    """It should return work, not a list to re-filter."""
    from shotgun.models import Job
    from shotgun.pipeline import candidate_tokens

    for company, source in (("Supabase", "arbeitnow"), ("Codesphere", "arbeitnow")):
        db.upsert_job(conn, Job(
            source=source, company=company, title="Security Engineer",
            url=f"https://example.com/{company}",
        ))

    prefs = Preferences(raw={"sources": {"ats_boards": {"ashby": ["supabase"]}}})
    out = candidate_tokens(conn, prefs)

    assert "codesphere" in out
    assert "supabase" not in out


def test_candidate_tokens_ignores_ats_sourced_postings(conn) -> None:
    """An ATS posting already came from a board we have, so its company is
    not a discovery. Only the aggregators are a company directory."""
    from shotgun.models import Job
    from shotgun.pipeline import candidate_tokens

    db.upsert_job(conn, Job(
        source="greenhouse:canonical", company="Canonical",
        title="Security Engineer", url="https://example.com/c",
    ))
    assert candidate_tokens(conn, Preferences(raw={})) == []
