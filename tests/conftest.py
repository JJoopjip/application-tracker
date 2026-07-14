"""Shared test helpers.

The logic/matching functions read rows via ``row["column"]`` (they operate on
``sqlite3.Row`` in production). Plain dicts support the same access, so tests
build rows with :func:`make_row`, which fills in every column those functions
touch so a missing key never masks a real assertion.
"""

from datetime import date, datetime, timedelta


def make_row(**overrides):
    """A minimal application row with sensible defaults, overridable per test."""
    base = {
        "id": 1,
        "company": "Acme",
        "position": "Engineer",
        "status": "applied",
        "next_action_due": None,
        "last_activity": datetime.now().isoformat(timespec="seconds"),
        "email_thread_id": None,
        "contact_email": None,
        "contact_name": None,
    }
    base.update(overrides)
    return base


def days_ago_iso(n: int) -> str:
    """ISO datetime for `n` days before now (for staleness tests)."""
    return (datetime.now() - timedelta(days=n)).isoformat(timespec="seconds")


def date_in(n: int) -> str:
    """ISO date `n` days from today (negative = past)."""
    return (date.today() + timedelta(days=n)).isoformat()
