"""The standing list of companies and open security roles.

Written for a sweep run every few weeks. The point of the output is not "here
is everything" — `shotgun list` already does that — it is "here is what
changed since you last looked", so a run that surfaces four new roles costs
four lines to read rather than nine hundred.

Two files, same data:

    reports/security-roles.json   the record, and what the next run diffs against
    reports/security-roles.md     the one you read

State lives in the JSON rather than the database, so the report survives a
rebuilt database and can be committed and diffed in git.

How a role is identified
------------------------
By company + title + country, hashed. Deliberately *not* the job id, which is
per-database, and not the URL, which changes whenever a board reposts the same
role — either would report a role you have already seen as new.

Note this is a different key from `Job.fingerprint`, which is company + title
only. The report needs country in the key for a reason the job table does not:
two postings for the same role in two countries are one row in the database
but are genuinely two entries to read here, and collapsing them would hide
whichever arrived second.

What counts as changed
----------------------
Only the fields worth re-reading: title, company, country, location, URL,
remote flag, salary. A posting whose description was reworded is *unchanged*
and stays out of the "new since" section — boards reword constantly and it is
not a new job. Everything still open is carried forward, so the file is always
the complete picture rather than a changelog.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from .config import REPO_ROOT

REPORT_DIR = REPO_ROOT / "reports"
JSON_PATH = REPORT_DIR / "security-roles.json"
MD_PATH = REPORT_DIR / "security-roles.md"

# A change in any of these is worth telling you about. Description is absent
# on purpose; see the module docstring.
TRACKED = ("title", "company", "country", "location", "url", "remote", "salary")


def role_key(company: str, title: str, country: str | None) -> str:
    return hashlib.sha256(
        "|".join((company.strip().lower(), title.strip().lower(),
                  (country or "").strip().upper())).encode()
    ).hexdigest()[:32]


@dataclass
class Diff:
    new: list[dict] = field(default_factory=list)
    changed: list[dict] = field(default_factory=list)
    unchanged: list[dict] = field(default_factory=list)
    closed: list[dict] = field(default_factory=list)

    @property
    def counts(self) -> dict[str, int]:
        return {
            "new": len(self.new), "changed": len(self.changed),
            "unchanged": len(self.unchanged), "closed": len(self.closed),
        }


def _salary(row: sqlite3.Row) -> str | None:
    low, high, cur = row["salary_min"], row["salary_max"], row["salary_currency"]
    if not (low or high):
        return None
    cur = cur or ""
    if low and high:
        return f"{cur} {low:,.0f}–{high:,.0f}".strip()
    return f"{cur} {(low or high):,.0f}".strip()


def collect(conn: sqlite3.Connection,
            *, stages: tuple[str, ...] = ("queued",)) -> list[dict]:
    """Every open role, one record each.

    Reads application stages rather than re-running the title rules, so the
    report cannot disagree with what `shotgun queue` shows — one filter, one
    answer.
    """
    marks = ",".join("?" * len(stages))
    rows = conn.execute(
        f"""SELECT j.company, j.title, j.location, j.country, j.remote,
                   j.url, j.apply_url, j.ats, j.source, j.posted_at,
                   j.salary_min, j.salary_max, j.salary_currency,
                   s.score, s.level
              FROM jobs j
              JOIN applications a ON a.job_id = j.id
         LEFT JOIN scores s       ON s.job_id = j.id
             WHERE a.stage IN ({marks})
          ORDER BY j.company COLLATE NOCASE, j.title""",
        stages,
    ).fetchall()

    return [{
        "id": role_key(r["company"], r["title"], r["country"]),
        "company": r["company"],
        "title": r["title"],
        "location": r["location"],
        "country": r["country"],
        "remote": bool(r["remote"]),
        "url": r["apply_url"] or r["url"],
        "ats": r["ats"],
        "source": (r["source"] or "").split(":")[0],
        "salary": _salary(r),
        "posted_at": r["posted_at"],
        "score": r["score"],
        "level": r["level"],
    } for r in rows]


def load_previous(path: Path | None = None) -> dict[str, dict]:
    """Prior roles keyed by role id. Missing or corrupt file means empty.

    A corrupt file must not abort the run: the report is derivable from the
    database, so the worst case of ignoring it is one noisy run where
    everything reads as new.
    """
    # Resolved at call time rather than bound as a default argument: a
    # module-level default freezes the path when the function is *defined*, so
    # any later reassignment of JSON_PATH — a test, or a caller redirecting
    # output — is silently ignored and the real report gets overwritten.
    path = path or JSON_PATH
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}
    return {r["id"]: r for r in payload.get("roles", []) if r.get("id")}


def diff(current: list[dict], previous: dict[str, dict]) -> Diff:
    result = Diff()
    seen: set[str] = set()

    for role in current:
        seen.add(role["id"])
        was = previous.get(role["id"])
        if was is None:
            result.new.append(role)
        elif any(was.get(f) != role.get(f) for f in TRACKED):
            result.changed.append(role | {
                "_was": {f: was.get(f) for f in TRACKED if was.get(f) != role.get(f)}
            })
        else:
            result.unchanged.append(role)

    result.closed = [was for fid, was in previous.items() if fid not in seen]
    return result


def build(conn: sqlite3.Connection,
          *, path: Path | None = None) -> tuple[Diff, list[dict]]:
    """Diff the database against the last report. Writes nothing."""
    current = collect(conn)
    previous = load_previous(path)
    changes = diff(current, previous)

    today = date.today().isoformat()
    for role in current:
        role["first_seen"] = (previous.get(role["id"]) or {}).get("first_seen") or today
        role["last_seen"] = today
    return changes, current


def write_json(roles: list[dict], changes: Diff, path: Path | None = None) -> Path:
    path = path or JSON_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "counts": changes.counts | {"total_open": len(roles)},
        "new_this_run": [r["id"] for r in changes.new],
        "roles": sorted(roles, key=lambda r: (r["company"].lower(), r["title"])),
    }, indent=2, ensure_ascii=False) + "\n")
    return path


HEAD = ("| Company | Role | Location | Remote | Salary | First seen |\n"
        "|---|---|---|---|---|---|")


def _row(role: dict) -> str:
    where = role.get("location") or role.get("country") or "—"
    link = f"[{role['title']}]({role['url']})" if role.get("url") else role["title"]
    return (f"| {role['company']} | {link} | {where} | "
            f"{'✅' if role.get('remote') else ''} | {role.get('salary') or ''} | "
            f"{role.get('first_seen', '')} |")


def write_markdown(roles: list[dict], changes: Diff, path: Path | None = None) -> Path:
    path = path or MD_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    c = changes.counts

    by_company: dict[str, list[dict]] = {}
    for r in roles:
        by_company.setdefault(r["company"], []).append(r)

    lines = [
        "# Open security roles",
        "",
        f"_Generated {datetime.now():%Y-%m-%d %H:%M}_ · "
        f"**{len(roles)} open** across **{len(by_company)} companies** · "
        f"{c['new']} new · {c['changed']} changed · {c['closed']} closed since last run",
        "",
        "Produced by `shotgun report`. What counts as a security role, and what "
        "is filtered out, is the title configuration in "
        "`config/preferences.yaml`.",
        "",
    ]

    if changes.new:
        lines += [f"## 🆕 New since last run ({len(changes.new)})", "", HEAD]
        lines += [_row(r) for r in sorted(changes.new,
                                          key=lambda r: (r["company"].lower(), r["title"]))]
        lines += [""]

    if changes.changed:
        lines += [f"## ✏️ Changed ({len(changes.changed)})", "", HEAD]
        for r in sorted(changes.changed, key=lambda r: r["company"].lower()):
            lines.append(_row(r))
            was = ", ".join(f"{k}: {v!r} → {r.get(k)!r}" for k, v in r["_was"].items())
            lines.append(f"| | _{was}_ | | | | |")
        lines += [""]

    if changes.closed:
        lines += [f"## ⛔ No longer listed ({len(changes.closed)})", ""]
        lines += [f"- {r['company']} — {r['title']}"
                  for r in sorted(changes.closed, key=lambda r: r["company"].lower())]
        lines += [""]

    lines += [f"## All open roles ({len(roles)})", ""]
    for company in sorted(by_company, key=str.lower):
        items = by_company[company]
        lines += [f"### {company} ({len(items)})", "", HEAD]
        lines += [_row(r) for r in sorted(items, key=lambda r: r["title"])]
        lines += [""]

    path.write_text("\n".join(lines))
    return path


def generate(conn: sqlite3.Connection) -> tuple[Diff, dict[str, Path]]:
    """Build and write both files. Returns the diff and where they went."""
    changes, roles = build(conn)
    return changes, {
        "json": write_json(roles, changes),
        "markdown": write_markdown(roles, changes),
    }
