"""Read-only Gmail access for the review queue.

Scope is strictly `gmail.readonly` — this app can read messages and nothing
else. It never sends, deletes, or modifies your mail.

Auth flow (one time, see README):
  credentials.json  → you download this from Google Cloud (OAuth desktop app)
  token.json        → created on first connect, caches your authorization

We keep the OAuth dance out of the request path except for a single, explicit
"Connect Gmail" action that opens your browser.
"""

import base64
import re

from .db import ROOT

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
CREDENTIALS_PATH = ROOT / "credentials.json"
TOKEN_PATH = ROOT / "token.json"


def status() -> str:
    """One of: 'no_credentials', 'needs_auth', 'connected'."""
    if not CREDENTIALS_PATH.exists():
        return "no_credentials"
    if not TOKEN_PATH.exists():
        return "needs_auth"
    return "connected"


def _load_creds():
    """Load cached creds, refreshing if needed. Returns creds or None."""
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    if not TOKEN_PATH.exists():
        return None
    creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            TOKEN_PATH.write_text(creds.to_json())
        except Exception:  # noqa: BLE001 — treat as needs re-auth
            return None
    return creds if creds and creds.valid else None


def connect() -> dict:
    """Run the OAuth flow (opens a browser) and cache token.json.
    Returns {"ok": True} or {"ok": False, "error": ...}. Blocks until the user
    authorizes in the browser — it's an explicit, one-time action."""
    if not CREDENTIALS_PATH.exists():
        return {
            "ok": False,
            "error": "credentials.json is missing. Follow the Gmail setup steps in the README first.",
        }
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow

        flow = InstalledAppFlow.from_client_secrets_file(
            str(CREDENTIALS_PATH), SCOPES
        )
        # port=0 picks a free port; opens the system browser to authorize.
        creds = flow.run_local_server(port=0, open_browser=True)
        TOKEN_PATH.write_text(creds.to_json())
        return {"ok": True}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"Couldn't complete Google sign-in: {exc}"}


def _service():
    from googleapiclient.discovery import build

    creds = _load_creds()
    if creds is None:
        return None
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


# --- Message parsing -------------------------------------------------------
def _header(headers: list, name: str) -> str:
    for h in headers:
        if h.get("name", "").lower() == name.lower():
            return h.get("value", "")
    return ""


def _split_from(value: str) -> tuple[str, str]:
    """'Jane Doe <jane@co.com>' -> ('Jane Doe', 'jane@co.com')."""
    m = re.match(r"\s*(.*?)\s*<([^>]+)>", value)
    if m:
        name = m.group(1).strip().strip('"')
        return name, m.group(2).strip().lower()
    return "", value.strip().lower()


def _decode(data: str) -> str:
    try:
        return base64.urlsafe_b64decode(data.encode("utf-8")).decode(
            "utf-8", errors="replace"
        )
    except Exception:  # noqa: BLE001
        return ""


def _extract_body(payload: dict) -> str:
    """Walk the MIME tree for text/plain (falling back to text/html stripped)."""
    plain, html = [], []

    def walk(part):
        mime = part.get("mimeType", "")
        body = part.get("body", {})
        data = body.get("data")
        if mime == "text/plain" and data:
            plain.append(_decode(data))
        elif mime == "text/html" and data:
            html.append(_decode(data))
        for sub in part.get("parts", []) or []:
            walk(sub)

    walk(payload)
    if plain:
        return "\n".join(plain)
    if html:
        text = re.sub(r"<[^>]+>", " ", "\n".join(html))
        return re.sub(r"\s+\n", "\n", text)
    return ""


def fetch_recent(days: int = 14, max_messages: int = 60) -> dict:
    """Return {"ok": True, "messages": [ {...}, ... ]} or {"ok": False, "error"}.

    Each message dict: id, thread_id, sender_name, sender_email, subject,
    snippet, body (truncated).
    """
    service = _service()
    if service is None:
        return {"ok": False, "error": "Gmail isn't connected. Connect it on the Settings page first."}

    try:
        query = f"newer_than:{int(days)}d in:inbox"
        ids, page_token = [], None
        while len(ids) < max_messages:
            resp = (
                service.users()
                .messages()
                .list(userId="me", q=query, pageToken=page_token, maxResults=50)
                .execute()
            )
            ids.extend(m["id"] for m in resp.get("messages", []))
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
        ids = ids[:max_messages]

        out = []
        for mid in ids:
            msg = (
                service.users()
                .messages()
                .get(userId="me", id=mid, format="full")
                .execute()
            )
            payload = msg.get("payload", {})
            headers = payload.get("headers", [])
            name, email = _split_from(_header(headers, "From"))
            body = _extract_body(payload)[:4000]
            out.append(
                {
                    "id": msg["id"],
                    "thread_id": msg.get("threadId", ""),
                    "sender_name": name,
                    "sender_email": email,
                    "subject": _header(headers, "Subject"),
                    "snippet": msg.get("snippet", ""),
                    "body": body,
                }
            )
        return {"ok": True, "messages": out}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"Couldn't read your inbox: {exc}"}
