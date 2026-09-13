"""SQLite store. Everything lives in data/shotgun.db (gitignored)."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from .config import DATA_DIR
from .models import Job, Stage

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id              INTEGER PRIMARY KEY,
    fingerprint     TEXT NOT NULL UNIQUE,
    source          TEXT NOT NULL,
    source_id       TEXT,
    company         TEXT NOT NULL,
    title           TEXT NOT NULL,
    url             TEXT NOT NULL,
    apply_url       TEXT,
    ats             TEXT,
    location        TEXT,
    country         TEXT,
    remote          INTEGER NOT NULL DEFAULT 0,
    description     TEXT,
    salary_min      REAL,
    salary_max      REAL,
    salary_currency TEXT,
    posted_at       TEXT,
    discovered_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS scores (
    job_id           INTEGER PRIMARY KEY REFERENCES jobs(id) ON DELETE CASCADE,
    score            INTEGER,
    level            TEXT,
    reasoning        TEXT,
    dealbreakers     TEXT,   -- json array: disqualifying only
    concerns         TEXT,   -- json array: friction, does not disqualify
    key_requirements TEXT,   -- json array
    rule_reason      TEXT,
    -- Mobility: what makes an out-of-region role worth applying to.
    visa_sponsorship   TEXT DEFAULT 'unknown',
    relocation_support TEXT DEFAULT 'unknown',
    employment_type    TEXT DEFAULT 'unknown',
    hiring_countries   TEXT,   -- json array
    -- score adjusted by the mobility weights; the apply queue sorts on this
    priority         INTEGER,
    scored_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS applications (
    id           INTEGER PRIMARY KEY,
    job_id       INTEGER NOT NULL UNIQUE REFERENCES jobs(id) ON DELETE CASCADE,
    stage        TEXT NOT NULL,
    resume_path  TEXT,
    cover_path   TEXT,
    filled_at    TEXT,
    submitted_at TEXT,
    notes        TEXT,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id             INTEGER PRIMARY KEY,
    application_id INTEGER REFERENCES applications(id) ON DELETE CASCADE,
    job_id         INTEGER REFERENCES jobs(id) ON DELETE CASCADE,
    kind           TEXT NOT NULL,
    detail         TEXT,
    at             TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS answers (
    key        TEXT PRIMARY KEY,
    question   TEXT NOT NULL,
    value      TEXT NOT NULL,
    confidence TEXT NOT NULL DEFAULT 'confirmed',
    sensitive  INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_jobs_company   ON jobs(company);
CREATE INDEX IF NOT EXISTS idx_apps_stage     ON applications(stage);
CREATE INDEX IF NOT EXISTS idx_events_job     ON events(job_id);
"""


def db_path() -> Path:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    return DATA_DIR / "shotgun.db"


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(db_path())
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


# Columns added after the first schema shipped. Applied on every init so an
# existing data/shotgun.db picks them up without a manual migration.
_ADDED_COLUMNS = [
    ("scores", "concerns", "TEXT"),
    ("scores", "visa_sponsorship", "TEXT DEFAULT 'unknown'"),
    ("scores", "relocation_support", "TEXT DEFAULT 'unknown'"),
    ("scores", "employment_type", "TEXT DEFAULT 'unknown'"),
    ("scores", "hiring_countries", "TEXT"),
    ("scores", "priority", "INTEGER"),
]


def init() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)
        for table, column, decl in _ADDED_COLUMNS:
            existing = {
                r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()
            }
            if column not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


# ------------------------------------------------------------------ jobs

def upsert_job(conn: sqlite3.Connection, job: Job) -> tuple[int, bool]:
    """Insert a job, or return the existing row. Returns (job_id, is_new)."""
    existing = conn.execute(
        "SELECT id FROM jobs WHERE fingerprint = ?", (job.fingerprint,)
    ).fetchone()
    if existing:
        # Backfill a description or apply_url if this source has one and we didn't.
        # COALESCE, so a source that carries a field we're missing fills it in
        # without a source that lacks it wiping what we have.
        #
        # Salary was previously left out, which hid a fix: when the Ashby
        # fetcher started reading compensation, only the 20 genuinely new rows
        # picked it up — 6,941 known rows kept their NULLs and the comp floor
        # stayed inert. Location is backfilled only when NULL for a different
        # reason: two postings sharing a fingerprint but naming *different*
        # locations is the 427-row merge problem, and picking a winner here
        # would just swap which one is lost. That needs the fingerprint to
        # include location; see the README.
        conn.execute(
            """UPDATE jobs
                  SET description     = COALESCE(description, ?),
                      apply_url       = COALESCE(apply_url, ?),
                      ats             = COALESCE(ats, ?),
                      location        = COALESCE(location, ?),
                      country         = COALESCE(country, ?),
                      salary_min      = COALESCE(salary_min, ?),
                      salary_max      = COALESCE(salary_max, ?),
                      salary_currency = COALESCE(salary_currency, ?),
                      posted_at       = COALESCE(posted_at, ?)
                WHERE id = ?""",
            (job.description, job.apply_url, job.ats, job.location, job.country,
             job.salary_min, job.salary_max, job.salary_currency, job.posted_at,
             existing["id"]),
        )
        return existing["id"], False

    cur = conn.execute(
        """INSERT INTO jobs (fingerprint, source, source_id, company, title, url,
                             apply_url, ats, location, country, remote, description,
                             salary_min, salary_max, salary_currency, posted_at,
                             discovered_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            job.fingerprint, job.source, job.source_id, job.company, job.title,
            job.url, job.apply_url, job.ats, job.location, job.country,
            int(job.remote), job.description, job.salary_min, job.salary_max,
            job.salary_currency, job.posted_at, job.discovered_at,
        ),
    )
    return int(cur.lastrowid), True


def job_row(conn: sqlite3.Connection, job_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()


def unscored_jobs(
    conn: sqlite3.Connection,
    limit: int = 200,
    *,
    include_rule_filtered: bool = False,
) -> list[sqlite3.Row]:
    """Jobs still waiting on the ranker.

    `include_rule_filtered` also returns jobs whose only score row came from
    the rule filter (`score IS NULL`) — the ones rejected on title, location or
    comp without ever reaching the model. Those are invisible to the default
    query, so widening `titles.include` in preferences could never reach the
    postings it was widened *for*: they keep the rejection recorded under the
    old rules forever. Jobs the model has actually scored are never returned,
    so a refilter pass cannot re-spend on them.
    """
    where = (
        "s.job_id IS NULL OR s.score IS NULL"
        if include_rule_filtered
        else "s.job_id IS NULL"
    )
    return conn.execute(
        f"""SELECT j.* FROM jobs j
              LEFT JOIN scores s ON s.job_id = j.id
             WHERE {where}
             ORDER BY j.discovered_at DESC
             LIMIT ?""",
        (limit,),
    ).fetchall()


# ---------------------------------------------------------------- scores

def save_score(
    conn: sqlite3.Connection,
    job_id: int,
    *,
    score: int | None,
    level: str | None,
    reasoning: str | None,
    dealbreakers: list[str] | None = None,
    concerns: list[str] | None = None,
    key_requirements: list[str] | None = None,
    rule_reason: str | None = None,
    visa_sponsorship: str = "unknown",
    relocation_support: str = "unknown",
    employment_type: str = "unknown",
    hiring_countries: list[str] | None = None,
    priority: int | None = None,
) -> None:
    conn.execute(
        """INSERT INTO scores (job_id, score, level, reasoning, dealbreakers,
                               concerns, key_requirements, rule_reason,
                               visa_sponsorship, relocation_support,
                               employment_type, hiring_countries, priority,
                               scored_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(job_id) DO UPDATE SET
               score=excluded.score, level=excluded.level,
               reasoning=excluded.reasoning, dealbreakers=excluded.dealbreakers,
               concerns=excluded.concerns,
               key_requirements=excluded.key_requirements,
               rule_reason=excluded.rule_reason,
               visa_sponsorship=excluded.visa_sponsorship,
               relocation_support=excluded.relocation_support,
               employment_type=excluded.employment_type,
               hiring_countries=excluded.hiring_countries,
               priority=excluded.priority, scored_at=excluded.scored_at""",
        (
            job_id, score, level, reasoning,
            json.dumps(dealbreakers or []), json.dumps(concerns or []),
            json.dumps(key_requirements or []),
            rule_reason, visa_sponsorship, relocation_support, employment_type,
            json.dumps(hiring_countries or []), priority, _now(),
        ),
    )


def score_row(conn: sqlite3.Connection, job_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM scores WHERE job_id = ?", (job_id,)).fetchone()


# ---------------------------------------------------------- applications

def set_stage(
    conn: sqlite3.Connection,
    job_id: int,
    stage: Stage,
    *,
    note: str | None = None,
    resume_path: str | None = None,
    cover_path: str | None = None,
) -> int:
    row = conn.execute(
        "SELECT id FROM applications WHERE job_id = ?", (job_id,)
    ).fetchone()

    if row:
        app_id = row["id"]
        conn.execute(
            """UPDATE applications
                  SET stage = ?,
                      resume_path = COALESCE(?, resume_path),
                      cover_path  = COALESCE(?, cover_path),
                      notes       = COALESCE(?, notes),
                      filled_at   = CASE WHEN ? = 'awaiting_approval'
                                         THEN ? ELSE filled_at END,
                      submitted_at = CASE WHEN ? = 'submitted'
                                          THEN ? ELSE submitted_at END,
                      updated_at  = ?
                WHERE id = ?""",
            (str(stage), resume_path, cover_path, note, str(stage), _now(),
             str(stage), _now(), _now(), app_id),
        )
    else:
        # The milestone timestamps have to be stamped here too, not only on
        # the update path. A job whose *first* recorded stage is submitted —
        # `shotgun mark <id> submitted` on a row discovery never reached —
        # otherwise ends up submitted with submitted_at NULL, and the tracking
        # loses the one date you actually want to follow up against.
        cur = conn.execute(
            """INSERT INTO applications (job_id, stage, resume_path, cover_path,
                                         notes, filled_at, submitted_at,
                                         created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                job_id, str(stage), resume_path, cover_path, note,
                _now() if stage is Stage.AWAITING_APPROVAL else None,
                _now() if stage is Stage.SUBMITTED else None,
                _now(), _now(),
            ),
        )
        app_id = int(cur.lastrowid)

    log_event(conn, kind=f"stage:{stage}", detail=note, job_id=job_id, application_id=app_id)
    return app_id


# Shared projection so the CLI and the web UI show the same fields.
_APP_COLUMNS = """
    a.*, j.company, j.title, j.url, j.apply_url, j.ats, j.location, j.remote,
    j.description, j.salary_min, j.salary_max, j.salary_currency, j.posted_at,
    s.score, s.level, s.reasoning, s.key_requirements, s.dealbreakers, s.concerns,
    s.rule_reason, s.visa_sponsorship, s.relocation_support,
    s.employment_type, s.hiring_countries, s.priority
"""

# "Apply to visa/relocation roles first", in SQL — but expressed through
# `priority` alone, which is what actually encodes it.
#
# This used to lead with `(s.visa_sponsorship = 'yes') DESC` and
# `(s.relocation_support = 'yes') DESC`. Both are redundant, because
# visa.priority() already adds the sponsorship, relocation and contract
# bonuses into `priority` — and as leading keys they overrode score entirely.
# Since visa.analyse() reads the whole job description, any posting carrying
# sponsorship boilerplate got visa='yes' whether or not the role was ever
# relevant: "Enterprise Account Executive, Brazil" and a role scoring 3 both
# sorted above an 84-scoring security role.
#
# Ordering on priority keeps the intent and the arithmetic honest: a
# visa-sponsoring 70 (priority 95) still beats a silent 84, while a score-3
# posting sinks to where it belongs. Unscored rows sort last so the rule
# filter's leavings never lead a listing.
_APP_ORDER = """
    ORDER BY (s.score IS NULL) ASC,
             s.priority DESC NULLS LAST,
             s.score DESC NULLS LAST,
             a.updated_at DESC
"""


def applications_by_stage(conn: sqlite3.Connection, stage: Stage) -> list[sqlite3.Row]:
    return conn.execute(
        f"""SELECT {_APP_COLUMNS}
             FROM applications a
             JOIN jobs j   ON j.id = a.job_id
             LEFT JOIN scores s ON s.job_id = a.job_id
            WHERE a.stage = ?
            {_APP_ORDER}""",
        (str(stage),),
    ).fetchall()


def all_applications(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        f"""SELECT {_APP_COLUMNS}
             FROM applications a
             JOIN jobs j ON j.id = a.job_id
             LEFT JOIN scores s ON s.job_id = a.job_id
            {_APP_ORDER}"""
    ).fetchall()


def application_detail(conn: sqlite3.Connection, job_id: int) -> sqlite3.Row | None:
    return conn.execute(
        f"""SELECT {_APP_COLUMNS}
             FROM applications a
             JOIN jobs j ON j.id = a.job_id
             LEFT JOIN scores s ON s.job_id = a.job_id
            WHERE a.job_id = ?""",
        (job_id,),
    ).fetchone()


def events_for(conn: sqlite3.Connection, job_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM events WHERE job_id = ? ORDER BY at DESC, id DESC LIMIT 60",
        (job_id,),
    ).fetchall()


def stage_counts(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute(
        "SELECT stage, COUNT(*) AS n FROM applications GROUP BY stage"
    ).fetchall()
    return {r["stage"]: r["n"] for r in rows}


# ---------------------------------------------------------------- events

def log_event(
    conn: sqlite3.Connection,
    *,
    kind: str,
    detail: str | None = None,
    job_id: int | None = None,
    application_id: int | None = None,
) -> None:
    conn.execute(
        "INSERT INTO events (application_id, job_id, kind, detail, at) VALUES (?,?,?,?,?)",
        (application_id, job_id, kind, detail, _now()),
    )


# --------------------------------------------------------------- answers

def get_answer(conn: sqlite3.Connection, key: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM answers WHERE key = ?", (key,)).fetchone()


def list_answers(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM answers ORDER BY key").fetchall()


def delete_answer(conn: sqlite3.Connection, key: str) -> bool:
    cur = conn.execute("DELETE FROM answers WHERE key = ?", (key,))
    return cur.rowcount > 0


def put_answer(
    conn: sqlite3.Connection,
    key: str,
    question: str,
    value: str,
    *,
    confidence: str = "confirmed",
    sensitive: bool = False,
) -> None:
    conn.execute(
        """INSERT INTO answers (key, question, value, confidence, sensitive, updated_at)
           VALUES (?,?,?,?,?,?)
           ON CONFLICT(key) DO UPDATE SET
               value=excluded.value, confidence=excluded.confidence,
               sensitive=excluded.sensitive, updated_at=excluded.updated_at""",
        (key, question, value, confidence, int(sensitive), _now()),
    )
