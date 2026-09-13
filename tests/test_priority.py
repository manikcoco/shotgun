"""Company and country prioritisation.

Priority is written when a posting is scored, which is the trap this file
guards. Adding a company or country bonus to preferences.yaml has to reach the
postings already in the database — otherwise the edit silently applies to
future discoveries only, and the queue you actually read stays in the old
order. `reprioritise` is what closes that gap, and it has to do it without
paying for a single model call.
"""

from __future__ import annotations

import pytest

from shotgun import db
from shotgun.config import Preferences
from shotgun.models import Job, Stage
from shotgun.score import preference_bonus, reprioritise

PREFS = Preferences(raw={
    "locations": {"regions": ["europe", "uk", "india"]},
    "priority": {
        "company_groups": {
            "faang": {"bonus": 12, "companies": ["amazon", "aws", "google", "meta"]},
            "mango": {"bonus": 12, "companies": ["google", "nvidia", "openai"]},
            "cheap": {"bonus": 3, "companies": ["nvidia"]},
        },
        "country_bonus": {"GB": 8, "PL": 8},
    },
})


# ------------------------------------------------------------------- config

def test_groups_flatten_to_one_lookup() -> None:
    assert PREFS.company_bonuses["amazon"] == 12
    assert PREFS.country_bonuses == {"GB": 8, "PL": 8}


def test_a_company_in_two_groups_takes_the_higher_bonus() -> None:
    """Not the sum. Google is legitimately in both FAANG and MANGO, and
    listing it twice must not quietly double what it earns."""
    assert PREFS.company_bonuses["google"] == 12
    assert PREFS.company_bonuses["nvidia"] == 12   # 12 from mango, not 3+12


def test_missing_config_means_no_bonuses() -> None:
    empty = Preferences(raw={})
    assert empty.company_bonuses == {}
    assert empty.country_bonuses == {}
    assert preference_bonus({"company": "Google", "location": "London, UK"}, empty) \
        == (0, [])


def test_country_codes_are_normalised() -> None:
    prefs = Preferences(raw={"priority": {"country_bonus": {"gb": 8, " pl ": 5}}})
    assert prefs.country_bonuses == {"GB": 8, "PL": 5}


# ------------------------------------------------------------- the resolver

def row(company: str, location: str | None) -> dict:
    return {"company": company, "location": location}


def test_company_and_country_add_together() -> None:
    bonus, why = preference_bonus(row("Google", "London, United Kingdom"), PREFS)
    assert bonus == 20
    assert why == ["company google +12", "country GB +8"]


def test_poland_is_prioritised() -> None:
    bonus, why = preference_bonus(row("Some Startup", "Warsaw, Poland"), PREFS)
    assert bonus == 8
    assert why == ["country PL +8"]


def test_two_aliases_for_one_employer_count_once() -> None:
    """"Amazon Web Services (AWS)" matches both `amazon` and `aws`. That is
    one company, so it earns one bonus."""
    bonus, _ = preference_bonus(row("Amazon Web Services (AWS)", "Berlin, Germany"),
                                PREFS)
    assert bonus == 12


def test_matching_is_on_word_boundaries() -> None:
    """The reason aliases are matched with \\b rather than as substrings:
    `meta` would otherwise boost every posting from Metabase."""
    assert preference_bonus(row("Metabase", "Berlin, Germany"), PREFS) == (0, [])
    assert preference_bonus(row("Googler Inc", "Berlin, Germany"), PREFS) == (0, [])
    assert preference_bonus(row("Meta", "Berlin, Germany"), PREFS)[0] == 12


def test_a_region_with_no_country_earns_nothing() -> None:
    """"Remote - Europe" names no country, so there is no market to prefer."""
    assert preference_bonus(row("Some Startup", "Remote - Europe"), PREFS) == (0, [])


def test_missing_company_and_location_are_survivable() -> None:
    assert preference_bonus(row("", None), PREFS) == (0, [])


# ----------------------------------------------------------- reprioritise

@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(db, "db_path", lambda: tmp_path / "test.db")
    db.init()
    with db.connect() as c:
        yield c


def scored(conn, company: str, location: str, score: int) -> int:
    job_id, _ = db.upsert_job(conn, Job(
        source="ats", company=company, title="Staff Security Engineer",
        url=f"https://example.com/{company}/{location}", location=location,
    ))
    # priority deliberately written as the bare score, i.e. what a run before
    # the company and country bonuses existed would have stored.
    db.save_score(conn, job_id, score=score, level="staff", reasoning="fit",
                  priority=score)
    db.set_stage(conn, job_id, Stage.QUEUED)
    return job_id


def priority_of(conn, job_id: int) -> int:
    return db.score_row(conn, job_id)["priority"]


def test_reprioritise_reaches_scores_already_paid_for(conn) -> None:
    """The whole point. No model call, and the old rows move."""
    boosted = scored(conn, "Google", "London, United Kingdom", 70)
    plain = scored(conn, "Acme", "Berlin, Germany", 70)

    tally = reprioritise(conn, PREFS)

    assert priority_of(conn, boosted) == 90   # 70 + company 12 + country 8
    assert priority_of(conn, plain) == 70
    assert tally == {"changed": 1, "unchanged": 1, "signals_changed": 0}


def test_reprioritise_reorders_the_queue(conn) -> None:
    """A UK role at a prioritised company outranks a better-scoring role
    somewhere with no bonus — which is the behaviour being asked for."""
    scored(conn, "Acme", "Berlin, Germany", 80)
    scored(conn, "Google", "London, United Kingdom", 70)
    reprioritise(conn, PREFS)

    order = [r["company"] for r in db.applications_by_stage(conn, Stage.QUEUED)]
    assert order == ["Google", "Acme"]


def test_reprioritise_leaves_stages_alone(conn) -> None:
    """It changes the order of the queue, not its membership. `rebucket` is
    the one allowed to move things between stages."""
    job_id = scored(conn, "Google", "London, United Kingdom", 70)
    db.set_stage(conn, job_id, Stage.AWAITING_APPROVAL)
    reprioritise(conn, PREFS)

    assert db.application_detail(conn, job_id)["stage"] == str(Stage.AWAITING_APPROVAL)


def test_reprioritise_skips_unscored_rows(conn) -> None:
    """A rule-filtered posting has no score, so it has no priority to
    recompute — and must not be given one, or it stops sorting last."""
    job_id, _ = db.upsert_job(conn, Job(
        source="ats", company="Google", title="Staff Security Engineer",
        url="https://example.com/x", location="London, United Kingdom",
    ))
    db.save_score(conn, job_id, score=None, level=None, reasoning=None,
                  rule_reason="title matched no include pattern")
    db.set_stage(conn, job_id, Stage.FILTERED_OUT)

    assert reprioritise(conn, PREFS) == {"changed": 0, "unchanged": 0, "signals_changed": 0}
    assert priority_of(conn, job_id) is None


def test_reprioritise_is_idempotent(conn) -> None:
    scored(conn, "Google", "London, United Kingdom", 70)
    reprioritise(conn, PREFS)
    assert reprioritise(conn, PREFS) == {"changed": 0, "unchanged": 1, "signals_changed": 0}


def test_reprioritise_keeps_the_mobility_bonuses(conn) -> None:
    """Recomputing from stored signals must not lose them. The visa bonus was
    added by the run that scored the posting; if this pass rebuilt the signals
    wrongly it would silently strip 15 points off every sponsoring role."""
    job_id, _ = db.upsert_job(conn, Job(
        source="ats", company="Acme", title="Staff Security Engineer",
        url="https://example.com/v", location="Warsaw, Poland",
    ))
    db.save_score(conn, job_id, score=70, level="staff", reasoning="fit",
                  visa_sponsorship="yes", relocation_support="yes",
                  employment_type="permanent", priority=70)
    db.set_stage(conn, job_id, Stage.QUEUED)

    reprioritise(conn, PREFS)
    # 70 + visa 15 + relocation 10 + country PL 8
    assert priority_of(conn, job_id) == 103


def test_reprioritise_survives_an_unrecognised_stored_signal(conn) -> None:
    """Old rows should not be able to crash a free reordering pass."""
    job_id, _ = db.upsert_job(conn, Job(
        source="ats", company="Acme", title="Staff Security Engineer",
        url="https://example.com/w", location="Warsaw, Poland",
    ))
    db.save_score(conn, job_id, score=70, level="staff", reasoning="fit",
                  visa_sponsorship="maybe", employment_type="freelance-ish",
                  priority=70)
    db.set_stage(conn, job_id, Stage.QUEUED)

    reprioritise(conn, PREFS)
    assert priority_of(conn, job_id) == 78   # unknown signals, country PL only


# ------------------------------------------- signals are re-read, not trusted

def test_reprioritise_re_reads_the_visa_signals(conn) -> None:
    """The stored signals are derived data, so a fix to the visa patterns has
    to reach rows already in the database. Trusting the stored strings meant
    improving those patterns — which is exactly when this gets run — changed
    nothing for the 15,000 postings already scored.
    """
    job_id, _ = db.upsert_job(conn, Job(
        source="ats", company="Acme", title="Staff Security Engineer",
        url="https://example.com/s", location="Austin, Texas",
        description="We are happy to sponsor a visa for the right candidate.",
    ))
    # What an older, narrower pattern set recorded: it missed the offer.
    db.save_score(conn, job_id, score=70, level="staff", reasoning="fit",
                  visa_sponsorship="unknown", priority=45)
    db.set_stage(conn, job_id, Stage.QUEUED)

    tally = reprioritise(conn, PREFS)

    row = db.score_row(conn, job_id)
    assert row["visa_sponsorship"] == "yes"
    # Out of region but sponsoring, so it keeps its score plus the visa bonus.
    assert row["priority"] == 85
    assert tally["signals_changed"] == 1


def test_reprioritise_reports_a_signal_change_even_at_equal_priority(conn) -> None:
    """A re-read that corrects the record but happens to land on the same
    number still has to be written, or the row keeps a stale reading."""
    job_id, _ = db.upsert_job(conn, Job(
        source="ats", company="Acme", title="Staff Security Engineer",
        url="https://example.com/t", location="Berlin, Germany",
        description="Visa sponsorship is not available for this position.",
    ))
    db.save_score(conn, job_id, score=70, level="staff", reasoning="fit",
                  visa_sponsorship="unknown", priority=70)
    db.set_stage(conn, job_id, Stage.QUEUED)

    tally = reprioritise(conn, PREFS)

    assert db.score_row(conn, job_id)["visa_sponsorship"] == "no"
    assert tally["signals_changed"] == 1
    assert tally["changed"] == 1


def test_reprioritise_keeps_stored_signals_when_there_is_no_description(conn) -> None:
    """Nothing to re-read from, so the run that scored it stays authoritative
    rather than being silently downgraded to unknown."""
    job_id, _ = db.upsert_job(conn, Job(
        source="ats", company="Acme", title="Staff Security Engineer",
        url="https://example.com/u", location="Austin, Texas", description=None,
    ))
    db.save_score(conn, job_id, score=70, level="staff", reasoning="fit",
                  visa_sponsorship="yes", priority=85)
    db.set_stage(conn, job_id, Stage.QUEUED)

    tally = reprioritise(conn, PREFS)

    assert db.score_row(conn, job_id)["visa_sponsorship"] == "yes"
    assert tally["signals_changed"] == 0


def test_reprioritise_does_not_downgrade_what_claude_read(conn) -> None:
    """The regression this guards, and it is a costly one.

    What `scores` holds is not the regex's answer — it is the regex merged
    with Claude's, and Claude read the whole posting. A free re-read that
    overwrote instead of merging dropped n26's two 88-scoring security roles
    from priority 113 to 98 and moved them down the queue, because their
    sponsorship offer was one Claude had understood and no pattern caught.
    """
    job_id, _ = db.upsert_job(conn, Job(
        source="ats", company="Acme", title="Staff Security Engineer",
        url="https://example.com/claude", location="Berlin, Germany",
        description="A great team, a real product, and nothing about mobility.",
    ))
    db.save_score(conn, job_id, score=88, level="staff", reasoning="fit",
                  visa_sponsorship="yes", relocation_support="yes",
                  employment_type="permanent", priority=113)
    db.set_stage(conn, job_id, Stage.QUEUED)

    tally = reprioritise(conn, PREFS)

    row = db.score_row(conn, job_id)
    assert row["visa_sponsorship"] == "yes"
    assert row["relocation_support"] == "yes"
    assert row["priority"] == 113
    assert tally["signals_changed"] == 0


def test_an_explicit_refusal_still_overrides_what_claude_read(conn) -> None:
    """The other direction has to keep working. Claude occasionally reads
    "we welcome applicants worldwide" as sponsorship; a posting that says in
    plain words that it will not sponsor is not a maybe."""
    job_id, _ = db.upsert_job(conn, Job(
        source="ats", company="Acme", title="Staff Security Engineer",
        url="https://example.com/refuse", location="Austin, Texas",
        description="Visa sponsorship is not available for this position.",
    ))
    db.save_score(conn, job_id, score=88, level="staff", reasoning="fit",
                  visa_sponsorship="yes", priority=103)
    db.set_stage(conn, job_id, Stage.QUEUED)

    reprioritise(conn, PREFS)

    row = db.score_row(conn, job_id)
    assert row["visa_sponsorship"] == "no"
    assert row["priority"] == 38          # out of region, explicit refusal: 88 - 50
