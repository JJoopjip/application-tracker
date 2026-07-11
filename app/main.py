"""FastAPI application — Phase 1: the core, manual job application tracker.

Server-rendered HTML with a sprinkle of HTMX for the one-tap card actions.
No build step, no npm. Run with `python run.py`.
"""

import csv
import io
import json
from datetime import date, timedelta

from fastapi import FastAPI, Form, Request
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import db, logic
from .db import ROOT

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
        "detail.html", {"request": request, "app": app_row}
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
