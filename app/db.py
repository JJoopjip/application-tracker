"""SQLite access layer for the job application tracker.

Uses the stdlib `sqlite3` module (no ORM) to keep the project dependency-light
and portable — the whole thing can be zipped up and moved to another machine.
The database lives at data/tracker.db.
"""

import sqlite3
from datetime import date, datetime
from pathlib import Path

# Project root = one level up from this file's directory (app/).
ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "tracker.db"

# The nine allowed pipeline statuses, in forward order. The order matters:
# "Move forward →" walks an application one step down this list.
STATUSES = [
    "wishlist",
    "applied",
    "screening",
    "interview",
    "final",
    "offer",
    "rejected",
    "ghosted",
    "withdrawn",
]

# Statuses that count as "in the pipeline / still live". Used for the active
# count, the response-rate denominator, and the needs-attention staleness rule.
ACTIVE_STATUSES = {"applied", "screening", "interview", "final", "offer"}

# Statuses that mean an application actually got submitted somewhere. Used as
# the denominator for "total applied" and the response rate.
APPLIED_STATUSES = {
    "applied",
    "screening",
    "interview",
    "final",
    "offer",
    "rejected",
    "ghosted",
}

# Reaching one of these means the employer responded (screening or beyond).
RESPONDED_STATUSES = {"screening", "interview", "final", "offer"}

# Terminal statuses advancing stops at.
FORWARD_STATUSES = ["wishlist", "applied", "screening", "interview", "final", "offer"]


# Every column on the applications table. AI (Phase 2) and linkage (Phase 3)
# columns are created now but left nullable and unused until those phases.
SCHEMA = """
CREATE TABLE IF NOT EXISTS applications (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,

    -- Core
    company           TEXT NOT NULL,
    position          TEXT NOT NULL,
    location          TEXT,
    salary            TEXT,
    status            TEXT NOT NULL DEFAULT 'wishlist',
    source            TEXT,
    seniority         TEXT,
    industry          TEXT,
    company_size      TEXT,

    -- AI-extracted (Phase 2, nullable for now)
    keywords          TEXT,   -- JSON array
    keyword_gap       TEXT,   -- JSON array
    match_score       INTEGER,
    match_reason      TEXT,
    visa_note         TEXT,
    jd_full_text      TEXT,
    posting_url       TEXT,

    -- Linkage (Phase 3, nullable for now)
    resume_path       TEXT,
    cover_letter_path TEXT,

    -- Tracking
    date_applied      TEXT,      -- ISO date (YYYY-MM-DD)
    next_action       TEXT,
    next_action_due   TEXT,      -- ISO date
    last_activity     TEXT,      -- ISO datetime, auto-updated on any change
    contact_name      TEXT,
    contact_email     TEXT,
    email_thread_id   TEXT,
    interview_notes   TEXT,
    created_at        TEXT       -- ISO datetime
);

-- Phase 3: a history of tailored resumes generated for an application. Newer
-- versions are kept alongside older ones rather than overwriting.
CREATE TABLE IF NOT EXISTS resume_versions (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    application_id INTEGER NOT NULL,
    pdf_path       TEXT,
    docx_path      TEXT,
    source_folder  TEXT,     -- the generator's output/<slug>/ folder
    created_at     TEXT,
    FOREIGN KEY (application_id) REFERENCES applications(id) ON DELETE CASCADE
);
"""


def get_conn() -> sqlite3.Connection:
    """Open a connection with row access by column name."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """Create the data directory and table if they don't exist yet."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with get_conn() as conn:
        conn.executescript(SCHEMA)


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def today_iso() -> str:
    return date.today().isoformat()


# Columns a user is allowed to set through the add/edit form.
EDITABLE_FIELDS = [
    "company",
    "position",
    "location",
    "salary",
    "status",
    "source",
    "seniority",
    "industry",
    "company_size",
    "posting_url",
    # AI-extracted (Phase 2). Present here so the intake review screen can save
    # them; the plain add/edit form simply doesn't submit them.
    "keywords",       # JSON-encoded array string
    "keyword_gap",    # JSON-encoded array string
    "match_score",    # int
    "match_reason",
    "visa_note",
    "jd_full_text",
    "date_applied",
    "next_action",
    "next_action_due",
    "contact_name",
    "contact_email",
    "interview_notes",
]


def create_application(data: dict) -> int:
    """Insert a new application. `data` keys are restricted to EDITABLE_FIELDS."""
    fields = {k: (data.get(k) or None) for k in EDITABLE_FIELDS}
    if fields.get("status") not in STATUSES:
        fields["status"] = "wishlist"
    fields["created_at"] = now_iso()
    fields["last_activity"] = now_iso()

    cols = ", ".join(fields.keys())
    placeholders = ", ".join("?" for _ in fields)
    with get_conn() as conn:
        cur = conn.execute(
            f"INSERT INTO applications ({cols}) VALUES ({placeholders})",
            list(fields.values()),
        )
        return cur.lastrowid


def update_application(app_id: int, data: dict) -> None:
    """Update editable fields on an application and bump last_activity."""
    fields = {k: (data.get(k) or None) for k in EDITABLE_FIELDS if k in data}
    if "status" in fields and fields["status"] not in STATUSES:
        fields.pop("status")
    fields["last_activity"] = now_iso()

    assignments = ", ".join(f"{k} = ?" for k in fields)
    with get_conn() as conn:
        conn.execute(
            f"UPDATE applications SET {assignments} WHERE id = ?",
            list(fields.values()) + [app_id],
        )


def set_fields(app_id: int, **fields) -> None:
    """Directly set arbitrary columns (used by quick actions). Always bumps
    last_activity."""
    fields["last_activity"] = now_iso()
    assignments = ", ".join(f"{k} = ?" for k in fields)
    with get_conn() as conn:
        conn.execute(
            f"UPDATE applications SET {assignments} WHERE id = ?",
            list(fields.values()) + [app_id],
        )


def get_application(app_id: int) -> sqlite3.Row | None:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM applications WHERE id = ?", (app_id,)
        ).fetchone()


def delete_application(app_id: int) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM applications WHERE id = ?", (app_id,))


def add_resume_version(
    app_id: int, pdf_path: str, docx_path: str, source_folder: str
) -> int:
    """Record a generated resume and point the application at it (latest wins,
    but older versions stay in resume_versions for history)."""
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO resume_versions "
            "(application_id, pdf_path, docx_path, source_folder, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (app_id, pdf_path, docx_path, source_folder, now_iso()),
        )
        conn.execute(
            "UPDATE applications SET resume_path = ?, last_activity = ? WHERE id = ?",
            (pdf_path, now_iso(), app_id),
        )
        return cur.lastrowid


def get_resume_versions(app_id: int) -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM resume_versions WHERE application_id = ? "
            "ORDER BY created_at DESC",
            (app_id,),
        ).fetchall()


def get_resume_version(version_id: int) -> sqlite3.Row | None:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM resume_versions WHERE id = ?", (version_id,)
        ).fetchone()


def all_applications(
    status: str | None = None, search: str | None = None
) -> list[sqlite3.Row]:
    """Return all applications, newest activity first, optionally filtered."""
    clauses = []
    params: list = []
    if status and status in STATUSES:
        clauses.append("status = ?")
        params.append(status)
    if search:
        clauses.append(
            "(company LIKE ? OR position LIKE ? OR interview_notes LIKE ?)"
        )
        like = f"%{search}%"
        params.extend([like, like, like])
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with get_conn() as conn:
        return conn.execute(
            f"SELECT * FROM applications {where} "
            "ORDER BY COALESCE(last_activity, created_at) DESC",
            params,
        ).fetchall()
