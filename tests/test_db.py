"""Job upsert and the answer store.

The backfill in `upsert_job` matters more than it looks. It originally covered
only description, apply_url and ats, which silently capped a later fix: when
the Ashby fetcher started reading the compensation object it already requested,
only genuinely new rows picked it up — 6,941 known rows kept their NULL
salaries and the comp floor stayed inert.
"""

from __future__ import annotations

import pytest

from shotgun import db
from shotgun.models import Job, Stage


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(db, "db_path", lambda: tmp_path / "test.db")
    db.init()
    with db.connect() as c:
        yield c


def job(**over) -> Job:
    base = dict(source="ats", company="Acme", title="Staff Security Engineer",
                url="https://example.com/1")
    return Job(**(base | over))


def test_insert_then_recognise_a_duplicate(conn) -> None:
    first_id, is_new = db.upsert_job(conn, job())
    assert is_new
    same_id, is_new = db.upsert_job(conn, job())
    assert not is_new
    assert same_id == first_id


def test_fingerprint_ignores_source_and_url(conn) -> None:
    """The same role found on two boards must dedupe to one row."""
    db.upsert_job(conn, job(source="greenhouse", url="https://gh/1"))
    _, is_new = db.upsert_job(conn, job(source="linkedin", url="https://li/9"))
    assert not is_new


def test_salary_is_backfilled_onto_a_known_row(conn) -> None:
    """The regression. A first sighting with no compensation, then a source
    that has it — the figure must land."""
    job_id, _ = db.upsert_job(conn, job())
    assert db.job_row(conn, job_id)["salary_max"] is None

    db.upsert_job(conn, job(salary_min=120000, salary_max=160000,
                            salary_currency="EUR"))
    row = db.job_row(conn, job_id)
    assert row["salary_min"] == 120000
    assert row["salary_max"] == 160000
    assert row["salary_currency"] == "EUR"


def test_backfill_never_overwrites_what_we_already_have(conn) -> None:
    """COALESCE, not assignment: a source lacking a field must not wipe it."""
    job_id, _ = db.upsert_job(conn, job(
        description="full JD", salary_max=160000, salary_currency="EUR",
        location="Berlin, Germany", posted_at="2026-08-01",
    ))
    db.upsert_job(conn, job())   # same role, no detail at all

    row = db.job_row(conn, job_id)
    assert row["description"] == "full JD"
    assert row["salary_max"] == 160000
    assert row["location"] == "Berlin, Germany"
    assert row["posted_at"] == "2026-08-01"


def test_location_is_backfilled_only_when_missing(conn) -> None:
    """Two postings sharing a fingerprint but naming different locations is the
    known merge problem; picking a winner here would only change which one is
    lost, so the existing value stands."""
    job_id, _ = db.upsert_job(conn, job())
    db.upsert_job(conn, job(location="Remote, Poland"))
    assert db.job_row(conn, job_id)["location"] == "Remote, Poland"

    db.upsert_job(conn, job(location="Remote, United Kingdom"))
    assert db.job_row(conn, job_id)["location"] == "Remote, Poland"


def test_description_and_apply_url_backfill(conn) -> None:
    job_id, _ = db.upsert_job(conn, job())
    db.upsert_job(conn, job(description="the JD", apply_url="https://apply",
                            ats="greenhouse"))
    row = db.job_row(conn, job_id)
    assert row["description"] == "the JD"
    assert row["apply_url"] == "https://apply"
    assert row["ats"] == "greenhouse"


# ------------------------------------------------------- answer store

def test_answer_round_trip(conn) -> None:
    db.put_answer(conn, "notice_period", "What is your notice period?", "2 months")
    row = db.get_answer(conn, "notice_period")
    assert row["value"] == "2 months"
    assert row["sensitive"] == 0
    assert row["confidence"] == "confirmed"


def test_put_answer_updates_in_place(conn) -> None:
    db.put_answer(conn, "notice_period", "q", "3 months")
    db.put_answer(conn, "notice_period", "q", "2 months")
    assert len(db.list_answers(conn)) == 1
    assert db.get_answer(conn, "notice_period")["value"] == "2 months"


def test_sensitive_answers_are_marked(conn) -> None:
    db.put_answer(conn, "salary", "Expected salary?", "120000", sensitive=True)
    assert db.get_answer(conn, "salary")["sensitive"] == 1


def test_delete_answer(conn) -> None:
    db.put_answer(conn, "pronouns", "q", "he/him")
    assert db.delete_answer(conn, "pronouns") is True
    assert db.delete_answer(conn, "pronouns") is False
    assert db.get_answer(conn, "pronouns") is None


def test_list_answers_is_sorted(conn) -> None:
    for key in ("salary", "notice_period", "pronouns"):
        db.put_answer(conn, key, "q", "v")
    assert [r["key"] for r in db.list_answers(conn)] == [
        "notice_period", "pronouns", "salary"]


# ------------------------------------------------------------ stages

def test_set_stage_creates_then_updates(conn) -> None:
    job_id, _ = db.upsert_job(conn, job())
    db.set_stage(conn, job_id, Stage.DISCOVERED)
    db.set_stage(conn, job_id, Stage.QUEUED, note="passed the filter")

    row = db.application_detail(conn, job_id)
    assert row["stage"] == str(Stage.QUEUED)
    assert row["notes"] == "passed the filter"
    kinds = [e["kind"] for e in db.events_for(conn, job_id)]
    assert f"stage:{Stage.QUEUED}" in kinds
    assert f"stage:{Stage.DISCOVERED}" in kinds


def test_filled_at_is_stamped_on_awaiting_approval(conn) -> None:
    job_id, _ = db.upsert_job(conn, job())
    db.set_stage(conn, job_id, Stage.QUEUED)
    assert db.application_detail(conn, job_id)["filled_at"] is None
    db.set_stage(conn, job_id, Stage.AWAITING_APPROVAL, resume_path="/tmp/r.pdf")
    assert db.application_detail(conn, job_id)["filled_at"] is not None


def test_submitted_at_is_stamped_on_submit(conn) -> None:
    job_id, _ = db.upsert_job(conn, job())
    db.set_stage(conn, job_id, Stage.SUBMITTED)
    assert db.application_detail(conn, job_id)["submitted_at"] is not None


def test_filled_at_is_stamped_even_on_a_fresh_row(conn) -> None:
    """The insert path used to skip the milestone timestamps entirely."""
    job_id, _ = db.upsert_job(conn, job())
    db.set_stage(conn, job_id, Stage.AWAITING_APPROVAL, resume_path="/tmp/r.pdf")
    assert db.application_detail(conn, job_id)["filled_at"] is not None


def test_an_ordinary_stage_stamps_neither_timestamp(conn) -> None:
    job_id, _ = db.upsert_job(conn, job())
    db.set_stage(conn, job_id, Stage.QUEUED)
    row = db.application_detail(conn, job_id)
    assert row["filled_at"] is None
    assert row["submitted_at"] is None


def test_a_skipped_answer_is_stored_but_not_auto_fillable(conn) -> None:
    from shotgun.answers import SKIPPED

    db.put_answer(conn, "referral", "How did you hear?", "", confidence=SKIPPED)
    db.put_answer(conn, "notice_period", "Notice?", "2 months")

    row = db.get_answer(conn, "referral")
    assert row is not None and row["confidence"] == SKIPPED

    # The exact query pipeline._stored_answers() runs.
    fillable = {
        r["key"] for r in conn.execute(
            "SELECT key FROM answers WHERE sensitive = 0 AND confidence = 'confirmed'"
        )
    }
    assert "referral" not in fillable
    assert "notice_period" in fillable


# ------------------------------------------- prepare_one reuse_docs

def test_reuse_docs_refuses_when_nothing_is_on_disk(conn, tmp_path) -> None:
    """The whole point of the flag is spending nothing, so it must fail loudly
    rather than fall back to tailoring."""
    from shotgun import pipeline

    job_id, _ = db.upsert_job(conn, job())
    db.set_stage(conn, job_id, Stage.QUEUED)

    out = pipeline.prepare_one(conn, job_id, None, "", reuse_docs=True)
    assert out["status"] == "error"
    assert "no documents on disk" in out["detail"]


def test_reuse_docs_refuses_when_the_recorded_file_is_gone(conn, tmp_path) -> None:
    from shotgun import pipeline

    job_id, _ = db.upsert_job(conn, job())
    db.set_stage(conn, job_id, Stage.QUEUED,
                 resume_path=str(tmp_path / "deleted.pdf"))

    out = pipeline.prepare_one(conn, job_id, None, "", reuse_docs=True)
    assert out["status"] == "error"


def test_reuse_docs_uses_the_recorded_files_without_tailoring(conn, tmp_path) -> None:
    from shotgun import pipeline

    resume = tmp_path / "resume.pdf"
    resume.write_bytes(b"%PDF-1.4 fake")
    cover = tmp_path / "cover.txt"
    cover.write_text("the cover letter")

    job_id, _ = db.upsert_job(conn, job())
    db.set_stage(conn, job_id, Stage.QUEUED,
                 resume_path=str(resume), cover_path=str(cover))

    # dry_run stops before the browser; prof/profile_yaml are unused because
    # no model call happens — passing None proves it.
    out = pipeline.prepare_one(conn, job_id, None, "", reuse_docs=True, dry_run=True)
    assert out["status"] == "documents_only"
    assert out["reused"] is True
    assert out["resume"] == str(resume)


# --------------------------------------------------- refilter backlog

def test_unscored_jobs_skips_rule_filtered_by_default(conn) -> None:
    job_id, _ = db.upsert_job(conn, job())
    db.save_score(conn, job_id, score=None, level=None, reasoning=None,
                  rule_reason="title matched no include pattern")
    assert db.unscored_jobs(conn) == []


def test_refilter_reaches_rule_filtered_jobs(conn) -> None:
    """Widening titles.include in preferences was unable to reach the postings
    it was widened for: they carry a rule-only score row from the old rules and
    the default query cannot see them."""
    job_id, _ = db.upsert_job(conn, job())
    db.save_score(conn, job_id, score=None, level=None, reasoning=None,
                  rule_reason="title matched no include pattern")

    rows = db.unscored_jobs(conn, include_rule_filtered=True)
    assert [r["id"] for r in rows] == [job_id]


def test_refilter_never_returns_a_model_scored_job(conn) -> None:
    """So a refilter pass cannot re-spend on work already paid for."""
    job_id, _ = db.upsert_job(conn, job())
    db.save_score(conn, job_id, score=82, level="staff", reasoning="good fit")
    assert db.unscored_jobs(conn, include_rule_filtered=True) == []


def test_a_never_seen_job_is_returned_either_way(conn) -> None:
    job_id, _ = db.upsert_job(conn, job())
    assert [r["id"] for r in db.unscored_jobs(conn)] == [job_id]
    assert [r["id"] for r in db.unscored_jobs(conn, include_rule_filtered=True)] == [job_id]


# ------------------------------------------------------ list ordering

def test_a_rule_filtered_job_never_outranks_a_scored_one(conn) -> None:
    """visa.analyse() reads the whole description, so any posting carrying
    sponsorship boilerplate got visa='yes' — and with that as the leading sort
    key, "Enterprise Account Executive, Brazil" outranked an 84-scoring
    security role in every listing."""
    reject, _ = db.upsert_job(conn, job(title="Enterprise Account Executive, Brazil"))
    db.save_score(conn, reject, score=None, level=None, reasoning=None,
                  rule_reason="title matched no include pattern",
                  visa_sponsorship="yes", relocation_support="yes")
    db.set_stage(conn, reject, Stage.FILTERED_OUT)

    good, _ = db.upsert_job(conn, job(title="Cloud Security Engineer", company="ClickHouse"))
    db.save_score(conn, good, score=84, level="staff", reasoning="strong",
                  priority=84)
    db.set_stage(conn, good, Stage.QUEUED)

    order = [r["job_id"] for r in db.all_applications(conn)]
    assert order.index(good) < order.index(reject)


def test_a_low_score_with_mobility_does_not_outrank_a_high_one(conn) -> None:
    low, _ = db.upsert_job(conn, job(title="Software Engineer, Security Observability"))
    db.save_score(conn, low, score=3, level="other", reasoning="poor fit",
                  relocation_support="yes", priority=0)
    db.set_stage(conn, low, Stage.SCORED_LOW)

    high, _ = db.upsert_job(conn, job(title="Cloud Security Engineer", company="ClickHouse"))
    db.save_score(conn, high, score=84, level="staff", reasoning="strong", priority=84)
    db.set_stage(conn, high, Stage.QUEUED)

    order = [r["job_id"] for r in db.all_applications(conn)]
    assert order.index(high) < order.index(low)


def test_sponsorship_still_wins_between_comparable_roles(conn) -> None:
    """The intent survives — it just runs through `priority`, where
    visa.priority() has already added the bonus."""
    sponsored, _ = db.upsert_job(conn, job(title="Staff Security Engineer", company="Sponsors"))
    db.save_score(conn, sponsored, score=70, level="staff", reasoning="fit",
                  visa_sponsorship="yes", priority=95)
    db.set_stage(conn, sponsored, Stage.QUEUED)

    silent, _ = db.upsert_job(conn, job(title="Staff Security Engineer", company="Silent"))
    db.save_score(conn, silent, score=84, level="staff", reasoning="fit", priority=84)
    db.set_stage(conn, silent, Stage.QUEUED)

    order = [r["job_id"] for r in db.all_applications(conn)]
    assert order.index(sponsored) < order.index(silent)


# ------------------------------------------------- threshold rebucket

def test_rebucket_requeues_when_the_threshold_drops(conn) -> None:
    """Lowering scoring.minimum_score has to reach scores already paid for, or
    the edit means nothing."""
    from shotgun.config import Preferences
    from shotgun.score import rebucket

    job_id, _ = db.upsert_job(conn, job())
    db.save_score(conn, job_id, score=68, level="staff", reasoning="close")
    db.set_stage(conn, job_id, Stage.SCORED_LOW)

    prefs = Preferences.load()
    prefs.raw.setdefault("scoring", {})["minimum_score"] = 50
    moved = rebucket(conn, prefs)

    assert moved["to_queued"] == 1
    assert db.application_detail(conn, job_id)["stage"] == str(Stage.QUEUED)


def test_rebucket_respects_dealbreakers(conn) -> None:
    from shotgun.config import Preferences
    from shotgun.score import rebucket

    job_id, _ = db.upsert_job(conn, job())
    db.save_score(conn, job_id, score=90, level="staff", reasoning="fit",
                  dealbreakers=["Active Top Secret clearance required"])
    db.set_stage(conn, job_id, Stage.SCORED_LOW)

    prefs = Preferences.load()
    prefs.raw.setdefault("scoring", {})["minimum_score"] = 50
    rebucket(conn, prefs)
    assert db.application_detail(conn, job_id)["stage"] == str(Stage.SCORED_LOW)


def test_rebucket_leaves_submitted_applications_alone(conn) -> None:
    """A config edit must not disturb something already sent."""
    from shotgun.config import Preferences
    from shotgun.score import rebucket

    job_id, _ = db.upsert_job(conn, job())
    db.save_score(conn, job_id, score=20, level="other", reasoning="weak")
    db.set_stage(conn, job_id, Stage.SUBMITTED)

    prefs = Preferences.load()
    prefs.raw.setdefault("scoring", {})["minimum_score"] = 50
    rebucket(conn, prefs)
    assert db.application_detail(conn, job_id)["stage"] == str(Stage.SUBMITTED)


def test_rebucket_never_calls_the_model(conn, monkeypatch) -> None:
    from shotgun import llm
    from shotgun.config import Preferences
    from shotgun.score import rebucket

    monkeypatch.setattr(llm, "client", lambda: pytest.fail("rebucket must be free"))
    job_id, _ = db.upsert_job(conn, job())
    db.save_score(conn, job_id, score=68, level="staff", reasoning="close")
    db.set_stage(conn, job_id, Stage.SCORED_LOW)
    rebucket(conn, Preferences.load())


# ------------------------------------------------ applied/not-applied

def test_submitted_at_drives_the_applied_column(conn) -> None:
    """The stage encodes it, but nothing about 'awaiting_approval' says
    'filled in and never sent', which is the state that matters."""
    from shotgun.cli import APPLIED_STAGES, _applied_text

    sent, _ = db.upsert_job(conn, job(title="Sent Role"))
    db.set_stage(conn, sent, Stage.SUBMITTED)
    row = db.application_detail(conn, sent)
    assert "yes" in _applied_text(row).plain
    assert row["submitted_at"][:4] in _applied_text(row).plain

    filled, _ = db.upsert_job(conn, job(title="Filled Role"))
    db.set_stage(conn, filled, Stage.AWAITING_APPROVAL, resume_path="/tmp/r.pdf")
    assert _applied_text(db.application_detail(conn, filled)).plain == "filled"

    queued, _ = db.upsert_job(conn, job(title="Queued Role"))
    db.set_stage(conn, queued, Stage.QUEUED)
    assert _applied_text(db.application_detail(conn, queued)).plain == "no"

    assert Stage.INTERVIEW in APPLIED_STAGES
    assert Stage.QUEUED not in APPLIED_STAGES


def test_post_submit_stages_all_count_as_applied(conn) -> None:
    from shotgun.cli import _applied_text

    for stage in (Stage.ACKNOWLEDGED, Stage.SCREENING, Stage.INTERVIEW,
                  Stage.OFFER, Stage.REJECTED):
        jid, _ = db.upsert_job(conn, job(title=f"Role {stage}"))
        db.set_stage(conn, jid, Stage.SUBMITTED)
        db.set_stage(conn, jid, stage)
        assert "yes" in _applied_text(db.application_detail(conn, jid)).plain, stage


def test_failed_and_withdrawn_are_not_applied(conn) -> None:
    from shotgun.cli import _applied_text

    failed, _ = db.upsert_job(conn, job(title="Blew Up"))
    db.set_stage(conn, failed, Stage.FAILED)
    assert _applied_text(db.application_detail(conn, failed)).plain == "failed"

    dropped, _ = db.upsert_job(conn, job(title="Dropped"))
    db.set_stage(conn, dropped, Stage.WITHDRAWN)
    assert _applied_text(db.application_detail(conn, dropped)).plain == "dropped"


# --------------------------------------------- rejection reason gist

def test_reason_gist_collapses_regex_noise() -> None:
    """The stored reason embeds the regex that matched, so 2,209 rejections
    read as 2,209 distinct strings. Grouping needs the category."""
    from shotgun.cli import _reason_gist

    a = _reason_gist(r"title excluded by /\b(sales|account executive)\b/")
    b = _reason_gist(r"title excluded by /\b(recruiter|talent|sourcer)\b/")
    assert a == b == "title excluded"


@pytest.mark.parametrize(
    ("stored", "expected"),
    [
        (r"title matched no include pattern", "title matched no include pattern"),
        ("US needs visa/reloc/contract; none stated", "out of region, no mobility support"),
        ("country CA out of scope", "country out of scope"),
        ("max EUR 40,000 below floor 90,000", "salary below floor"),
        ("JD contains dealbreaker 'polygraph'", "JD contains a dealbreaker phrase"),
        ("acme is on the blocked list", "company on the blocked list"),
        (None, "—"),
    ],
)
def test_reason_gist_categories(stored, expected: str) -> None:
    from shotgun.cli import _reason_gist
    assert _reason_gist(stored) == expected
