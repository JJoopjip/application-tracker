"""Settings page (Phase 2): the Anthropic API key, the resume text, and the
Gmail connection status — all editable in-browser."""

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import gmail_client, settings_store
from ..templating import templates

router = APIRouter()


@router.get("/settings", response_class=HTMLResponse)
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


@router.post("/settings/resume")
async def save_resume(request: Request):
    form = await request.form()
    settings_store.set_resume(form.get("resume", ""))
    return RedirectResponse("/settings?saved=resume", status_code=303)


@router.post("/settings/apikey")
async def save_apikey(request: Request):
    form = await request.form()
    key = (form.get("api_key") or "").strip()
    if key:  # never blank out an existing key with an empty submit
        settings_store.set_api_key(key)
    return RedirectResponse("/settings?saved=key", status_code=303)
