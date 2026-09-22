"""The standing report, and the dedupe that makes a recurring sweep readable.

The value of running this every few weeks is that the second run tells you
what moved rather than repeating nine hundred roles, so the tests that matter
are about identity and change detection, not formatting.
"""

from __future__ import annotations

import json

import pytest

from shotgun import db, report
from shotgun.models import Job, Stage


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(db, "db_path", lambda: tmp_path / "test.db")
    db.init()
    with db.connect() as c:
        yield c


def add(conn, *, company="Acme", title="Security Engineer", country="DE", **over):
    job = Job(source=over.pop("source", "ats"), company=company, title=title,
              url=over.pop("url", "https://example.com/1"), country=country, **over)
    job_id, _ = db.upsert_job(conn, job)
    db.set_stage(conn, job_id, Stage.QUEUED)
    return job_id


def test_collect_reports_open_roles(conn) -> None:
    add(conn, title="Security Engineer")
    add(conn, title="Detection Engineer")
    assert {r["title"] for r in report.collect(conn)} == {
        "Security Engineer", "Detection Engineer"}


def test_filtered_out_roles_are_not_reported(conn) -> None:
    job_id = add(conn)
    db.set_stage(conn, job_id, Stage.FILTERED_OUT)
    assert report.collect(conn) == []


# ----------------------------------------------------------------- identity

def test_identity_survives_a_repost_under_a_new_url(conn) -> None:
    """Keyed on company+title+country, not the URL, which changes on repost."""
    add(conn, url="https://board/one")
    before = report.collect(conn)[0]["id"]
    conn.execute("UPDATE jobs SET url = ?", ("https://board/reposted",))
    assert report.collect(conn)[0]["id"] == before


def test_country_is_part_of_the_report_key() -> None:
    """Job.fingerprint is company+title; the report needs country too, because
    the same role in two countries is two entries to read."""
    assert (report.role_key("Acme", "Security Engineer", "DE")
            != report.role_key("Acme", "Security Engineer", "IE"))


def test_the_key_is_insensitive_to_case_and_padding() -> None:
    assert (report.role_key(" Acme ", "Security Engineer", "de")
            == report.role_key("acme", " security engineer ", "DE"))


# ------------------------------------------------------------------ diffing

def test_first_run_is_all_new(conn) -> None:
    add(conn)
    assert report.diff(report.collect(conn), {}).counts == {
        "new": 1, "changed": 0, "unchanged": 0, "closed": 0}


def test_a_second_run_with_no_movement_reports_nothing_new(conn) -> None:
    """The point of the whole exercise."""
    add(conn)
    current = report.collect(conn)
    changes = report.diff(current, {r["id"]: r for r in current})
    assert changes.counts == {"new": 0, "changed": 0, "unchanged": 1, "closed": 0}


def test_a_reworded_description_is_not_a_change(conn) -> None:
    """Boards reword constantly; that is not a new job and must not be noise."""
    add(conn, description="original")
    previous = {r["id"]: r for r in report.collect(conn)}
    conn.execute("UPDATE jobs SET description = ?", ("rewritten entirely",))
    changes = report.diff(report.collect(conn), previous)
    assert changes.counts["unchanged"] == 1
    assert changes.counts["changed"] == 0


def test_a_moved_role_is_changed_and_records_the_old_value(conn) -> None:
    add(conn, location="Berlin")
    previous = {r["id"]: r for r in report.collect(conn)}
    conn.execute("UPDATE jobs SET location = ?", ("Munich",))
    changes = report.diff(report.collect(conn), previous)
    assert changes.counts["changed"] == 1
    assert changes.changed[0]["_was"] == {"location": "Berlin"}


def test_a_vanished_role_is_reported_closed(conn) -> None:
    add(conn)
    previous = {r["id"]: r for r in report.collect(conn)}
    conn.execute("DELETE FROM jobs")
    assert report.diff(report.collect(conn), previous).counts["closed"] == 1


# ------------------------------------------------------------------ writing

def test_first_seen_survives_across_runs(conn, tmp_path) -> None:
    """A role open for a month must not claim to be new today."""
    add(conn)
    path = tmp_path / "r.json"
    changes, roles = report.build(conn, path=path)
    roles[0]["first_seen"] = "2026-01-01"
    report.write_json(roles, changes, path)

    _, again = report.build(conn, path=path)
    assert again[0]["first_seen"] == "2026-01-01"
    assert again[0]["last_seen"] != "2026-01-01"


def test_a_corrupt_previous_report_does_not_abort_the_run(tmp_path) -> None:
    bad = tmp_path / "r.json"
    bad.write_text("{not json at all")
    assert report.load_previous(bad) == {}


def test_a_missing_previous_report_reads_as_empty(tmp_path) -> None:
    assert report.load_previous(tmp_path / "nope.json") == {}


def test_both_files_are_written(conn, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(report, "JSON_PATH", tmp_path / "s.json")
    monkeypatch.setattr(report, "MD_PATH", tmp_path / "s.md")
    add(conn, company="Canonical")
    changes, paths = report.generate(conn)

    payload = json.loads(paths["json"].read_text())
    assert payload["counts"]["total_open"] == 1
    assert payload["roles"][0]["company"] == "Canonical"

    md = paths["markdown"].read_text()
    assert "# Open security roles" in md
    assert "Canonical" in md
    assert changes.counts["new"] == 1


def test_writing_honours_a_reassigned_module_path(conn, tmp_path, monkeypatch) -> None:
    """Regression: the output paths were module-level default arguments, which
    bind at definition time — so reassigning JSON_PATH was silently ignored and
    the real report was overwritten. Caught when a test run clobbered it.

    Asserts the real file is *untouched* rather than absent: it legitimately
    exists once you have run `shotgun report`, and an absence check would pass
    only on machines that never had.
    """
    real = report.REPORT_DIR / "security-roles.json"
    before = real.read_bytes() if real.exists() else None

    monkeypatch.setattr(report, "JSON_PATH", tmp_path / "s.json")
    monkeypatch.setattr(report, "MD_PATH", tmp_path / "s.md")
    add(conn)
    report.generate(conn)

    assert (tmp_path / "s.json").exists()
    after = real.read_bytes() if real.exists() else None
    assert after == before
