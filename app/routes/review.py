"""Gmail review queue (Phase 4): connect, scan the inbox on a background thread,
classify each new email, and let the user confirm/ignore proposed changes. The
applications table is only ever written from an email in review_confirm."""

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import ai, db, gmail_client, jobs, matching
from ..templating import templates

router = APIRouter()


@router.post("/gmail/connect")
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


@router.get("/review", response_class=HTMLResponse)
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


@router.post("/gmail/scan", response_class=HTMLResponse)
def gmail_scan(request: Request, days: int = 14):
    if gmail_client.status() != "connected":
        jobs.scan_error("Gmail isn't connected yet — connect it on the Settings page.")
    else:
        jobs.scan_start(_run_scan, days)
    return _scan_status_partial(request)


@router.get("/review/scan-status", response_class=HTMLResponse)
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


@router.post("/review/{event_id}/confirm")
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


@router.post("/review/{event_id}/ignore")
def review_ignore(event_id: int):
    db.resolve_email_event(event_id, "ignored")
    return RedirectResponse("/review", status_code=303)
