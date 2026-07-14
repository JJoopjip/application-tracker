"""Shared Jinja2 setup: one Templates instance plus the globals and filters the
route modules render with. Kept separate from main.py so routers can import
``templates`` without importing the app (which would be circular)."""

import json

from fastapi.templating import Jinja2Templates

from . import db, logic
from .db import ROOT

templates = Jinja2Templates(directory=str(ROOT / "templates"))

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

EMAIL_LABELS = {
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


templates.env.globals["STATUSES"] = db.STATUSES
templates.env.globals["STATUS_LABELS"] = STATUS_LABELS
templates.env.globals["fmt_date"] = logic.fmt_date
templates.env.globals["attn_reason"] = logic.attn_reason
templates.env.globals["EMAIL_LABELS"] = EMAIL_LABELS
templates.env.filters["fromjson"] = _fromjson
