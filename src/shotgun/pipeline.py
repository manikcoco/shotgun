"""The run loop: discover -> score -> tailor -> fill -> queue."""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from . import apply as apply_mod
from . import db, render, score, tailor
from . import profile as profile_mod
from .config import Preferences
from .discover import ats_boards, jobspy_source, remote_boards, remotecom
from .geo import is_location_neutral
from .models import Job, Stage

log = logging.getLogger(__name__)


SOURCES = ["ats_boards", "remote_boards", "jobspy", "remotecom"]


def discover(prefs: Preferences, only: list[str] | None = None) -> dict[str, int]:
    """Fetch from every enabled source and store new postings.

    `only` restricts to named sources — useful for testing one adapter
    without waiting on the slow scrapers.
    """
    wanted = set(only or SOURCES)

    def ats_with_descriptions() -> list[Job]:
        """The ATS boards, with descriptions filled in where they were
        missing and the title rules say the posting is worth reading.

        Kept here rather than in the adapter because the title rules live in
        preferences and the adapter has no business knowing them.
        """
        jobs = ats_boards.discover(prefs.ats_boards)
        ats_boards.hydrate_descriptions(
            jobs,
            lambda job: prefs.title_matches(
                job.title, job.company, any_level=True)[0],
        )
        return jobs

    fetchers = [
        ("ats_boards", ats_with_descriptions),
        ("remote_boards", lambda: remote_boards.discover(prefs.remote_boards)),
        ("jobspy", lambda: jobspy_source.discover(prefs.jobspy, prefs.regions)),
        ("remotecom", lambda: remotecom.discover(prefs.remotecom)),
    ]

    stats = {"found": 0, "new": 0, "duplicates": 0, "errors": 0}

    # Stored as each source finishes, not once at the very end. The JobSpy
    # sweep runs for over an hour, and accumulating every source in memory
    # first meant an interrupted run — or a later source raising — threw away
    # everything already fetched. Three sweeps were lost that way before this
    # was noticed, because a source can succeed loudly in the log and still
    # never reach the database.
    for name, fetch in fetchers:
        if name not in wanted:
            continue
        try:
            jobs = fetch()
        except Exception:
            stats["errors"] += 1
            log.exception("source %s failed entirely", name)
            continue

        stats["found"] += len(jobs)
        stored = _store(jobs, stats)
        log.info("%s -> %d fetched, %d new (stored)", name, len(jobs), stored)

    return stats


@dataclass
class SweptRole:
    """One security opening found by a direct sweep."""
    company: str
    board: str
    token: str
    title: str
    location: str | None
    remote: bool
    neutral: bool
    url: str
    reason: str


@dataclass
class Sweep:
    """What a sweep found, per board, including the boards that answered
    nothing — a token that has gone stale is worth seeing."""
    roles: list[SweptRole] = field(default_factory=list)
    boards: int = 0
    postings: int = 0
    quiet: list[str] = field(default_factory=list)   # live, no security role
    failed: list[tuple[str, str]] = field(default_factory=list)
    tied_down: int = 0   # security roles dropped for naming a country/region


def sweep(
    prefs: Preferences,
    companies: list[str] | None = None,
    *,
    any_level: bool = False,
    neutral_only: bool = False,
    store: bool = False,
) -> Sweep:
    """Ask the ATS boards directly what security roles they have open.

    `discover` + `rank` answers this too, but only after storing thousands of
    postings and paying to score the survivors. This is the short question —
    which of these companies is hiring in security at all — and it costs a
    few seconds of HTTP and no model calls.

    `any_level` widens the title test to the any-level patterns for every
    company swept, not only the ones on the config's list. That is the
    difference between "what would the pipeline keep" and "what has this
    company actually got open", which is the question worth asking when the
    list of companies is one you chose deliberately.

    `neutral_only` keeps just the postings that are remote without naming a
    country or a region — see `geo.is_location_neutral`. Counted before it is
    applied, so the summary can say how many were dropped by it.
    """
    out = Sweep()
    results = ats_boards.fetch_boards(prefs.ats_boards, companies)

    for result in results:
        if result.error:
            out.failed.append((f"{result.board}:{result.token}", result.error))
            continue

        out.boards += 1
        out.postings += len(result.jobs)
        found = 0

        for job in result.jobs:
            ok, reason = prefs.title_matches(
                job.title, job.company, any_level=any_level
            )
            if not ok:
                continue
            found += 1
            neutral = is_location_neutral(job.location)
            if neutral_only and not neutral:
                out.tied_down += 1
                continue
            out.roles.append(SweptRole(
                company=job.company, board=result.board, token=result.token,
                title=job.title, location=job.location, remote=bool(job.remote),
                neutral=neutral, url=job.apply_url or job.url, reason=reason,
            ))

        if not found:
            out.quiet.append(f"{result.board}:{result.token}")

    out.roles.sort(key=lambda r: (r.company.lower(), r.title.lower()))

    if store:
        stats = {"found": 0, "new": 0, "duplicates": 0, "errors": 0}
        _store([j for r in results for j in r.jobs], stats)
        log.info("sweep stored: %d new, %d already known",
                 stats["new"], stats["duplicates"])

    return out


def candidate_tokens(conn: sqlite3.Connection, prefs: Preferences) -> list[str]:
    """Board tokens worth trying, from companies already seen on the boards.

    The aggregators are a company directory we already paid to fetch. A
    company advertising on Remotive, arbeitnow, WeWorkRemotely or LinkedIn is
    remote-friendly by definition — the Supabase and DuckDuckGo profile — and
    if it also runs a public ATS board, that board is higher signal than the
    aggregator listing ever was: full descriptions, real locations, a direct
    apply URL.

    Mining the stored apply URLs for tokens was the obvious approach and it
    does not work: every aggregator keeps the URL on its own domain
    (`linkedin.com/jobs/view/...`, `arbeitnow.com/jobs/...`), so the token is
    only revealed by a redirect discovery never follows. Across 26,000
    postings it yielded two tokens. The company *name* is the only handle
    there is.

    Already-configured tokens are excluded, so this returns work rather than
    a list to re-filter.
    """
    known = {
        str(token).strip().lower()
        for board in ats_boards.FETCHERS
        for token in (prefs.ats_boards.get(board) or [])
    }

    rows = conn.execute(
        """SELECT DISTINCT company FROM jobs
            WHERE source IN ('remotive','remoteok','weworkremotely','arbeitnow')
               OR source LIKE 'jobspy:%'"""
    ).fetchall()

    out: list[str] = []
    for row in rows:
        for variant in ats_boards.token_variants(row["company"]):
            if variant not in known and variant not in out:
                out.append(variant)
    return out


@dataclass
class FoundBoard:
    """A live board that was not in the config."""
    board: str
    token: str
    company: str
    postings: int
    security: int
    neutral: int
    titles: list[tuple[str, str]] = field(default_factory=list)


def probe_candidates(
    prefs: Preferences,
    candidates: list[str],
    *,
    max_workers: int = 24,
) -> list[FoundBoard]:
    """Try candidate tokens and report the live boards, ranked by usefulness.

    Ranked by security openings rather than by size, because a board with
    4,000 postings and none in security is worth less than one with three.
    """
    results = ats_boards.probe_tokens(candidates, max_workers=max_workers)

    found: list[FoundBoard] = []
    for result in results:
        security = [
            job for job in result.jobs
            if prefs.title_matches(job.title, job.company, any_level=True)[0]
        ]
        found.append(FoundBoard(
            board=result.board,
            token=result.token,
            company=result.company,
            postings=len(result.jobs),
            security=len(security),
            neutral=sum(1 for j in security if is_location_neutral(j.location)),
            titles=[(j.title, j.location or "?") for j in security[:4]],
        ))

    found.sort(key=lambda f: (-f.security, -f.neutral, -f.postings))
    return found


def _store(jobs, stats: dict) -> int:
    """Upsert a batch, committing per posting. Returns how many were new."""
    new = 0
    with db.connect() as conn:
        for job in jobs:
            try:
                job_id, is_new = db.upsert_job(conn, job)
                if is_new:
                    db.set_stage(conn, job_id, Stage.DISCOVERED)
                    new += 1
                    stats["new"] += 1
                else:
                    stats["duplicates"] += 1
                conn.commit()
            except Exception:
                conn.rollback()
                stats["errors"] += 1
                log.exception("storing %s @ %s failed", job.title, job.company)
    return new


def prepare_one(
    conn: sqlite3.Connection,
    job_id: int,
    prof,
    profile_yaml: str,
    *,
    dry_run: bool = False,
    documents_only: bool = False,
    reuse_docs: bool = False,
) -> dict:
    """Tailor, render, and fill one application. Never submits.

    Split out from the batch loop so a single job can be driven from the CLI
    (`shotgun prepare --job-id N`) or the web UI — which is the right way to
    test a new filler without burning ten applications on a bug.

    `reuse_docs` is what makes that claim true. Tailoring used to run
    unconditionally, so every attempt at a filler cost a resume and a cover
    letter even when perfectly good ones were already on disk. With it set,
    the documents already recorded for this application are reused and no
    model call is made at all.
    """
    job = db.job_row(conn, job_id)
    if job is None:
        return {"job_id": job_id, "status": "error", "detail": "no such job"}

    score_data = db.score_row(conn, job_id)
    label = f"{job['title']} @ {job['company']}"
    outcome: dict = {"job_id": job_id, "label": label}

    try:
        if reuse_docs:
            existing = db.application_detail(conn, job_id)
            resume = (existing["resume_path"] if existing else None) or ""
            if not resume or not Path(resume).exists():
                return outcome | {
                    "status": "error",
                    "detail": "no documents on disk to reuse — run without "
                              "--reuse-docs once to generate them",
                }
            pdf_path = Path(resume)
            html_path = pdf_path.with_suffix(".html")
            cover_file = existing["cover_path"]
            cover_body = (
                Path(cover_file).read_text()
                if cover_file and Path(cover_file).exists() else None
            )
            cover_path = Path(cover_file) if cover_file else None
            outcome |= {
                "resume": str(pdf_path),
                "cover": str(cover_path) if cover_path else None,
                "reused": True,
            }
            log.info("reusing existing documents for %s (no model calls)", label)
        else:
            db.set_stage(conn, job_id, Stage.TAILORING)

            tailored = tailor.tailor_resume(job, score_data, profile_yaml)
            report = tailor.verify_no_fabrication(tailored, prof)

            if not report.ok:
                # A bullet claiming an employer, technology or metric the
                # profile does not contain is a fabricated claim. Refuse it.
                note = "fabrication check failed: " + "; ".join(report.blockers)
                db.set_stage(conn, job_id, Stage.FAILED, note=note)
                return outcome | {"status": "blocked", "detail": note}

            if report.warnings:
                # Untraceable skill-list entries are usually rephrasings, so
                # they ride along to the approval queue rather than kill the job.
                db.log_event(
                    conn, kind="fabrication_warning",
                    detail="; ".join(report.warnings[:20]), job_id=job_id,
                )
                outcome["fabrication_warnings"] = report.warnings

            pdf_path, html_path = render.render_resume(
                prof, tailored, job["company"], job["title"]
            )
            cover = tailor.write_cover_letter(job, score_data, profile_yaml)
            cover_path = render.render_cover_letter(
                cover.body, prof.contact.name, job["company"], job["title"]
            )
            cover_body = cover.body
            outcome |= {
                "resume": str(pdf_path),
                "resume_html": str(html_path),
                "cover": str(cover_path),
                "omitted": tailored.omitted,
            }

        if dry_run or documents_only:
            db.set_stage(
                conn, job_id, Stage.QUEUED,
                note="documents generated; form not opened",
                resume_path=str(pdf_path),
                cover_path=str(cover_path) if cover_path else None,
            )
            return outcome | {"status": "documents_only"}

        result = apply_mod.prepare(
            job["apply_url"] or job["url"],
            prof, pdf_path, cover_body, _stored_answers(conn),
            questions_by_key=_answer_questions(conn),
        )

        if result.ok:
            db.set_stage(
                conn, job_id, Stage.AWAITING_APPROVAL, note=result.summary(),
                resume_path=str(pdf_path),
                cover_path=str(cover_path) if cover_path else None,
            )
            outcome |= {"status": "awaiting_approval", "detail": result.summary()}
        else:
            db.set_stage(
                conn, job_id, Stage.FAILED, note=result.error,
                resume_path=str(pdf_path),
                cover_path=str(cover_path) if cover_path else None,
            )
            outcome |= {"status": "fill_failed", "detail": result.error}

        if result.unanswered_questions:
            db.log_event(
                conn, kind="unanswered",
                detail="; ".join(result.unanswered_questions[:20]),
                job_id=job_id,
            )
            outcome["unanswered"] = result.unanswered_questions
        if result.screenshot:
            outcome["screenshot"] = str(result.screenshot)

    except Exception as exc:
        log.exception("preparing %s failed", label)
        db.set_stage(conn, job_id, Stage.FAILED, note=f"{type(exc).__name__}: {exc}")
        outcome |= {"status": "error", "detail": f"{type(exc).__name__}: {exc}"}

    return outcome


def prepare_queued(
    prefs: Preferences,
    *,
    limit: int | None = None,
    dry_run: bool = False,
    job_id: int | None = None,
    reuse_docs: bool = False,
) -> list[dict]:
    """Prepare the queue, highest-priority first.

    Ordering comes from the SQL in db.applications_by_stage: visa-sponsoring
    and relocation-supporting roles sort above the rest, so a small
    `max_per_run` is spent where it matters.
    """
    cap = limit if limit is not None else prefs.max_per_run
    prof = profile_mod.load()
    profile_yaml = profile_mod.load_yaml()

    with db.connect() as conn:
        if job_id is not None:
            outcome = prepare_one(conn, job_id, prof, profile_yaml,
                                  dry_run=dry_run, reuse_docs=reuse_docs)
            conn.commit()
            return [outcome]

        queued = db.applications_by_stage(conn, Stage.QUEUED)[:cap]
        outcomes = []
        for app in queued:
            outcomes.append(
                prepare_one(conn, app["job_id"], prof, profile_yaml, dry_run=dry_run)
            )
            # Commit after each application. These take minutes apiece — a
            # crash on the eighth must not erase the first seven, and holding
            # one write transaction across every browser session is what locks
            # the web UI out of the database.
            conn.commit()
        return outcomes


def _answer_questions(conn: sqlite3.Connection) -> dict[str, str]:
    """key -> the question it answers, for label-based matching."""
    rows = conn.execute("SELECT key, question FROM answers").fetchall()
    return {r["key"]: r["question"] for r in rows}


def _stored_answers(conn: sqlite3.Connection) -> dict[str, str]:
    """Non-sensitive confirmed answers, safe to auto-fill.

    Sensitive answers are deliberately excluded — storing them is one consent,
    replaying them into an arbitrary form is another.
    """
    rows = conn.execute(
        "SELECT key, value FROM answers WHERE sensitive = 0 AND confidence = 'confirmed'"
    ).fetchall()
    return {r["key"]: r["value"] for r in rows}


def run(prefs: Preferences, *, skip_discovery: bool = False, dry_run: bool = False) -> dict:
    """Full pipeline in one call."""
    report: dict = {}

    if not skip_discovery:
        report["discovery"] = discover(prefs)

    profile_yaml = profile_mod.load_yaml()
    with db.connect() as conn:
        report["scoring"] = score.score_pending(
            conn, prefs, profile_yaml, profile=profile_mod.load()
        )

    report["prepared"] = prepare_queued(prefs, dry_run=dry_run)
    return report
