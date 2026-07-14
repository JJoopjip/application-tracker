"""Export / backup / restore — the user's data is never trapped. CSV and JSON
export, plus JSON restore in merge or replace mode (never runs unprompted)."""

import csv
import io
import json
from datetime import date

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from .. import db
from ..templating import templates

router = APIRouter()

ALL_COLUMNS = [
    "id", "company", "position", "location", "salary", "status", "source",
    "seniority", "industry", "company_size", "keywords", "keyword_gap",
    "match_score", "match_reason", "visa_note", "jd_full_text", "posting_url",
    "resume_path", "cover_letter_path", "date_applied", "next_action",
    "next_action_due", "last_activity", "contact_name", "contact_email",
    "email_thread_id", "interview_notes", "created_at",
]


@router.get("/export/csv")
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


@router.get("/export/json")
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


@router.get("/backup", response_class=HTMLResponse)
def backup_page(request: Request):
    count = len(db.all_applications())
    return templates.TemplateResponse(
        request,
        "backup.html", {"request": request, "count": count, "message": None}
    )


@router.post("/restore", response_class=HTMLResponse)
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
