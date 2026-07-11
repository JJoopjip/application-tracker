"""The one AI call in Phase 2: read a job description + the user's resume and
extract a structured application record — including the keyword_gap, which is
the whole point of this feature.

Uses the Anthropic Python SDK with **structured outputs** (`output_config.format`
with a JSON schema), so the model is constrained to return valid JSON matching
our shape. We still parse defensively and, on any failure, hand back the raw
text and a clear message rather than crashing — the user can always fall back to
entering the fields by hand.

Model: claude-opus-4-8 (Anthropic's most capable Opus-tier model). To trade some
quality for lower cost/latency you can change MODEL below to "claude-sonnet-5"
or "claude-haiku-4-5" — the prompt and schema work the same on all of them.
"""

import json

import anthropic

from . import settings_store

MODEL = "claude-opus-4-8"

# ---------------------------------------------------------------------------
# The prompt. Edit this freely to tune what the model extracts — it's plain
# English on purpose. The schema below (EXTRACTION_SCHEMA) enforces the shape;
# this prompt shapes the *judgement*.
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = """\
You are helping a business-development / partnerships job seeker in Toronto \
track and analyze job postings. You will be given a JOB DESCRIPTION and the \
seeker's RESUME.

Extract a structured summary of the job, and honestly assess fit against the \
resume. Be specific and truthful — this person relies on your assessment to \
decide where to spend their limited time, so do not flatter.

Guidance for the tricky fields:
- keywords: the skills, tools, and terms THIS employer actually cares about \
(pull from the posting, not generic filler).
- keyword_gap: the single most important field. List keywords that appear in \
the JOB DESCRIPTION but are NOT meaningfully present in the RESUME. These are \
the gaps the seeker should address. If the resume covers everything, return an \
empty list — don't invent gaps.
- match_score: 0–100. How well this specific resume fits this specific job. Be \
calibrated: a strong-but-imperfect fit is ~70–85, a stretch is ~40–60.
- match_reason: 2–3 plain sentences — what's strong, what's weak, why the score.
- visa_note: any work-authorization or sponsorship language in the JD (e.g. \
"must be legally authorized to work in Canada", "no sponsorship"). Empty string \
if none.
- For any field not stated in the posting, use an empty string (or empty list). \
Do not guess salary, location, etc. if the posting doesn't say."""

# JSON schema for structured outputs. Constrains the model to exactly this
# shape (all fields required, no extras). Keep in sync with SYSTEM_PROMPT.
EXTRACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "company": {"type": "string"},
        "position": {"type": "string"},
        "location": {"type": "string"},
        "salary": {"type": "string"},
        "seniority": {"type": "string"},
        "industry": {"type": "string"},
        "company_size": {"type": "string"},
        "keywords": {"type": "array", "items": {"type": "string"}},
        "keyword_gap": {"type": "array", "items": {"type": "string"}},
        "match_score": {"type": "integer"},
        "match_reason": {"type": "string"},
        "visa_note": {"type": "string"},
    },
    "required": [
        "company", "position", "location", "salary", "seniority", "industry",
        "company_size", "keywords", "keyword_gap", "match_score",
        "match_reason", "visa_note",
    ],
    "additionalProperties": False,
}


def _strip_fences(text: str) -> str:
    """Remove ```json ... ``` fences if the model added them anyway."""
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else t
        if t.endswith("```"):
            t = t[: -3]
    return t.strip()


def analyze(jd_text: str, resume_text: str) -> dict:
    """Run the extraction. Returns one of:
      {"ok": True, "data": {...}}                      — parsed fields
      {"ok": False, "error": "...", "raw": "<maybe>"}  — degrade gracefully
    """
    jd_text = (jd_text or "").strip()
    if not jd_text:
        return {"ok": False, "error": "There's no job text to analyze."}

    api_key = settings_store.get_api_key()
    if not api_key:
        return {
            "ok": False,
            "error": (
                "No Anthropic API key yet. Add one on the Settings page, then "
                "try again. (You can still add applications by hand without it.)"
            ),
        }

    if not resume_text.strip():
        # Not fatal — analysis still works, but keyword_gap needs the resume.
        resume_text = "(The seeker has not provided a resume yet.)"

    client = anthropic.Anthropic(api_key=api_key)

    user_content = (
        f"JOB DESCRIPTION:\n{jd_text}\n\n"
        f"RESUME:\n{resume_text}"
    )

    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=4096,
            system=SYSTEM_PROMPT,
            output_config={
                "format": {"type": "json_schema", "schema": EXTRACTION_SCHEMA},
                "effort": "medium",  # balance of quality vs. speed for extraction
            },
            messages=[{"role": "user", "content": user_content}],
        )
    except anthropic.AuthenticationError:
        return {
            "ok": False,
            "error": (
                "That API key was rejected. Double-check it on the Settings "
                "page — it should start with 'sk-ant-'."
            ),
        }
    except anthropic.RateLimitError:
        return {"ok": False, "error": "Anthropic is rate-limiting right now. Wait a moment and try again."}
    except anthropic.APIError as exc:
        return {"ok": False, "error": f"The AI service had a problem: {exc}"}
    except Exception as exc:  # noqa: BLE001 — never crash the request
        return {"ok": False, "error": f"Something went wrong reaching the AI: {exc}"}

    if response.stop_reason == "refusal":
        return {
            "ok": False,
            "error": (
                "The AI declined to analyze this text. You can still enter the "
                "details by hand."
            ),
        }

    raw = next((b.text for b in response.content if b.type == "text"), "")
    try:
        data = json.loads(_strip_fences(raw))
    except (ValueError, TypeError):
        return {
            "ok": False,
            "error": "The AI's response wasn't valid JSON. You can edit the fields by hand below.",
            "raw": raw,
        }

    # Normalize types defensively.
    try:
        data["match_score"] = int(data.get("match_score") or 0)
    except (ValueError, TypeError):
        data["match_score"] = 0
    for k in ("keywords", "keyword_gap"):
        if not isinstance(data.get(k), list):
            data[k] = []

    return {"ok": True, "data": data}
