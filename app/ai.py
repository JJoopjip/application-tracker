"""The one AI call in Phase 2: read a job description + the user's resume and
extract a structured application record — including the keyword_gap, which is
the whole point of this feature.

Two backends, picked automatically (see `_generate_structured`):
  1. Anthropic API — used when an API key is saved on the Settings page. Uses
     **structured outputs** (`output_config.format` with a JSON schema) so the
     model is constrained to valid JSON.
  2. Claude CLI (`claude -p`, headless) — used when there's no API key, so the
     app runs off your existing Claude sign-in with no metered API billing. The
     CLI can't hard-constrain the schema, so we embed the schema in the prompt,
     validate the reply against it (jsonschema), and do one repair round if it
     doesn't match.

Either way we parse defensively, **validate against the JSON schema**, and on
any failure hand back the raw text and a clear message rather than crashing —
the user can always fall back to entering the fields by hand.

Model: claude-opus-4-8 (Anthropic's most capable Opus-tier model). To trade some
quality for lower cost/latency you can change MODEL below to "claude-sonnet-5"
or "claude-haiku-4-5" — the prompt and schema work the same on all of them, and
on both backends.
"""

import json
import logging
import shutil
import subprocess

import anthropic
import jsonschema

from . import settings_store

logger = logging.getLogger(__name__)

MODEL = "claude-opus-4-8"

# Headless Claude CLI backend. Used only when no API key is set.
CLI_BIN = "claude"
CLI_TIMEOUT = 180  # seconds — an Opus analysis can take a while

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


def _extract_json(text: str) -> str:
    """Pull the JSON object out of a model reply, tolerating fences or stray
    prose around it. Grabs the outermost {...} span if present."""
    t = _strip_fences(text)
    start, end = t.find("{"), t.rfind("}")
    if start != -1 and end > start:
        return t[start : end + 1]
    return t


def _parse_and_validate(raw: str, schema: dict):
    """Parse `raw` as JSON and validate it against `schema`.
    Returns the dict, or raises ValueError / jsonschema.ValidationError."""
    data = json.loads(_extract_json(raw))
    jsonschema.validate(data, schema)
    return data


# ---------------------------------------------------------------------------
# Backend 1: the Anthropic API (schema-constrained structured outputs).
# ---------------------------------------------------------------------------
def _api_structured(api_key, system_prompt, user_content, schema, model, effort, max_tokens):
    client = anthropic.Anthropic(api_key=api_key)
    # Structured outputs constrain the reply to the schema. `effort` is only
    # sent when requested AND supported — Haiku 4.5 (used for email triage) has
    # no effort/thinking control and rejects the parameter with a 400.
    output_config = {"format": {"type": "json_schema", "schema": schema}}
    if effort:
        output_config["effort"] = effort
    try:
        response = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system_prompt,
            output_config=output_config,
            messages=[{"role": "user", "content": user_content}],
        )
    except anthropic.AuthenticationError:
        return {"ok": False, "error": (
            "That API key was rejected. Double-check it on the Settings page — "
            "it should start with 'sk-ant-'.")}
    except anthropic.RateLimitError:
        return {"ok": False, "error": "Anthropic is rate-limiting right now. Wait a moment and try again."}
    except anthropic.APIError as exc:
        return {"ok": False, "error": f"The AI service had a problem: {exc}"}
    except Exception as exc:  # noqa: BLE001 — never crash the request
        return {"ok": False, "error": f"Something went wrong reaching the AI: {exc}"}

    usage = getattr(response, "usage", None)
    if usage is not None:
        logger.info(
            "AI call [api] model=%s input_tokens=%s output_tokens=%s",
            model, usage.input_tokens, usage.output_tokens,
        )

    if response.stop_reason == "refusal":
        return {"ok": False, "error": (
            "The AI declined to handle this text. You can still enter the "
            "details by hand.")}

    raw = next((b.text for b in response.content if b.type == "text"), "")
    try:
        data = _parse_and_validate(raw, schema)
    except (ValueError, TypeError, jsonschema.ValidationError):
        return {"ok": False, "raw": raw, "error": (
            "The AI's response wasn't valid JSON. You can edit the fields by "
            "hand below.")}
    return {"ok": True, "data": data}


# ---------------------------------------------------------------------------
# Backend 2: the headless Claude CLI (`claude -p`). No API key required.
# ---------------------------------------------------------------------------
def _cli_available() -> bool:
    return shutil.which(CLI_BIN) is not None


def _cli_generate(prompt: str, model: str) -> str:
    """Run the headless CLI and return the model's text (the `result` field of
    the JSON envelope). Raises RuntimeError with a user-readable message."""
    try:
        proc = subprocess.run(
            [CLI_BIN, "-p", prompt, "--output-format", "json", "--model", model],
            capture_output=True, text=True, timeout=CLI_TIMEOUT,
        )
    except FileNotFoundError:
        raise RuntimeError("The Claude CLI isn't installed or on PATH.")
    except subprocess.TimeoutExpired:
        raise RuntimeError("The Claude CLI took too long to respond. Try again.")

    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeError(f"The Claude CLI errored: {detail[:300] or 'unknown error'}")
    try:
        env = json.loads(proc.stdout)
    except (ValueError, TypeError):
        raise RuntimeError("The Claude CLI returned output we couldn't parse.")
    if env.get("is_error"):
        raise RuntimeError(str(env.get("result") or "The Claude CLI reported an error."))
    usage = env.get("usage")
    if usage:
        logger.info(
            "AI call [cli] model=%s input_tokens=%s output_tokens=%s cost_usd=%s",
            model, usage.get("input_tokens"), usage.get("output_tokens"),
            env.get("total_cost_usd"),
        )
    return env.get("result") or ""


def _cli_prompt(system_prompt, user_content, schema) -> str:
    """Build a single prompt that embeds the schema and demands raw JSON."""
    return (
        f"{system_prompt}\n\n"
        "Respond with ONLY a single JSON object and nothing else — no prose, no "
        "explanation, no markdown code fences. It must validate against this "
        "JSON schema exactly (every required key present, correct types):\n"
        f"{json.dumps(schema)}\n\n"
        f"{user_content}"
    )


def _cli_repair_prompt(system_prompt, schema, bad_reply, problem) -> str:
    """Ask the model to fix a reply that failed schema validation."""
    return (
        f"{system_prompt}\n\n"
        "Your previous reply did NOT satisfy the required schema.\n"
        f"Validation problem: {problem}\n\n"
        "Return a corrected answer: ONLY the JSON object, no prose, no fences, "
        "validating exactly against this schema:\n"
        f"{json.dumps(schema)}\n\n"
        f"Your previous reply was:\n{bad_reply}"
    )


def _cli_structured(system_prompt, user_content, schema, model):
    """CLI path with schema validation and one repair round."""
    prompt = _cli_prompt(system_prompt, user_content, schema)
    raw = ""
    for attempt in range(2):  # first shot, then a single repair attempt
        try:
            raw = _cli_generate(prompt, model)
        except RuntimeError as exc:
            return {"ok": False, "error": str(exc)}
        try:
            data = _parse_and_validate(raw, schema)
            return {"ok": True, "data": data}
        except (ValueError, TypeError, jsonschema.ValidationError) as exc:
            # Feed the exact failure back and try once more; if this was already
            # the repair round, fall through to graceful degradation.
            prompt = _cli_repair_prompt(system_prompt, schema, raw, exc)

    return {"ok": False, "raw": raw, "error": (
        "The AI's response didn't match the expected format. You can edit the "
        "fields by hand below.")}


# ---------------------------------------------------------------------------
# Dispatch: API when a key is set, else the headless CLI, else a clear error.
# ---------------------------------------------------------------------------
def _generate_structured(system_prompt, user_content, schema, model,
                         effort="medium", max_tokens=4096):
    api_key = settings_store.get_api_key()
    if api_key:
        return _api_structured(
            api_key, system_prompt, user_content, schema, model, effort, max_tokens)
    if _cli_available():
        return _cli_structured(system_prompt, user_content, schema, model)
    return {"ok": False, "error": (
        "No AI backend available. Either add an Anthropic API key on the "
        "Settings page, or install the Claude CLI (`claude`) and sign in.")}


def analyze(jd_text: str, resume_text: str) -> dict:
    """Run the extraction. Returns one of:
      {"ok": True, "data": {...}}                      — parsed fields
      {"ok": False, "error": "...", "raw": "<maybe>"}  — degrade gracefully
    """
    jd_text = (jd_text or "").strip()
    if not jd_text:
        return {"ok": False, "error": "There's no job text to analyze."}

    if not resume_text.strip():
        # Not fatal — analysis still works, but keyword_gap needs the resume.
        resume_text = "(The seeker has not provided a resume yet.)"

    user_content = (
        f"JOB DESCRIPTION:\n{jd_text}\n\n"
        f"RESUME:\n{resume_text}"
    )

    result = _generate_structured(
        SYSTEM_PROMPT, user_content, EXTRACTION_SCHEMA, MODEL, effort="medium")
    if not result["ok"]:
        return result
    data = result["data"]

    # Normalize types defensively.
    try:
        data["match_score"] = int(data.get("match_score") or 0)
    except (ValueError, TypeError):
        data["match_score"] = 0
    for k in ("keywords", "keyword_gap"):
        if not isinstance(data.get(k), list):
            data[k] = []

    return {"ok": True, "data": data}


# ===========================================================================
# Phase 4 — classify an inbox email about a job application.
# Model is claude-haiku-4-5: this is a simple 7-way classification and every
# result is shown to the user for review before it changes anything (see
# review_confirm), so a misclassification is caught, not acted on. Haiku is the
# right tier for this high-volume, low-effort task — ~5x cheaper than Opus.
# Bump EMAIL_MODEL to claude-sonnet-5 or claude-opus-4-8 if you want more
# accuracy at higher cost. (Haiku has no effort control — see classify_email.)
# ===========================================================================
EMAIL_MODEL = "claude-haiku-4-5"

EMAIL_CLASSES = [
    "rejection",
    "interview_invite",
    "screening_request",
    "assessment",       # online assessment / take-home / OA
    "offer",
    "recruiter_outreach",
    "not_job_related",
]

# Comment this prompt freely to tune classification. The guardrail that matters
# most: DO NOT over-call "rejection" — "we're moving forward with others for
# this role, but we'll keep you in mind" is common and is NOT a plain rejection
# of the person; only classify a clear no as a rejection.
EMAIL_SYSTEM_PROMPT = """\
You classify emails for a job seeker's application tracker. You are given one \
email (sender, subject, body). Decide which single category it fits, and \
extract a few facts. Be conservative and precise — the user reviews every call \
before anything changes, and false positives waste their time.

Categories:
- rejection: a clear "no" for the person for a specific role.
- interview_invite: an invitation to interview / schedule a call with the team.
- screening_request: a recruiter/HR asking to schedule an initial screen or \
phone chat, or asking screening questions.
- assessment: an online assessment, coding test, take-home, or case study to \
complete.
- offer: a job offer or offer-related logistics.
- recruiter_outreach: a recruiter proactively reaching out about a role the \
seeker did NOT already apply to (inbound sourcing).
- not_job_related: newsletters, marketing, personal mail, anything not about a \
specific application or opportunity.

Important nuances:
- "We're moving forward with other candidates FOR THIS ROLE but will keep your \
resume on file / encourage you to apply to other roles" is a rejection of that \
application. But a generic talent-community / keep-in-touch blast is \
not_job_related.
- If it's ambiguous, prefer the lower-commitment category and lower confidence.

Extract:
- company: the employer the email is about (not the email provider). Empty if unclear.
- role: the specific position mentioned, if any. Empty otherwise.
- dates: any dates, times, or deadlines mentioned (free text). Empty if none.
- notable_detail: one short phrase worth remembering — e.g. "invited to reapply \
in 6 months", "assessment due Friday", "salary band $90-110k". Empty if none.
- confidence: 0-100, how sure you are of the category.
- reasoning: one sentence explaining the classification."""

EMAIL_SCHEMA = {
    "type": "object",
    "properties": {
        "classification": {"type": "string", "enum": EMAIL_CLASSES},
        "confidence": {"type": "integer"},
        "company": {"type": "string"},
        "role": {"type": "string"},
        "dates": {"type": "string"},
        "notable_detail": {"type": "string"},
        "reasoning": {"type": "string"},
    },
    "required": [
        "classification", "confidence", "company", "role", "dates",
        "notable_detail", "reasoning",
    ],
    "additionalProperties": False,
}


def classify_email(email: dict) -> dict:
    """Classify one email. Returns {"ok": True, "data": {...}} or
    {"ok": False, "error": ...}. `email` has sender_name/sender_email/subject/body.
    Uses the API when a key is set, otherwise the headless Claude CLI."""
    content = (
        f"From: {email.get('sender_name','')} <{email.get('sender_email','')}>\n"
        f"Subject: {email.get('subject','')}\n\n"
        f"{email.get('body','') or email.get('snippet','')}"
    )
    result = _generate_structured(
        EMAIL_SYSTEM_PROMPT, content, EMAIL_SCHEMA, EMAIL_MODEL,
        effort=None, max_tokens=1024)  # Haiku: no effort param; fast/cheap
    if not result["ok"]:
        return result
    data = result["data"]

    try:
        data["confidence"] = int(data.get("confidence") or 0)
    except (ValueError, TypeError):
        data["confidence"] = 0
    if data.get("classification") not in EMAIL_CLASSES:
        data["classification"] = "not_job_related"
    return {"ok": True, "data": data}
