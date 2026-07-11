"""Match a classified email to an existing application, and map a
classification to a proposed pipeline status.

Match order (spec): email_thread_id → contact_email → fuzzy company name.
"""

import re
from difflib import SequenceMatcher

from . import db

# What status a confirmed email of each kind would move the application to.
# recruiter_outreach and not_job_related don't imply a status change.
STATUS_FOR_CLASS = {
    "rejection": "rejected",
    "interview_invite": "interview",
    "screening_request": "screening",
    "assessment": "screening",
    "offer": "offer",
    "recruiter_outreach": None,
    "not_job_related": None,
}

_FUZZY_THRESHOLD = 0.82


def _norm(name: str) -> str:
    """Normalize a company name for comparison: lowercase, drop common
    suffixes and punctuation."""
    s = (name or "").lower()
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    s = re.sub(
        r"\b(inc|llc|ltd|corp|corporation|co|company|group|technologies|"
        r"technology|labs|the)\b",
        " ",
        s,
    )
    return re.sub(r"\s+", " ", s).strip()


def find_match(thread_id: str, sender_email: str, company: str) -> int | None:
    """Return a matching application id, or None."""
    apps = db.all_applications()

    # 1. Exact Gmail thread already linked to an application.
    if thread_id:
        for a in apps:
            if a["email_thread_id"] and a["email_thread_id"] == thread_id:
                return a["id"]

    # 2. Known contact email.
    if sender_email:
        se = sender_email.lower()
        for a in apps:
            if a["contact_email"] and a["contact_email"].lower() == se:
                return a["id"]

    # 3. Fuzzy company-name match.
    target = _norm(company)
    if target:
        best_id, best_score = None, 0.0
        for a in apps:
            score = SequenceMatcher(None, target, _norm(a["company"])).ratio()
            if score > best_score:
                best_id, best_score = a["id"], score
        if best_score >= _FUZZY_THRESHOLD:
            return best_id

    return None


def proposed_status(classification: str) -> str | None:
    return STATUS_FOR_CLASS.get(classification)
