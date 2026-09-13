"""The free pass, and counting what it keeps.

Two things are load-bearing here. The filter has to reject on the cheap checks
before it reads a description, or a pass over a real corpus takes 24 seconds
instead of two. And `filter_tally` has to answer "how many roles actually match
me" without a model call, because `status` reports stages and a backlog of
9,573 unranked postings is neither kept nor rejected.
"""

from __future__ import annotations

import pytest

from shotgun import db, score, visa
from shotgun.config import Preferences
from shotgun.models import Job, Stage
from shotgun.score import filter_tally, rule_filter

PREFS = Preferences(raw={
    "titles": {
        "include": [r"\b(security|appsec)\b.*\bengineer\b"],
        "exclude": [r"\bintern\b"],
    },
    "locations": {"regions": ["europe", "uk", "india"], "accept_remote": True,
                  "reject_other_countries": True,
                  "allow_if_mobility_friendly": ["US"]},
    "compensation": {"minimum": {"EUR": 70000}},
    "scoring": {"dealbreakers": ["polygraph"]},
})

SPONSORS = "Visa sponsorship is available for this role."


def row(**over) -> dict:
    base = dict(title="Staff Security Engineer", company="Acme",
                description="Ordinary posting text.", location="Berlin, Germany",
                salary_currency=None, salary_max=None)
    return base | over


# ------------------------------------------------------- cheap checks first

def test_a_title_rejection_never_reads_the_description(monkeypatch) -> None:
    """The performance fix, asserted where it can actually regress. The title
    check rejects ~94% of a real corpus and costs a fraction of what reading
    the description costs; doing them the other way round meant analysing
    9,000 postings in order to throw the answer away."""
    called = []
    monkeypatch.setattr(visa, "analyse",
                        lambda *a, **k: called.append(a) or visa.VisaSignals())

    ok, why, signals = rule_filter(row(title="Senior Chef"), PREFS)

    assert not ok
    assert why == "title matched no include pattern"
    assert called == []
    assert signals.sponsorship is visa.Support.UNKNOWN


def test_a_blocked_company_never_reads_the_description(monkeypatch) -> None:
    called = []
    monkeypatch.setattr(visa, "analyse",
                        lambda *a, **k: called.append(a) or visa.VisaSignals())
    prefs = Preferences(raw=PREFS.raw | {"apply": {"blocked_companies": ["acme"]}})

    ok, why, _ = rule_filter(row(), prefs)
    assert not ok
    assert "blocked" in why
    assert called == []


def test_a_posting_that_clears_the_title_is_still_analysed() -> None:
    """Skipping the analysis entirely would break the thing it exists for:
    an out-of-region role that sponsors has to survive the location check."""
    ok, _, signals = rule_filter(
        row(location="Austin, Texas", description=SPONSORS), PREFS,
    )
    assert ok
    assert signals.sponsorship is visa.Support.YES


def test_an_out_of_region_role_with_no_mobility_is_rejected() -> None:
    ok, why, _ = rule_filter(row(location="Austin, Texas"), PREFS)
    assert not ok
    assert why == "US needs visa/reloc/contract; none stated"


def test_the_comp_floor_only_bites_when_a_number_is_published() -> None:
    assert rule_filter(row(), PREFS)[0]
    ok, why, _ = rule_filter(
        row(salary_currency="EUR", salary_max=50000), PREFS,
    )
    assert not ok
    assert "below floor" in why


def test_a_dealbreaker_phrase_in_the_jd_rejects() -> None:
    ok, why, _ = rule_filter(row(description="You must pass a polygraph."), PREFS)
    assert not ok
    assert "polygraph" in why


# ------------------------------------------------------------------- tally

@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(db, "db_path", lambda: tmp_path / "test.db")
    db.init()
    with db.connect() as c:
        yield c


def store(conn, title: str, *, location="Berlin, Germany", scored=False,
          stage=Stage.DISCOVERED) -> int:
    job_id, _ = db.upsert_job(conn, Job(
        source="ats", company="Acme", title=title, location=location,
        url=f"https://example.com/{title}", description="Ordinary posting text.",
    ))
    if scored:
        db.save_score(conn, job_id, score=72, level="staff", reasoning="fit")
    db.set_stage(conn, job_id, stage)
    return job_id


def test_it_counts_what_matches_and_what_does_not(conn) -> None:
    store(conn, "Staff Security Engineer")
    store(conn, "Senior Security Engineer")
    store(conn, "Senior Chef")

    tally = filter_tally(conn, PREFS)

    assert tally["total"] == 3
    assert tally["passed"] == 2
    assert tally["rejected"] == 1
    assert tally["reasons"]["title matched no include pattern"] == 1
    assert tally["companies"]["Acme"] == 2


def test_pending_is_the_bill_the_next_rank_would_run_up(conn) -> None:
    store(conn, "Staff Security Engineer", scored=True, stage=Stage.QUEUED)
    store(conn, "Senior Security Engineer")

    tally = filter_tally(conn, PREFS)
    assert tally["passed"] == 2
    assert tally["pending"] == 1


def test_a_posting_an_older_filter_rejected_still_counts_as_pending(conn) -> None:
    """It sits at `filtered_out` with no score. Widening the filter is exactly
    when this number matters, so keying off the stage would hide the cost of
    the edit that just got made."""
    store(conn, "Senior Security Engineer", stage=Stage.FILTERED_OUT)

    tally = filter_tally(conn, PREFS)
    assert tally["passed"] == 1
    assert tally["pending"] == 1


def test_nothing_stored_is_not_an_error(conn) -> None:
    tally = filter_tally(conn, PREFS)
    assert tally["total"] == 0
    assert tally["passed"] == 0
    assert tally["pending"] == 0


def test_the_tally_makes_no_model_call(conn, monkeypatch) -> None:
    """The whole reason it is worth having."""
    def boom(*a, **k):
        raise AssertionError("filter_tally must not call the model")

    monkeypatch.setattr(score, "score_with_claude", boom)
    store(conn, "Staff Security Engineer")
    assert filter_tally(conn, PREFS)["passed"] == 1


# ------------------------------------------------------------ dealbreakers

EPPA = (
    "Elastic is an equal opportunity employer. See the "
    '<a href="https://www.dol.gov/eppac.pdf">Employee Polygraph Protection '
    "Act (EPPA)</a> Poster and the Family and Medical Leave Act poster."
)


def test_a_dealbreaker_can_exclude_its_own_false_positive() -> None:
    """`polygraph` as a plain substring matched the EPPA poster link that
    US-incorporated employers paste into every posting — a law *forbidding*
    employers from demanding a polygraph. It was rejecting eight in-region
    Elastic security roles across Spain, Portugal, Greece, Ireland, Poland
    and the UK."""
    prefs = Preferences(raw=PREFS.raw | {
        "scoring": {"dealbreakers": [r"\bpolygraph\b(?!\s+protection)"]},
    })
    ok, why, _ = rule_filter(row(description=EPPA), prefs)
    assert ok, why


def test_a_real_polygraph_requirement_is_still_a_dealbreaker() -> None:
    prefs = Preferences(raw=PREFS.raw | {
        "scoring": {"dealbreakers": [r"\bpolygraph\b(?!\s+protection)"]},
    })
    ok, why, _ = rule_filter(
        row(description="Candidates must pass a polygraph examination."), prefs,
    )
    assert not ok
    assert "dealbreaker" in why


def test_dealbreakers_are_case_insensitive() -> None:
    prefs = Preferences(raw=PREFS.raw | {
        "scoring": {"dealbreakers": ["active security clearance"]},
    })
    ok, _, _ = rule_filter(
        row(description="An ACTIVE SECURITY CLEARANCE is required."), prefs,
    )
    assert not ok


def test_an_invalid_pattern_is_matched_literally_rather_than_raising() -> None:
    """The config is hand-edited, so a phrase that happens not to be valid
    regex must not take the whole filter pass down with it. An unbalanced
    bracket is the realistic version of that mistake.

    Note what this does *not* protect against: a phrase that is valid regex
    but does not mean what was intended. "c++ (required)" compiles on Python
    3.11 and later, where `c++` is a possessive quantifier rather than two
    literal plus signs, so it silently stops matching the text it was written
    for. Only outright syntax errors fall back to a literal match.
    """
    prefs = Preferences(raw=PREFS.raw | {
        "scoring": {"dealbreakers": ["onsite 5 days (hybrid"]},
    })
    assert rule_filter(row(description="Ordinary text."), prefs)[0]
    ok, why, _ = rule_filter(
        row(description="This role is onsite 5 days (hybrid is not offered)."), prefs,
    )
    assert not ok
    assert "dealbreaker" in why


def test_a_blocked_company_matches_on_word_boundaries() -> None:
    """A block list is a "never apply here" instruction, usually a current
    employer, and the boards disagree about names: the ATS says
    "acme" where LinkedIn says "Acme GmbH". An exact
    compare honoured the rule on one source and ignored it on the other."""
    prefs = Preferences(raw=PREFS.raw | {
        "apply": {"blocked_companies": ["acme"]},
    })
    for name in ("acme", "acme GmbH", "Acme Inc"):
        ok, why, _ = rule_filter(row(company=name), prefs)
        assert not ok, name
        assert "blocked list" in why


def test_the_block_list_does_not_catch_a_company_that_merely_contains_it() -> None:
    prefs = Preferences(raw=PREFS.raw | {"apply": {"blocked_companies": ["meta"]}})
    assert rule_filter(row(company="Metabase"), prefs)[0]
    assert not rule_filter(row(company="Meta"), prefs)[0]


# ------------------------------------------------------------ mobility view

MOB_PREFS = Preferences(raw=PREFS.raw | {
    "titles": {
        "include": [r"\b(security|appsec)\b.*\bengineer\b"],
        "exclude": [r"\bintern\b"],
        "any_level": {"companies": [], "include": [r"\bsecurity\b"]},
    },
})


def mob_store(conn, title: str, company: str, description: str | None,
              location="Berlin, Germany") -> int:
    job_id, _ = db.upsert_job(conn, Job(
        source="ats", company=company, title=title, location=location,
        url=f"https://example.com/{company}/{title}", description=description,
    ))
    db.set_stage(conn, job_id, Stage.DISCOVERED)
    return job_id


PAD = " Ordinary posting prose about the team and the product." * 6


def test_mobility_separates_silence_from_refusal(conn) -> None:
    """The distinction the whole view exists for. Most employers never
    mention sponsorship, and reading that as "no" would discard most of the
    market — so silence is its own bucket, not a refusal."""
    from shotgun.score import mobility_report

    mob_store(conn, "Security Engineer", "Offers",
              "Visa sponsorship is available." + PAD)
    mob_store(conn, "Security Engineer", "Refuses",
              "Visa sponsorship is not available for this position." + PAD)
    mob_store(conn, "Security Engineer", "Quiet", "Nothing about mobility." + PAD)

    stances = {r["company"]: r["stance"] for r in mobility_report(conn, MOB_PREFS)}
    assert stances == {"Offers": "offers", "Refuses": "refuses", "Quiet": "silent"}


def test_a_posting_with_no_description_is_unreadable_not_silent(conn) -> None:
    """SmartRecruiters publishes no description on its list endpoint and
    LinkedIn omits it from search results — 324 security roles here. That is
    a gap in our data, not an answer from the employer, and calling it
    "silent" would claim to have read something that was never fetched."""
    from shotgun.score import mobility_report

    mob_store(conn, "Security Engineer", "NoDesc", None)
    assert mobility_report(conn, MOB_PREFS)[0]["stance"] == "unreadable"


def test_relocation_alone_counts_as_an_offer(conn) -> None:
    """Relocation without sponsorship is still an employer paying to move
    you, which is the question being asked."""
    from shotgun.score import mobility_report

    mob_store(conn, "Security Engineer", "Reloc",
              "We offer a generous relocation package." + PAD)
    role = mobility_report(conn, MOB_PREFS)[0]
    assert role["stance"] == "offers"
    assert role["relocation"] is visa.Support.YES
    assert role["sponsorship"] is visa.Support.UNKNOWN


def test_mobility_marks_target_regions(conn) -> None:
    from shotgun.score import mobility_report

    mob_store(conn, "Security Engineer", "Local",
              "Visa sponsorship is available." + PAD, location="Berlin, Germany")
    mob_store(conn, "Security Engineer", "Away",
              "Visa sponsorship is available." + PAD, location="Austin, Texas")

    flags = {r["company"]: r["in_region"] for r in mobility_report(conn, MOB_PREFS)}
    assert flags == {"Local": True, "Away": False}


def test_mobility_covers_unscored_postings(conn) -> None:
    """It reads descriptions rather than the `scores` table, which is the
    point: 603 of these roles have never been scored, and a view that needed
    a score would show almost nothing."""
    from shotgun.score import mobility_report

    mob_store(conn, "Security Engineer", "Unscored",
              "Visa sponsorship is available." + PAD)
    assert db.score_row(conn, 1) is None
    assert len(mobility_report(conn, MOB_PREFS)) == 1


def test_mobility_ignores_non_security_roles(conn) -> None:
    from shotgun.score import mobility_report

    mob_store(conn, "Senior Chef", "Kitchen",
              "Visa sponsorship is available." + PAD)
    assert mobility_report(conn, MOB_PREFS) == []


def test_mobility_stances_are_ordered_best_first() -> None:
    """`--stance all` sorts on this order, and the order is the feature.

    Grouping by company alone put the largest employer first, so the combined
    view opened on twelve Bosch project coordinators while the sponsoring
    roles sat 300 rows down — the opposite of what the question is asking.
    """
    from shotgun.score import MOBILITY_STANCES

    assert MOBILITY_STANCES == ("offers", "refuses", "silent", "unreadable")
