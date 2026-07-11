"""Tiny in-memory tracker for the long-running 'Tailor resume' jobs.

The generator takes minutes, so we run it on a background thread and let the
browser poll for status. State lives in a module-level dict — fine because the
app runs as a single uvicorn process (see run.py). It's intentionally not
persisted: an interrupted job just disappears and can be re-run.
"""

import threading

# app_id -> {"status": "running"|"done"|"error", "message": str, "version_id": int|None}
_jobs: dict[int, dict] = {}
_lock = threading.Lock()


def start(app_id: int, target, *args) -> bool:
    """Start `target(*args)` on a background thread for this application.
    Returns False if a job is already running for it."""
    with _lock:
        if _jobs.get(app_id, {}).get("status") == "running":
            return False
        _jobs[app_id] = {"status": "running", "message": "", "version_id": None}

    thread = threading.Thread(target=target, args=args, daemon=True)
    thread.start()
    return True


def set_done(app_id: int, version_id: int) -> None:
    with _lock:
        _jobs[app_id] = {"status": "done", "message": "", "version_id": version_id}


def set_error(app_id: int, message: str) -> None:
    with _lock:
        _jobs[app_id] = {"status": "error", "message": message, "version_id": None}


def get(app_id: int) -> dict | None:
    with _lock:
        return dict(_jobs[app_id]) if app_id in _jobs else None


def clear(app_id: int) -> None:
    with _lock:
        _jobs.pop(app_id, None)
