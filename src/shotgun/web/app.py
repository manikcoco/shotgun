"""Local web UI for reviewing and tracking applications.

Binds to 127.0.0.1 only. There is no auth, because there is no network
surface — this is a single-user tool reading a local SQLite file that contains
your phone number and salary expectations. Do not expose it.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .. import db, pipeline
from .. import profile as profile_mod
from ..config import PRIVATE_DIR, Preferences
from ..models import Stage

log = logging.getLogger(__name__)

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

app = FastAPI(title="shotgun", docs_url=None, redoc_url=None)


# Stage -> status role from the palette. Status colours are reserved for
# state and always ship with an icon and a label, never colour alone.
STAGE_STATUS = {
    Stage.OFFER: ("good", "★"),
    Stage.INTERVIEW: ("good", "◆"),
    Stage.SCREENING: ("good", "◇"),
    Stage.SUBMITTED: ("good", "✓"),
    Stage.ACKNOWLEDGED: ("good", "✓"),
    Stage.AWAITING_APPROVAL: ("warning", "!"),
    Stage.TAILORING: ("warning", "…"),
    Stage.QUEUED: ("neutral", "•"),
    Stage.DISCOVERED: ("neutral", "•"),
    Stage.SCORED_LOW: ("muted", "↓"),
    Stage.FILTERED_OUT: ("muted", "–"),
    Stage.REJECTED: ("serious", "✗"),
    Stage.WITHDRAWN: ("muted", "✗"),
    Stage.FAILED: ("critical", "⚠"),
}

# The stages worth showing as tiles, in pipeline order.
TILE_STAGES = [
    Stage.QUEUED,
    Stage.AWAITING_APPROVAL,
    Stage.SUBMITTED,
    Stage.INTERVIEW,
    Stage.OFFER,
    Stage.REJECTED,
    Stage.FAILED,
]


def _status_for(stage: str) -> tuple[str, str]:
    try:
        return STAGE_STATUS.get(Stage(stage), ("neutral", "•"))
    except ValueError:
        return ("neutral", "•")


def _visa_badge(value: str | None) -> tuple[str, str, str]:
    """(status role, icon, label) for a visa/relocation value."""
    return {
        "yes": ("good", "✓", "yes"),
        "no": ("critical", "✗", "no"),
    }.get(value or "unknown", ("muted", "?", "unknown"))


def _json_list(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, list) else []
    except json.JSONDecodeError:
        return []


def _row_to_view(row) -> dict:
    status, icon = _status_for(row["stage"])
    return {
        "job_id": row["job_id"],
        "stage": row["stage"],
        "status": status,
        "icon": icon,
        "title": row["title"],
        "company": row["company"],
        "location": row["location"] or "—",
        "score": row["score"],
        "priority": row["priority"],
        "level": row["level"] or "—",
        "ats": row["ats"] or "unknown",
        "url": row["url"],
        "apply_url": row["apply_url"] or row["url"],
        "updated_at": (row["updated_at"] or "")[:16].replace("T", " "),
        "visa": _visa_badge(row["visa_sponsorship"]),
        "reloc": _visa_badge(row["relocation_support"]),
        "employment": row["employment_type"] or "unknown",
        "resume_path": row["resume_path"],
        "cover_path": row["cover_path"],
    }


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, stage: str | None = None, mobility: str | None = None):
    with db.connect() as conn:
        counts = db.stage_counts(conn)
        if stage:
            try:
                rows = db.applications_by_stage(conn, Stage(stage))
            except ValueError:
                raise HTTPException(400, f"unknown stage {stage!r}") from None
        else:
            rows = db.all_applications(conn)

    views = [_row_to_view(r) for r in rows]

    if mobility == "1":
        views = [v for v in views if v["visa"][2] == "yes" or v["reloc"][2] == "yes"]

    tiles = [
        {
            "label": str(s).replace("_", " "),
            "value": counts.get(str(s), 0),
            "status": _status_for(str(s))[0],
            "stage": str(s),
        }
        for s in TILE_STAGES
    ]

    return TEMPLATES.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "tiles": tiles,
            "rows": views,
            "total": sum(counts.values()),
            "active_stage": stage,
            "mobility_only": mobility == "1",
            "stages": [str(s) for s in Stage],
        },
    )


@app.get("/job/{job_id}", response_class=HTMLResponse)
def detail(request: Request, job_id: int):
    with db.connect() as conn:
        row = db.application_detail(conn, job_id)
        if row is None:
            raise HTTPException(404, "no such application")
        events = db.events_for(conn, job_id)

    view = _row_to_view(row)
    view |= {
        "description": row["description"] or "",
        "reasoning": row["reasoning"] or "",
        "rule_reason": row["rule_reason"] or "",
        "key_requirements": _json_list(row["key_requirements"]),
        "dealbreakers": _json_list(row["dealbreakers"]),
        "concerns": _json_list(row["concerns"]),
        "hiring_countries": _json_list(row["hiring_countries"]),
        "salary": _salary(row),
        "posted_at": row["posted_at"] or "—",
        "remote": bool(row["remote"]),
    }

    cover_text = ""
    if row["cover_path"] and Path(row["cover_path"]).exists():
        cover_text = Path(row["cover_path"]).read_text()

    return TEMPLATES.TemplateResponse(
        request=request,
        name="detail.html",
        context={
            "app": view,
            "events": [
                {
                    "kind": e["kind"],
                    "detail": e["detail"] or "",
                    "at": (e["at"] or "")[:16].replace("T", " "),
                }
                for e in events
            ],
            "cover_text": cover_text,
            "stages": [str(s) for s in Stage],
        },
    )


def _salary(row) -> str:
    low, high, cur = row["salary_min"], row["salary_max"], row["salary_currency"]
    if not (low or high):
        return "not published"
    cur = cur or ""
    if low and high:
        return f"{cur} {low:,.0f} – {high:,.0f}"
    return f"{cur} {(low or high):,.0f}"


@app.post("/job/{job_id}/stage")
def update_stage(job_id: int, stage: str = Form(...), note: str = Form("")):
    try:
        target = Stage(stage)
    except ValueError:
        raise HTTPException(400, f"unknown stage {stage!r}") from None

    with db.connect() as conn:
        if db.job_row(conn, job_id) is None:
            raise HTTPException(404, "no such job")
        db.set_stage(conn, job_id, target, note=note or "changed from web UI")

    return RedirectResponse(f"/job/{job_id}", status_code=303)


@app.post("/job/{job_id}/prepare")
def prepare(job_id: int, documents_only: str = Form("")):
    """Tailor + render (+ optionally open and fill the form) for one job."""
    prof = profile_mod.load()
    profile_yaml = profile_mod.load_yaml()

    with db.connect() as conn:
        outcome = pipeline.prepare_one(
            conn, job_id, prof, profile_yaml,
            documents_only=documents_only == "1",
        )

    log.info("prepare %s -> %s", job_id, outcome.get("status"))
    return RedirectResponse(f"/job/{job_id}", status_code=303)


@app.get("/file")
def serve_file(path: str):
    """Serve a generated resume or cover letter.

    Restricted to PRIVATE_DIR: `path` arrives from a query string, so without
    the resolve-and-compare below a crafted value would read any file the
    process can reach.
    """
    target = Path(path).expanduser().resolve()
    root = PRIVATE_DIR.resolve()

    if not target.is_relative_to(root):
        raise HTTPException(403, "outside the generated-documents directory")
    if not target.is_file():
        raise HTTPException(404, "no such file")

    media = {
        ".pdf": "application/pdf",
        ".html": "text/html",
        ".txt": "text/plain",
        ".png": "image/png",
    }.get(target.suffix.lower(), "application/octet-stream")

    return FileResponse(target, media_type=media)


@app.get("/api/stats")
def stats():
    """Counts as JSON, for scripting against."""
    with db.connect() as conn:
        return {"stages": db.stage_counts(conn)}


def serve(host: str = "127.0.0.1", port: int = 8765, reload: bool = False) -> None:
    import uvicorn

    db.init()
    Preferences.load()   # fail fast on a broken preferences file
    uvicorn.run(
        "shotgun.web.app:app" if reload else app,
        host=host, port=port, reload=reload, log_level="info",
    )
