# Fieldnotes — Job Application Tracker

A calm, local-first tracker for a job hunt. Everything runs on your machine;
no data leaves it. This is **Phase 1** — the core manual tracker.

## What Phase 1 does

- One **Today** band with a plain-language summary of what needs you.
- **Needs attention** cards (a follow-up is due, or an active role has gone
  quiet for 7+ days) with one-tap actions: *Followed up (+7d)*,
  *Move forward →*, *Log rejection*, *Open details*.
- A searchable, status-filterable **pipeline** of every application.
- **Add / edit** form covering every core and tracking field.
- **Stats strip:** active, applied, response rate, interviews, offers.
- **Export to CSV** and **JSON backup / restore** — your data is never trapped.

## Phase 2 — Add from a job posting (built)

Paste a job description (or a link) on the **Add from posting** page and the app
uses the Anthropic API to fill in the fields, score your fit, and — most
usefully — find the **keyword gaps** between the posting and your resume. You
review and edit everything before it's saved. Set your API key and resume text
on the **Settings** page first.

## Phase 3 — Tailor resume (built)

Each application's detail page has a **Tailor resume** button. It runs your
existing resume generator at `/home/joopjip/resume_generator` (overridable with
the `RESUME_GEN_DIR` env var) against that job's description, then saves the
resulting `resume.pdf` / `resume.docx` into `data/resumes/` and links them to
the application — keeping every version, never overwriting.

Requirements for the button to work (all already set up on this machine): the
`resume-gen` entrypoint, the `claude` CLI (signed in), and the `resume-gen`
Docker image. A run takes a few minutes and produces a **draft to review** —
nothing is ever sent anywhere. The tracker wraps your generator; it never
modifies it (see `integrations/resume_gen.py`).

Phase 4 (Gmail review queue) is not built yet. The database already has the
columns it needs.

## Setup

Requires Python 3.11+.

```bash
# 1. Create a virtual environment and install dependencies
python3 -m venv .venv           # if this fails, see "venv note" below
source .venv/bin/activate
pip install -r requirements.txt

# 2. Run it
python run.py
```

Then open **http://127.0.0.1:8000** (it also tries to open your browser).

The SQLite database is created automatically at `data/tracker.db` on first run.

### venv note

If `python3 -m venv` complains that `ensurepip` is unavailable (some Debian /
Ubuntu / WSL setups), either install the system package
(`sudo apt install python3-venv`) or use `virtualenv` instead:

```bash
pip install --user virtualenv
virtualenv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

## Run with Docker (optional)

The plain `python run.py` path above is still the primary one. If you'd rather
use Docker:

```bash
docker build -t fieldnotes .
# Mount ./data so your database persists outside the container:
docker run -p 8000:8000 -v "$(pwd)/data:/app/data" fieldnotes
```

Then open http://127.0.0.1:8000. Your data lives in `./data` on the host.

## Portability

No Docker, no Postgres, no npm, no build step. Fonts are self-hosted in
`static/fonts/` so the UI looks right fully offline. Zip the folder (your `data/`
travels with it, or restore from a JSON backup on the new machine) and it runs
anywhere Python does.

## Data & backups

- Everything lives in `data/tracker.db`.
- **Backup** (top bar) downloads a full JSON copy; the same page restores from
  one (merge or replace — nothing is wiped without an explicit button press).
- `data/`, `.env`, and generated resumes are git-ignored.

## Project layout

```
run.py                 # start the server, print the URL
app/
  main.py              # FastAPI routes
  db.py                # SQLite schema + access (stdlib sqlite3, no ORM)
  logic.py             # stats, needs-attention rules, Today-band copy
templates/             # Jinja2 server-rendered HTML (+ HTMX partials)
static/
  css/                 # styles.css (palette variables at top) + fonts.css
  fonts/               # self-hosted Nunito + Inter (woff2)
  js/htmx.min.js       # bundled, no CDN
data/                  # tracker.db (created on first run) — git-ignored
```
