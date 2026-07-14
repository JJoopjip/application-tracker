"""FastAPI application entry point.

Server-rendered HTML with a sprinkle of HTMX for the one-tap card actions.
No build step, no npm. Run with `python run.py`.

The route handlers live in app/routes/*.py, grouped by concern (applications,
intake, tailor, review, settings, backup); this module just builds the app,
mounts static files, and includes those routers. Shared Jinja setup is in
app/templating.py.
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from . import db
from .db import ROOT
from .routes import applications, backup, intake, review, settings, tailor


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Run once on startup: make sure the DB and its schema exist."""
    db.init_db()
    yield


app = FastAPI(title="Job Application Tracker", lifespan=lifespan)

app.mount("/static", StaticFiles(directory=str(ROOT / "static")), name="static")

# One router per area of the app. Order is cosmetic — paths don't overlap.
app.include_router(applications.router)
app.include_router(intake.router)
app.include_router(tailor.router)
app.include_router(review.router)
app.include_router(settings.router)
app.include_router(backup.router)
