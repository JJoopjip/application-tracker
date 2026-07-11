"""Read/write the two things Phase 2 needs from disk:

  1. The Anthropic API key — stored in a `.env` file in the project root
     (git-ignored). Editable from the in-browser Settings page so a
     non-technical user never has to touch a text editor.
  2. The user's resume — stored as plain text at data/my_resume.txt, also
     editable in-browser.

Nothing here calls the network; it's just file access.
"""

from pathlib import Path

from .db import DATA_DIR, ROOT

ENV_PATH = ROOT / ".env"
RESUME_PATH = DATA_DIR / "my_resume.txt"
KEY_NAME = "ANTHROPIC_API_KEY"


# --- API key ---------------------------------------------------------------
def get_api_key() -> str | None:
    """Return the saved key, or None. Reads the .env file directly so a key
    saved from the Settings page is picked up without a server restart."""
    if not ENV_PATH.exists():
        return None
    for line in ENV_PATH.read_text().splitlines():
        line = line.strip()
        if line.startswith(f"{KEY_NAME}=") and not line.startswith("#"):
            value = line.split("=", 1)[1].strip().strip('"').strip("'")
            return value or None
    return None


def set_api_key(key: str) -> None:
    """Write (or replace) the API key line in .env, leaving other lines intact."""
    key = (key or "").strip()
    lines = ENV_PATH.read_text().splitlines() if ENV_PATH.exists() else []
    out = [ln for ln in lines if not ln.strip().startswith(f"{KEY_NAME}=")]
    out.append(f'{KEY_NAME}={key}')
    ENV_PATH.write_text("\n".join(out) + "\n")


def has_api_key() -> bool:
    return bool(get_api_key())


def masked_api_key() -> str:
    """A safe-to-display hint like 'sk-ant-…â€¦4f2a' — never the full key."""
    key = get_api_key()
    if not key:
        return ""
    if len(key) <= 12:
        return "•" * len(key)
    return f"{key[:7]}…{key[-4:]}"


# --- Resume ----------------------------------------------------------------
def get_resume() -> str:
    if RESUME_PATH.exists():
        return RESUME_PATH.read_text()
    return ""


def set_resume(text: str) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    RESUME_PATH.write_text(text or "")


def has_resume() -> bool:
    return bool(get_resume().strip())
