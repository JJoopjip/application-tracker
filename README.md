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

Phase 2 (AI job-posting intake), Phase 3 (resume generator) and Phase 4 (Gmail
review queue) are not built yet. The database already has the columns they need.

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
