#!/usr/bin/env python3
"""Start the Job Application Tracker.

    python run.py

Starts the local server and prints the URL. That's the whole thing.
"""

import os
import webbrowser

import uvicorn

# Bind address. Defaults to localhost for the plain `python run.py` path; the
# Docker image sets HOST=0.0.0.0 so the container is reachable from the host.
HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "8000"))

# Auto-reload on code changes — on by default so editing app/*.py takes effect
# without a manual restart. Set RELOAD=0 to disable (e.g. in the Docker image).
# We watch only app/ on purpose: watching the whole project would reload on
# every data/tracker.db write and churn through .venv.
RELOAD = os.environ.get("RELOAD", "1") != "0"


def main() -> None:
    url = f"http://{HOST}:{PORT}"
    print("\n  Job Application Tracker")
    print(f"  → open {url}\n")
    try:
        webbrowser.open(url)
    except Exception:
        pass  # No browser (e.g. WSL/headless) is fine — the URL is printed.
    uvicorn.run(
        "app.main:app",
        host=HOST,
        port=PORT,
        reload=RELOAD,
        reload_dirs=["app"] if RELOAD else None,
    )


if __name__ == "__main__":
    main()
