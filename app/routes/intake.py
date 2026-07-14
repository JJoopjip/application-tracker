"""Add-from-posting flow (Phase 2): paste/URL -> AI analysis -> editable review
-> save. Also the machine import endpoint the resume generator POSTs to."""

import json

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from .. import ai, db, fetch, import_from_generator, matching, settings_store
from ..templating import templates

router = APIRouter()


def _split_terms(value: str) -> list:
    """Turn a comma/newline separated string into a clean list."""
    if not value:
        return []
    parts = value.replace("\n", ",").split(",")
    return [p.strip() for p in parts if p.strip()]


@router.get("/intake", response_class=HTMLResponse)
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


@router.post("/intake/analyze", response_class=HTMLResponse)
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

    # Flag (don't block) if this looks like a role we already track, so the user
    # can open the existing card instead of creating a second one.
    duplicate = None
    if proposal:
        duplicate = matching.find_possible_duplicate(
            proposal.get("company", ""), proposal.get("position", "")
        )

    return templates.TemplateResponse(
        request,
        "review.html",
        {
            "request": request,
            "p": proposal,
            "jd_text": jd_text,
            "posting_url": url,
            "error": error,
            "duplicate": duplicate,
        },
    )


@router.post("/api/import")
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


@router.post("/intake/save")
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
