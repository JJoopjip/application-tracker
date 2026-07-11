"""Derived views over the raw application rows: the stats strip, the
needs-attention list, and the human "Today" sentence. Kept separate from the
DB layer so the rules are easy to read and tune.
"""

from datetime import date, datetime, timedelta

from . import db


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except (ValueError, TypeError):
        return None


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None


def needs_attention(rows: list) -> list:
    """A card needs attention when its next action is due today or overdue, OR
    it's still live but has gone quiet for more than 7 days."""
    today = date.today()
    stale_cutoff = datetime.now() - timedelta(days=7)
    out = []
    for r in rows:
        due = _parse_date(r["next_action_due"])
        is_due = due is not None and due <= today

        last = _parse_dt(r["last_activity"])
        is_stale = (
            r["status"] in db.ACTIVE_STATUSES
            and last is not None
            and last < stale_cutoff
        )

        if is_due or is_stale:
            out.append(r)

    # Soonest / most-overdue due dates first; undated stale ones after.
    def sort_key(r):
        due = _parse_date(r["next_action_due"])
        return (due is None, due or date.max)

    return sorted(out, key=sort_key)


def compute_stats(rows: list) -> dict:
    """The stats strip: active, total applied, response rate, interviews, offers."""
    active = sum(1 for r in rows if r["status"] in db.ACTIVE_STATUSES)
    applied = sum(1 for r in rows if r["status"] in db.APPLIED_STATUSES)
    responded = sum(1 for r in rows if r["status"] in db.RESPONDED_STATUSES)
    interviews = sum(
        1 for r in rows if r["status"] in {"interview", "final", "offer"}
    )
    offers = sum(1 for r in rows if r["status"] == "offer")

    response_rate = round(responded / applied * 100) if applied else 0

    return {
        "active": active,
        "applied": applied,
        "response_rate": response_rate,
        "interviews": interviews,
        "offers": offers,
    }


def today_band(rows: list, attention: list) -> str:
    """One warm, human sentence for the top band. Plain language, not stats.

    Tune this copy freely — it's the one bold, personal element of the UI.
    """
    if not rows:
        return "A fresh start. Add the first role you're eyeing and we'll take it from here."

    today = date.today()
    soon = today + timedelta(days=7)

    # Find the nearest upcoming interview (by next_action_due on interview-stage
    # applications) to name specifically, the way a friend would.
    upcoming_interview = None
    for r in rows:
        if r["status"] in {"interview", "final"}:
            due = _parse_date(r["next_action_due"])
            if due and today <= due <= soon:
                if upcoming_interview is None or due < upcoming_interview[1]:
                    upcoming_interview = (r, due)

    waiting = len(attention)

    parts = []
    if waiting == 1:
        parts.append("1 application is waiting on you")
    elif waiting > 1:
        parts.append(f"{waiting} applications are waiting on you")

    if upcoming_interview:
        r, due = upcoming_interview
        when = _friendly_day(due, today)
        parts.append(f"{r['company']}'s interview is {when}")

    if not parts:
        return "Nothing needs you right this second. Nice work keeping on top of it."

    if len(parts) == 1:
        return parts[0].capitalize() + "."
    return f"{parts[0].capitalize()}, and {parts[1]}."


def attn_reason(row) -> str:
    """Short human note on *why* a card is in Needs attention."""
    today = date.today()
    due = _parse_date(row["next_action_due"])
    if due is not None and due <= today:
        if due == today:
            return "Due today"
        overdue = (today - due).days
        return f"Overdue by {overdue} day{'s' if overdue != 1 else ''}"

    last = _parse_dt(row["last_activity"])
    if last is not None:
        days = (datetime.now() - last).days
        return f"Quiet for {days} days"
    return ""


def fmt_date(value: str | None) -> str:
    """Format an ISO date/datetime string for display, e.g. 'Jul 10'."""
    d = _parse_date(value)
    if d is None:
        return value or ""
    return d.strftime("%b %-d, %Y") if d.year != date.today().year else d.strftime("%b %-d")


def _friendly_day(d: date, today: date) -> str:
    delta = (d - today).days
    if delta == 0:
        return "today"
    if delta == 1:
        return "tomorrow"
    if 2 <= delta <= 6:
        return d.strftime("%A")  # e.g. "Thursday"
    return d.strftime("%b %-d")
