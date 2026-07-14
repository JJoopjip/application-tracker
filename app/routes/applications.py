"""Core application CRUD, the home/board/pipeline views, and the one-tap HTMX
quick actions (followed-up, advance, reject, set-status)."""

from datetime import date, timedelta

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from .. import db, jobs, logic
from ..templating import templates

router = APIRouter()


# ---------------------------------------------------------------------------
# Home / board / pipeline
# ---------------------------------------------------------------------------
@router.get("/", response_class=HTMLResponse)
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


@router.get("/board", response_class=HTMLResponse)
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
@router.get("/pipeline", response_class=HTMLResponse)
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
@router.get("/applications/new", response_class=HTMLResponse)
def new_form(request: Request):
    return templates.TemplateResponse(
        request,
        "form.html", {"request": request, "app": None}
    )


@router.post("/applications")
async def create(request: Request):
    form = await request.form()
    data = {k: form.get(k) for k in db.EDITABLE_FIELDS}
    app_id = db.create_application(data)
    return RedirectResponse(f"/applications/{app_id}", status_code=303)


@router.get("/applications/{app_id}", response_class=HTMLResponse)
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


@router.get("/applications/{app_id}/edit", response_class=HTMLResponse)
def edit_form(request: Request, app_id: int):
    app_row = db.get_application(app_id)
    if app_row is None:
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(
        request,
        "form.html", {"request": request, "app": app_row}
    )


@router.post("/applications/{app_id}")
async def update(request: Request, app_id: int):
    form = await request.form()
    data = {k: form.get(k) for k in db.EDITABLE_FIELDS if k in form}
    db.update_application(app_id, data)
    return RedirectResponse(f"/applications/{app_id}", status_code=303)


@router.post("/applications/{app_id}/delete")
def delete(app_id: int):
    db.delete_application(app_id)
    return RedirectResponse("/", status_code=303)


# ---------------------------------------------------------------------------
# Quick actions (HTMX) — each returns the refreshed home body.
# ---------------------------------------------------------------------------
def home_body(request: Request) -> HTMLResponse:
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


@router.post("/applications/{app_id}/followup", response_class=HTMLResponse)
def followup(request: Request, app_id: int):
    """Followed up (+7d): push the next action a week out."""
    due = (date.today() + timedelta(days=7)).isoformat()
    db.set_fields(app_id, next_action_due=due)
    return home_body(request)


@router.post("/applications/{app_id}/advance", response_class=HTMLResponse)
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
    return home_body(request)


@router.post("/applications/{app_id}/reject", response_class=HTMLResponse)
def reject(request: Request, app_id: int):
    """Log rejection — quietly, no drama."""
    db.set_fields(app_id, status="rejected", next_action=None, next_action_due=None)
    return home_body(request)


@router.post("/applications/{app_id}/status", response_class=HTMLResponse)
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
        return home_body(request)
    return Response(status_code=204)
