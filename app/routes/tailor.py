"""Tailor-resume flow (Phase 3): run the user's resume generator for an
application on a background thread, poll status via HTMX, download the result.
Also the save-JD endpoint used to attach a pasted posting to a hand-added row."""

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse

from .. import db, jobs
from ..templating import templates
from integrations import resume_gen

router = APIRouter()


@router.post("/applications/{app_id}/jd")
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


@router.post("/applications/{app_id}/tailor", response_class=HTMLResponse)
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


@router.get("/applications/{app_id}/tailor/status", response_class=HTMLResponse)
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


@router.get("/resume/{version_id}/download")
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
