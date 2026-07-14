"""FastAPI application — Phase 1: the core, manual job application tracker.

Server-rendered HTML with a sprinkle of HTMX for the one-tap card actions.
No build step, no npm. Run with `python run.py`.
"""

import csv
import io
import json
from contextlib import asynccontextmanager
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

from . import (
    ai,
    db,
    fetch,
    gmail_client,
    import_from_generator,
    jobs,
    logic,
    matching,
    settings_store,
)
from .db import ROOT
from integrations import resume_gen

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Run once on startup: make sure the DB and its schema exist."""
    db.init_db()
    yield


app = FastAPI(title="Job Application Tracker", lifespan=lifespan)

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
templates.env.globals["EMAIL_LABELS"] = {
    "rejection": "Rejection",
    "interview_invite": "Interview invite",
    "screening_request": "Screening request",
    "assessment": "Assessment / OA",
    "offer": "Offer",
    "recruiter_outreach": "Recruiter outreach",
    "not_job_related": "Not job-related",
}


def _fromjson(value):
    """Jinja filter: parse a JSON-array column into a list (empty on failure)."""
    if not value:
        return []
    try:
        return json.loads(value)
    except (ValueError, TypeError):
        return []


templates.env.filters["fromjson"] = _fromjson


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


@app.get("/board", response_class=HTMLResponse)
def board(request: Request):
    """Kanban board: one column per status, drag a card to change its status."""
    rows = db.all_applications()
    columns = {s: [] for s in db.STATUSES}
    for row in rows:
        columns.setdefault(row["status"], []).append(row)
    return templates.TemplateResponse(
        request,
        "board.html",
        {"request": request, "columns": columns, "total": len(rows)},
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


@app.post("/applications/{app_id}/status", response_class=HTMLResponse)
async def set_status(request: Request, app_id: int):
    """Jump an application straight to any status. Powers both the click-the-pill
    dropdown on list cards and drag-and-drop on the board.

    The board drives this with fetch() and only needs a 2xx; the list card asks
    (via `view=home`) for the refreshed home body so stats/attention update too.
    """
    form = await request.form()
    new_status = (form.get("status") or "").strip()
    if new_status not in db.STATUSES:
        return Response("Unknown status", status_code=400)

    app_row = db.get_application(app_id)
    if app_row is None:
        return Response("No such application", status_code=404)

    extra = {}
    # Stamp the applied date the first time it reaches "applied" (matches advance).
    if new_status == "applied" and not app_row["date_applied"]:
        extra["date_applied"] = date.today().isoformat()
    # Closing a role clears any pending nudge, like Log rejection does.
    if new_status in ("rejected", "ghosted", "withdrawn"):
        extra["next_action"] = None
        extra["next_action_due"] = None
    db.set_fields(app_id, status=new_status, **extra)

    if form.get("view") == "home":
        return _home_body(request)
    return Response(status_code=204)


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
            "gmail_status": gmail_client.status(),
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


@app.post("/api/import")
async def api_import(request: Request):
    """Machine endpoint: the resume generator's "Add to tracker" button POSTs
    {"folder": "<abs path to output/<slug>>", "jd_text": "<optional>"}. We read
    that folder off the shared filesystem, AI-extract company/role from the JD,
    create or reuse the application, and attach the resume + cover letter as a
    new version. Localhost-only, same trust model as the rest of the app."""
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "Expected a JSON body."}, status_code=400)
    folder = (payload.get("folder") or "").strip()
    if not folder:
        return JSONResponse({"ok": False, "error": "Missing 'folder'."}, status_code=400)
    result = import_from_generator.import_folder(folder, payload.get("jd_text"))
    return JSONResponse(result, status_code=200 if result.get("ok") else 400)


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
# Gmail review queue (Phase 4)
# ---------------------------------------------------------------------------
@app.post("/gmail/connect")
def gmail_connect():
    """Explicit one-time OAuth. Opens a browser; blocks until authorized."""
    gmail_client.connect()
    return RedirectResponse("/settings?saved=gmail", status_code=303)


def _run_scan(days: int) -> None:
    """Background worker: read inbox, classify each new email, queue matches."""
    result = gmail_client.fetch_recent(days)
    if not result["ok"]:
        jobs.scan_error(result["error"])
        return

    messages = result["messages"]
    relevant = errors = 0
    for m in messages:
        if db.email_seen(m["id"]):
            continue
        classified = ai.classify_email(m)
        if not classified["ok"]:
            errors += 1
            continue
        d = classified["data"]
        cls = d["classification"]
        note_bits = " · ".join(x for x in [d.get("dates"), d.get("notable_detail")] if x)

        if cls == "not_job_related":
            db.add_email_event(
                {
                    "message_id": m["id"], "thread_id": m["thread_id"],
                    "classification": cls, "confidence": d["confidence"],
                    "company_guess": d.get("company"), "role_guess": d.get("role"),
                    "sender_name": m["sender_name"], "sender_email": m["sender_email"],
                    "subject": m["subject"], "snippet": m["snippet"],
                    "dates_text": d.get("dates"), "notable_detail": d.get("notable_detail"),
                    "reasoning": d.get("reasoning"), "proposed_status": None,
                    "matched_application_id": None, "action_taken": "not_relevant",
                }
            )
            continue

        matched = matching.find_match(m["thread_id"], m["sender_email"], d.get("company", ""))
        db.add_email_event(
            {
                "message_id": m["id"], "thread_id": m["thread_id"],
                "classification": cls, "confidence": d["confidence"],
                "company_guess": d.get("company"), "role_guess": d.get("role"),
                "sender_name": m["sender_name"], "sender_email": m["sender_email"],
                "subject": m["subject"], "snippet": m["snippet"],
                "dates_text": d.get("dates"), "notable_detail": note_bits,
                "reasoning": d.get("reasoning"),
                "proposed_status": matching.proposed_status(cls),
                "matched_application_id": matched, "action_taken": None,
            }
        )
        relevant += 1

    msg = f"Scanned {len(messages)} email(s); {relevant} need your review."
    if errors:
        msg += f" ({errors} couldn't be classified and were skipped.)"
    jobs.scan_done(msg)


@app.get("/review", response_class=HTMLResponse)
def review_page(request: Request):
    return templates.TemplateResponse(
        request,
        "inbox.html",
        {
            "request": request,
            "gmail_status": gmail_client.status(),
            "scan": jobs.scan_get(),
            "events": db.pending_email_events(),
            "apps": db.all_applications(),
            "apps_by_id": {a["id"]: a for a in db.all_applications()},
        },
    )


@app.post("/gmail/scan", response_class=HTMLResponse)
def gmail_scan(request: Request, days: int = 14):
    if gmail_client.status() != "connected":
        jobs.scan_error("Gmail isn't connected yet — connect it on the Settings page.")
    else:
        jobs.scan_start(_run_scan, days)
    return _scan_status_partial(request)


@app.get("/review/scan-status", response_class=HTMLResponse)
def scan_status(request: Request):
    return _scan_status_partial(request)


def _scan_status_partial(request: Request) -> HTMLResponse:
    apps = db.all_applications()
    return templates.TemplateResponse(
        request,
        "partials/scan_status.html",
        {
            "request": request,
            "scan": jobs.scan_get(),
            "events": db.pending_email_events(),
            "apps": apps,
            "apps_by_id": {a["id"]: a for a in apps},
        },
    )


@app.post("/review/{event_id}/confirm")
async def review_confirm(request: Request, event_id: int):
    """Apply the (possibly edited) proposed change. This is the ONLY place the
    applications table is written from an email."""
    ev = db.get_email_event(event_id)
    if ev is None:
        return RedirectResponse("/review", status_code=303)
    form = await request.form()
    apply_to = (form.get("apply_to") or "new").strip()
    new_status = (form.get("status") or "").strip()
    note = (form.get("note") or "").strip()

    if apply_to == "new":
        # Create a fresh application from the email (captures recruiter inbound).
        app_id = db.create_application(
            {
                "company": ev["company_guess"] or ev["sender_name"] or "Unknown",
                "position": ev["role_guess"] or "(from email)",
                "status": new_status if new_status in db.STATUSES else "wishlist",
                "contact_name": ev["sender_name"],
                "contact_email": ev["sender_email"],
            }
        )
        db.set_fields(app_id, email_thread_id=ev["thread_id"])
        db.append_note(app_id, note)
        db.resolve_email_event(event_id, "created", app_id)
    else:
        app_id = int(apply_to)
        app_row = db.get_application(app_id)
        if app_row is not None:
            if new_status in db.STATUSES:
                db.set_fields(app_id, status=new_status)
            # Link the thread (and contact, if unknown) so future emails match.
            link = {"email_thread_id": ev["thread_id"]}
            if not app_row["contact_email"] and ev["sender_email"]:
                link["contact_email"] = ev["sender_email"]
            if not app_row["contact_name"] and ev["sender_name"]:
                link["contact_name"] = ev["sender_name"]
            db.set_fields(app_id, **link)
            db.append_note(app_id, note)
        db.resolve_email_event(event_id, "confirmed", app_id)

    return RedirectResponse("/review", status_code=303)


@app.post("/review/{event_id}/ignore")
def review_ignore(event_id: int):
    db.resolve_email_event(event_id, "ignored")
    return RedirectResponse("/review", status_code=303)


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
