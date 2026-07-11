# Job Application Tracker — Build Spec

> Paste this whole file into Claude Code (or save it as `SPEC.md` in your project folder and tell Claude Code to read it).
> Build it **phase by phase**. Do not start Phase 2 until Phase 1 runs end-to-end.

---

## 0. Context

A local-first job application tracker for managing a job search from one place. Everything runs on the user's machine; no data leaves it except calls to the Anthropic API for text analysis. It integrates with an existing Python resume generator and Gmail.

**Build order:** Phase 1 → 2 → 3 → 4. Each phase must be fully usable on its own before moving on.

---

## 1. Stack

- **Backend:** Python + FastAPI
- **DB:** SQLite (single file `data/tracker.db`), accessed via SQLAlchemy or plain `sqlite3`
- **Frontend:** Server-rendered HTML + vanilla JS + HTMX (no build step, no npm, no React). It must open by typing one command and hitting `localhost`.
- **AI:** Anthropic API via the `anthropic` Python SDK. Read the key from a `.env` file (`ANTHROPIC_API_KEY`). Never hardcode it.
- **Run command:** `python run.py` should start the server and print the URL. Keep it that simple.
- Include a `requirements.txt` and a short `README.md` with setup steps.

**Constraints**
- No Docker, no Postgres, no cloud services.
- The whole thing should survive being zipped up and moved to another machine.
- Write a `.gitignore` that excludes `.env`, `data/`, `credentials.json`, `token.json`, and any generated resumes.

---

## 2. Data model

One table: `applications`.

**Core**
| field | type | notes |
|---|---|---|
| id | int PK | |
| company | text | required |
| position | text | required |
| location | text | e.g. "Toronto / Hybrid" |
| salary | text | free text — postings are inconsistent |
| status | enum | `wishlist`, `applied`, `screening`, `interview`, `final`, `offer`, `rejected`, `ghosted`, `withdrawn` |
| source | text | LinkedIn / referral / company site / recruiter |
| seniority | text | Associate / Manager / Senior / Director |
| industry | text | |
| company_size | text | Startup / Mid / Enterprise |

**AI-extracted (Phase 2)**
| field | type | notes |
|---|---|---|
| keywords | JSON array | key skills/terms from the JD |
| keyword_gap | JSON array | JD keywords **missing** from my resume — this is the most valuable field, treat it as first-class in the UI |
| match_score | int 0–100 | |
| match_reason | text | 2–3 sentences: why this score, what's strong, what's weak |
| visa_note | text | any sponsorship / work-authorization language found in the JD |
| jd_full_text | text | raw JD, stored so I never lose it when the posting expires |
| posting_url | text | |

**Linkage (Phase 3)**
| field | type | notes |
|---|---|---|
| resume_path | text | path to the tailored resume actually sent |
| cover_letter_path | text | |

**Tracking**
| field | type | notes |
|---|---|---|
| date_applied | date | |
| next_action | text | *what* to do, not just "follow up" |
| next_action_due | date | |
| last_activity | datetime | auto-updated on any change |
| contact_name | text | |
| contact_email | text | used for Gmail matching in Phase 4 |
| email_thread_id | text | Gmail thread, so I can jump straight to it |
| interview_notes | text | long-form; questions asked, people met |
| created_at | datetime | |

Also create an `email_events` table in Phase 4 (see below).

---

## 3. Design direction (important — do not skip)

**The client has already rejected generic AI-looking dashboards.** This is a tool for someone who opens it every morning while anxious about their job hunt. It should feel **calm, warm, encouraging, and legible at a glance** — not like a corporate CRM, and not like a startup landing page.

**Do NOT use:** cream/beige background with a serif display face and a terracotta accent. Do NOT use near-black with a single acid accent. Do NOT use hairline-rule broadsheet columns. These are the three defaults; avoid all three.

**Direction to build instead — "friendly field notebook":**
- **Palette:** soft off-white paper base, a muted sage/eucalyptus green as the primary (progress, forward motion), a warm apricot for "needs attention", a dusty rose for rejections (soft, not alarming — rejections are normal and the UI shouldn't punish me), and a deep slate for text. Pick 5–6 exact hexes and put them in CSS variables at the top of one stylesheet. Everything else derives from them.
- **Type:** a rounded, friendly display face (e.g. Nunito, Quicksand, or Baloo 2) for headings and numbers, paired with a highly legible body face (e.g. Inter or Source Sans 3). Generous line-height. Nothing below 13px. Large tap targets.
- **Shape language:** soft, generous border-radius (14–18px), gentle shadows, no harsh borders. Cards should feel like they could be picked up.
- **Signature element:** the **"Today" band** at the top of the home screen — a warm, human, one-sentence summary written in plain language, not stats. Something like *"3 companies are waiting on you, and Shopify's interview is Thursday."* Under it, only the cards that need action today. Everything else is below the fold. This band is the one bold thing; keep the rest of the interface quiet.
- **Empty and failure states matter.** An empty pipeline should be an invitation, not a void. A rejection should be logged without drama. Write copy that's warm and specific, never chirpy or fake-positive.
- **Motion:** minimal. A soft fade when cards move stages. Respect `prefers-reduced-motion`.
- Must be **responsive down to phone width** and keyboard-navigable with visible focus rings.

---

# PHASE 1 — Core tracker (build this first, ship it, use it)

**Goal:** I can manage my whole job hunt manually and it's already better than a spreadsheet.

**Build:**
1. FastAPI app + SQLite + the `applications` table (all core + tracking fields; leave AI/linkage columns nullable for now).
2. **Home screen** with three zones, top to bottom:
   - **Today band** (the signature element described above)
   - **Needs attention** — cards where `next_action_due <= today`, OR status is active and `last_activity` is more than 7 days ago. Each card has one-tap actions: `Followed up (+7d)`, `Move forward →`, `Log rejection`, `Open details`.
   - **Pipeline** — all applications, filterable by status, with a search box across company/position/notes.
3. **Add / edit form** covering every core + tracking field.
4. **Stats strip:** active count, total applied, response rate (`% of applied that reached screening or beyond`), interviews, offers.
5. **Export to CSV** and **JSON backup / restore**. My data must never be trapped.

**Done when:** I can add a real application, see it surface in "Needs attention" a week later, and export it.

---

# PHASE 2 — JD intake and analysis

**Goal:** I paste a job posting and the app fills itself in.

**Build:**
1. An **"Add from job posting"** input that accepts either:
   - a **pasted JD** (textarea), or
   - a **URL** — fetch server-side with `httpx` + `BeautifulSoup`, strip nav/footer, extract main text. If the fetch fails or the page is JS-only (LinkedIn, Workday often are), **fail gracefully**: show a clear message saying the page couldn't be read and to paste the text instead. Do not silently produce garbage.
2. Store my resume as a plain-text file at `data/my_resume.txt` (I'll paste my resume into it). Add a settings page where I can edit it in-browser.
3. Send `{JD text} + {my resume text}` to the Anthropic API and extract, in **one structured call**:
   - company, position, location, salary, seniority, industry, company_size
   - `keywords` — the terms this employer actually cares about
   - `keyword_gap` — **JD keywords that do NOT appear in my resume**
   - `match_score` (0–100) and `match_reason`
   - `visa_note` — any work-authorization / sponsorship language
   
   Prompt the model to return **JSON only**, no prose, no markdown fences. Parse defensively (strip fences if present, `try/except`, show the raw response on failure rather than crashing).
4. Show the result in a **review screen before saving** — every field editable. The AI proposes; I decide. Never write straight to the DB.
5. In the application detail view, render `keyword_gap` prominently as chips. This is the thing I act on.

**Done when:** I can paste a real BD job posting and get a saved, fully-populated application in under 30 seconds.

---

# PHASE 3 — Resume generator integration

**Goal:** one button in the tracker produces the tailored resume for that specific job, and the tracker remembers which version I sent.

**Build:**
1. **First, read my existing Python resume generator.** It's somewhere in this project folder or I'll point you at it. Determine:
   - How it's invoked (CLI args? a function? a config file?)
   - What input it expects (JSON? YAML? plain args?)
   - What it outputs (.docx? .pdf? where?)
   
   **Ask me to confirm your understanding before writing the adapter.** Don't guess.
2. Write a thin adapter module `integrations/resume_gen.py` that:
   - Takes an application record
   - Maps it into whatever input format my generator expects (including `keywords` and `keyword_gap` so the resume can emphasize the right things)
   - Invokes the generator
   - Returns the output file path
   - **Does not modify my generator's source.** Wrap it, don't rewrite it.
3. Add a **"Tailor resume"** button on each application card. On click: run the adapter, save the output to `data/resumes/{company}_{position}_{date}.{ext}`, store the path in `resume_path`, and show a download/open link.
4. The detail view shows which resume version was sent, with the date. If I re-generate, keep a history rather than overwriting.

**Done when:** I click one button and get a resume tailored to that JD, permanently linked to that application.

---

# PHASE 4 — Gmail review queue

**Goal:** the app reads my inbox and tells me what changed. **It never changes anything without my confirmation.**

**Build:**
1. **Gmail API, read-only scope** (`gmail.readonly`). Nothing else. Write clear setup steps in the README:
   - Create a Google Cloud project → enable Gmail API → create OAuth desktop credentials → download `credentials.json` into the project root → first run opens a browser to authorize → token cached in `token.json`.
2. A **"Scan inbox"** button (and optionally a manual daily run). It fetches messages from the last N days (default 14, configurable).
3. For each message, use the Anthropic API to classify it as one of:
   `rejection` · `interview_invite` · `screening_request` · `assessment/OA` · `offer` · `recruiter_outreach` · `not_job_related`
   
   Also extract: which company it's about, any dates/deadlines mentioned, the sender's name and email, and any notable detail (e.g. *"invited to reapply in 6 months"*).
4. **Match** the email to an existing application by, in order: `email_thread_id` → `contact_email` → fuzzy company-name match. If no confident match, offer to **create a new application** from it (this catches recruiters reaching out to me — which is exactly the pipeline I want to capture).
5. **Review queue screen.** Each detected update is a card showing:
   - the email snippet
   - the proposed change (`Shopify · Partnerships Manager → Rejected`)
   - the model's confidence and reasoning
   - buttons: **Confirm** · **Edit** · **Ignore**
   
   **Nothing is written to the applications table until I click Confirm.** This is non-negotiable — false positives are common (e.g. "we're moving forward with others *for this role*, but…" is not a plain rejection).
6. Store every processed message in an `email_events` table (`message_id`, `thread_id`, `classification`, `confidence`, `matched_application_id`, `action_taken`, `processed_at`) so the same email is never re-surfaced and I have an audit trail.
7. When I confirm a rejection, also capture any reapply-window or feedback detail into `interview_notes`.

**Done when:** I click "Scan inbox", see a queue of real, correctly-classified updates, and clear it in under a minute.

---

# PHASE 5 (optional, only if 1–4 are solid) — Insights

A single **Insights** page answering the questions I actually care about:
- **Response rate by source** — is referral outperforming cold LinkedIn applications, and by how much? (This tells me where to spend my time.)
- **Response rate by match_score band** — am I wasting effort on low-match roles?
- Median days from `applied` → first response, and → rejection.
- Which `keyword_gap` terms appear most often across all my applications — i.e. **the skill I should actually go learn or add to my resume.**

Keep it to a handful of honest numbers with plain-language takeaways. No vanity charts.

---

## Working agreement

- **Ask before assuming**, especially about my resume generator's interface and about anything touching Gmail.
- Build the smallest thing that works, show it to me, then extend.
- Comment the AI prompt strings clearly so I can tune them myself later.
- If an Anthropic API call fails, degrade gracefully — I should still be able to enter the data by hand.
- Never delete or overwrite my data without confirmation.
