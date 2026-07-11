"""FastAPI application — Phase 1: the core, manual job application tracker.

Server-rendered HTML with a sprinkle of HTMX for the one-tap card actions.
No build step, no npm. Run with `python run.py`.
"""

import csv
import io
import json
from datetime import date, timedelta
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import ai, db, fetch, jobs, logic, settings_store
from .db import ROOT
from integrations import resume_gen

app = FastAPI(title="Job Application Tracker")

templates = Jinja2Templates(directory=str(ROOT / "templates"))
app.mount("/static", StaticFiles(directory=str(ROOT / "static")), name="static")

# Human-friendly labels for the nine statuses.
STATUS_LABELS = {
    "wishlist": "Wishlist",
    "applied": "Applied",
    "screening": "Screening",
    "interview": "Interview",
    "final": "Final round",
    "offer": "Offer",
    "rejected": "Rejected",
    "ghosted": "Ghosted",
    "withdrawn": "Withdrawn",
}

templates.env.globals["STATUSES"] = db.STATUSES
templates.env.globals["STATUS_LABELS"] = STATUS_LABELS
templates.env.globals["fmt_date"] = logic.fmt_date
templates.env.globals["attn_reason"] = logic.attn_reason


def _fromjson(value):
    """Jinja filter: parse a JSON-array column into a list (empty on failure)."""
    if not value:
        return []
    try:
        return json.loads(value)
    except (ValueError, TypeError):
        return []


templates.env.filters["fromjson"] = _fromjson


@app.on_event("startup")
def _startup() -> None:
    db.init_db()


# ---------------------------------------------------------------------------
# Home
# ---------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    rows = db.all_applications()
    attention = logic.needs_attention(rows)
    stats = logic.compute_stats(rows)
    band = logic.today_band(rows, attention)

    # Pipeline = everything, most-recent activity first (already sorted by DB).
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "request": request,
            "band": band,
            "attention": attention,
            "pipeline": rows,
            "stats": stats,
            "active_status": None,
            "search": "",
        },
    )


# The pipeline list is re-rendered on its own for filter/search via HTMX.
@app.get("/pipeline", response_class=HTMLResponse)
def pipeline(request: Request, status: str = "", q: str = ""):
    rows = db.all_applications(status=status or None, search=q or None)
    return templates.TemplateResponse(
        request,
        "partials/pipeline_list.html",
        {"request": request, "pipeline": rows, "active_status": status or None},
    )


# ---------------------------------------------------------------------------
# Add / edit
# ---------------------------------------------------------------------------
@app.get("/applications/new", response_class=HTMLResponse)
def new_form(request: Request):
    return templates.TemplateResponse(
        request,
        "form.html", {"request": request, "app": None}
    )


@app.post("/applications")
async def create(request: Request):
    form = await request.form()
    data = {k: form.get(k) for k in db.EDITABLE_FIELDS}
    app_id = db.create_application(data)
    return RedirectResponse(f"/applications/{app_id}", status_code=303)


@app.get("/applications/{app_id}", response_class=HTMLResponse)
def detail(request: Request, app_id: int):
    app_row = db.get_application(app_id)
    if app_row is None:
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(
        request,
        "detail.html",
        {
            "request": request,
            "app": app_row,
            "app_id": app_id,
            "job": jobs.get(app_id),
            "versions": db.get_resume_versions(app_id),
        },
    )


@app.get("/applications/{app_id}/edit", response_class=HTMLResponse)
def edit_form(request: Request, app_id: int):
    app_row = db.get_application(app_id)
    if app_row is None:
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(
        request,
        "form.html", {"request": request, "app": app_row}
    )


@app.post("/applications/{app_id}")
async def update(request: Request, app_id: int):
    form = await request.form()
    data = {k: form.get(k) for k in db.EDITABLE_FIELDS if k in form}
    db.update_application(app_id, data)
    return RedirectResponse(f"/applications/{app_id}", status_code=303)


@app.post("/applications/{app_id}/delete")
def delete(app_id: int):
    db.delete_application(app_id)
    return RedirectResponse("/", status_code=303)


# ---------------------------------------------------------------------------
# Quick actions (HTMX) — each returns the refreshed home body.
# ---------------------------------------------------------------------------
def _home_body(request: Request) -> HTMLResponse:
    rows = db.all_applications()
    attention = logic.needs_attention(rows)
    stats = logic.compute_stats(rows)
    band = logic.today_band(rows, attention)
    return templates.TemplateResponse(
        request,
        "partials/home_body.html",
        {
            "request": request,
            "band": band,
            "attention": attention,
            "pipeline": rows,
            "stats": stats,
            "active_status": None,
            "search": "",
        },
    )


@app.post("/applications/{app_id}/followup", response_class=HTMLResponse)
def followup(request: Request, app_id: int):
    """Followed up (+7d): push the next action a week out."""
    due = (date.today() + timedelta(days=7)).isoformat()
    db.set_fields(app_id, next_action_due=due)
    return _home_body(request)


@app.post("/applications/{app_id}/advance", response_class=HTMLResponse)
def advance(request: Request, app_id: int):
    """Move forward →: walk the application one step down the pipeline."""
    app_row = db.get_application(app_id)
    if app_row is not None:
        cur = app_row["status"]
        order = db.FORWARD_STATUSES
        if cur in order and order.index(cur) < len(order) - 1:
            new_status = order[order.index(cur) + 1]
            extra = {}
            # First time we mark as applied, stamp the applied date if empty.
            if new_status == "applied" and not app_row["date_applied"]:
                extra["date_applied"] = date.today().isoformat()
            db.set_fields(app_id, status=new_status, **extra)
    return _home_body(request)


@app.post("/applications/{app_id}/reject", response_class=HTMLResponse)
def reject(request: Request, app_id: int):
    """Log rejection — quietly, no drama."""
    db.set_fields(app_id, status="rejected", next_action=None, next_action_due=None)
    return _home_body(request)


# ---------------------------------------------------------------------------
# Settings — API key + resume text (Phase 2)
# ---------------------------------------------------------------------------
@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, saved: str = ""):
    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "request": request,
            "resume": settings_store.get_resume(),
            "has_key": settings_store.has_api_key(),
            "masked_key": settings_store.masked_api_key(),
            "saved": saved,
        },
    )


@app.post("/settings/resume")
async def save_resume(request: Request):
    form = await request.form()
    settings_store.set_resume(form.get("resume", ""))
    return RedirectResponse("/settings?saved=resume", status_code=303)


@app.post("/settings/apikey")
async def save_apikey(request: Request):
    form = await request.form()
    key = (form.get("api_key") or "").strip()
    if key:  # never blank out an existing key with an empty submit
        settings_store.set_api_key(key)
    return RedirectResponse("/settings?saved=key", status_code=303)


# ---------------------------------------------------------------------------
# Add from job posting (Phase 2): paste/URL -> AI -> editable review -> save
# ---------------------------------------------------------------------------
def _split_terms(value: str) -> list:
    """Turn a comma/newline separated string into a clean list."""
    if not value:
        return []
    parts = value.replace("\n", ",").split(",")
    return [p.strip() for p in parts if p.strip()]


@app.get("/intake", response_class=HTMLResponse)
def intake_page(request: Request):
    return templates.TemplateResponse(
        request,
        "intake.html",
        {
            "request": request,
            "has_key": settings_store.has_api_key(),
            "has_resume": settings_store.has_resume(),
            "error": None,
        },
    )


@app.post("/intake/analyze", response_class=HTMLResponse)
async def intake_analyze(request: Request):
    form = await request.form()
    jd_text = (form.get("jd_text") or "").strip()
    url = (form.get("url") or "").strip()

    # If they gave a URL and no pasted text, fetch it server-side.
    if url and not jd_text:
        fetched = fetch.fetch_job_text(url)
        if not fetched["ok"]:
            return templates.TemplateResponse(
                request,
                "intake.html",
                {
                    "request": request,
                    "has_key": settings_store.has_api_key(),
                    "has_resume": settings_store.has_resume(),
                    "error": fetched["error"],
                },
            )
        jd_text = fetched["text"]

    if not jd_text:
        return templates.TemplateResponse(
            request,
            "intake.html",
            {
                "request": request,
                "has_key": settings_store.has_api_key(),
                "has_resume": settings_store.has_resume(),
                "error": "Paste a job description, or enter a link to one, first.",
            },
        )

    result = ai.analyze(jd_text, settings_store.get_resume())
    if not result["ok"]:
        # Degrade gracefully: show the review screen empty so they can still
        # fill it in by hand, with the error explained at the top.
        proposal = {}
        error = result["error"]
    else:
        proposal = result["data"]
        error = None

    return templates.TemplateResponse(
        request,
        "review.html",
        {
            "request": request,
            "p": proposal,
            "jd_text": jd_text,
            "posting_url": url,
            "error": error,
        },
    )


@app.post("/intake/save")
async def intake_save(request: Request):
    """Create the application from the reviewed (and possibly edited) fields.
    The AI proposes; the user decides — nothing was written until this point."""
    form = await request.form()
    data = {k: form.get(k) for k in db.EDITABLE_FIELDS if k in form}

    # keywords / keyword_gap come from the form as comma-separated text; store
    # them as JSON arrays so the detail view can render them as chips.
    data["keywords"] = json.dumps(_split_terms(form.get("keywords", "")))
    data["keyword_gap"] = json.dumps(_split_terms(form.get("keyword_gap", "")))
    # match_score to int (or drop if blank)
    score = (form.get("match_score") or "").strip()
    data["match_score"] = int(score) if score.isdigit() else None
    data["jd_full_text"] = form.get("jd_full_text") or None

    app_id = db.create_application(data)
    return RedirectResponse(f"/applications/{app_id}", status_code=303)


# ---------------------------------------------------------------------------
# Tailor resume (Phase 3): run the user's resume generator for an application.
# ---------------------------------------------------------------------------
@app.post("/applications/{app_id}/jd")
async def save_jd(request: Request, app_id: int):
    """Save a pasted job description onto an application (for hand-added ones
    that have no JD yet), so it can be tailored against."""
    form = await request.form()
    jd = (form.get("jd_full_text") or "").strip()
    if jd:
        db.set_fields(app_id, jd_full_text=jd)
    return RedirectResponse(f"/applications/{app_id}", status_code=303)


def _run_tailor(app_id: int) -> None:
    """Background worker: generate the resume and record the result."""
    app_row = db.get_application(app_id)
    if app_row is None:
        jobs.set_error(app_id, "Application no longer exists.")
        return
    try:
        result = resume_gen.generate(app_row)
    except Exception as exc:  # noqa: BLE001 — never let the thread die silently
        jobs.set_error(app_id, f"Unexpected error: {exc}")
        return
    if not result["ok"]:
        jobs.set_error(app_id, result["error"])
        return
    version_id = db.add_resume_version(
        app_id,
        result.get("pdf_path"),
        result.get("docx_path"),
        result.get("folder"),
    )
    jobs.set_done(app_id, version_id)


@app.post("/applications/{app_id}/tailor", response_class=HTMLResponse)
def tailor(request: Request, app_id: int):
    app_row = db.get_application(app_id)
    if app_row is None:
        return RedirectResponse("/", status_code=303)

    # Fail fast on the obvious problems before launching a background job.
    if not (app_row["jd_full_text"] or "").strip():
        jobs.set_error(app_id, "Add the job description first, then tailor.")
    else:
        problem = resume_gen.preflight()
        if problem:
            jobs.set_error(app_id, problem)
        else:
            jobs.start(app_id, _run_tailor, app_id)

    return _tailor_status_partial(request, app_id)


@app.get("/applications/{app_id}/tailor/status", response_class=HTMLResponse)
def tailor_status(request: Request, app_id: int):
    return _tailor_status_partial(request, app_id)


def _tailor_status_partial(request: Request, app_id: int) -> HTMLResponse:
    job = jobs.get(app_id)
    versions = db.get_resume_versions(app_id)
    return templates.TemplateResponse(
        request,
        "partials/tailor_status.html",
        {"request": request, "app_id": app_id, "job": job, "versions": versions},
    )


@app.get("/resume/{version_id}/download")
def download_resume(version_id: int, fmt: str = "pdf"):
    version = db.get_resume_version(version_id)
    if version is None:
        return RedirectResponse("/", status_code=303)
    path = version["docx_path"] if fmt == "docx" else version["pdf_path"]
    if not path or not Path(path).exists():
        return RedirectResponse(f"/applications/{version['application_id']}", status_code=303)
    media = (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        if fmt == "docx"
        else "application/pdf"
    )
    return FileResponse(path, media_type=media, filename=Path(path).name)


# ---------------------------------------------------------------------------
# Export / backup / restore — your data is never trapped.
# ---------------------------------------------------------------------------
ALL_COLUMNS = [
    "id", "company", "position", "location", "salary", "status", "source",
    "seniority", "industry", "company_size", "keywords", "keyword_gap",
    "match_score", "match_reason", "visa_note", "jd_full_text", "posting_url",
    "resume_path", "cover_letter_path", "date_applied", "next_action",
    "next_action_due", "last_activity", "contact_name", "contact_email",
    "email_thread_id", "interview_notes", "created_at",
]


@app.get("/export/csv")
def export_csv():
    rows = db.all_applications()
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=ALL_COLUMNS)
    writer.writeheader()
    for r in rows:
        writer.writerow({c: r[c] for c in ALL_COLUMNS})
    filename = f"applications_{date.today().isoformat()}.csv"
    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/export/json")
def export_json():
    rows = db.all_applications()
    payload = {
        "exported_at": db.now_iso(),
        "version": 1,
        "applications": [dict(r) for r in rows],
    }
    filename = f"tracker_backup_{date.today().isoformat()}.json"
    return JSONResponse(
        content=payload,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/backup", response_class=HTMLResponse)
def backup_page(request: Request):
    count = len(db.all_applications())
    return templates.TemplateResponse(
        request,
        "backup.html", {"request": request, "count": count, "message": None}
    )


@app.post("/restore", response_class=HTMLResponse)
async def restore(request: Request, mode: str = Form("merge")):
    """Restore from a JSON backup. `mode` is 'merge' (default, keeps existing
    rows) or 'replace' (wipes the table first). Never runs without an explicit
    button press."""
    form = await request.form()
    upload = form.get("backup_file")
    message = None
    imported = 0
    try:
        raw = await upload.read()
        data = json.loads(raw)
        apps = data.get("applications", data if isinstance(data, list) else [])

        with db.get_conn() as conn:
            if mode == "replace":
                conn.execute("DELETE FROM applications")
            for rec in apps:
                cols = [c for c in ALL_COLUMNS if c != "id" and c in rec]
                placeholders = ", ".join("?" for _ in cols)
                conn.execute(
                    f"INSERT INTO applications ({', '.join(cols)}) "
                    f"VALUES ({placeholders})",
                    [rec.get(c) for c in cols],
                )
                imported += 1
        message = f"Restored {imported} application(s) ({mode})."
    except Exception as exc:  # noqa: BLE001 — surface any failure to the user
        message = f"Couldn't read that backup file: {exc}"

    count = len(db.all_applications())
    return templates.TemplateResponse(
        request,
        "backup.html", {"request": request, "count": count, "message": message}
    )
