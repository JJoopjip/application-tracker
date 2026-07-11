"""Fetch a job posting from a URL and extract its main text.

Many job boards (LinkedIn, Workday, Greenhouse-in-an-iframe) render the posting
with JavaScript, so a plain HTTP fetch returns an empty shell. When that
happens we fail *clearly* and tell the user to paste the text instead — we never
hand garbage to the AI.
"""

import httpx
from bs4 import BeautifulSoup

# Pretend to be a normal browser; some sites 403 an unknown client.
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

# Below this many characters of extracted text, assume the page was JS-only
# (or blocked) and didn't actually give us the posting.
_MIN_TEXT = 400


def fetch_job_text(url: str) -> dict:
    """Return {"ok": True, "text": ...} or {"ok": False, "error": ...}."""
    url = (url or "").strip()
    if not url:
        return {"ok": False, "error": "No URL provided."}
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    try:
        resp = httpx.get(
            url, headers=_HEADERS, timeout=15.0, follow_redirects=True
        )
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        return {
            "ok": False,
            "error": (
                f"The page returned an error ({exc.response.status_code}). "
                "It may require a login. Please paste the job text instead."
            ),
        }
    except httpx.HTTPError:
        return {
            "ok": False,
            "error": (
                "Couldn't reach that page. Check the link, or just paste the "
                "job text into the box instead."
            ),
        }

    soup = BeautifulSoup(resp.text, "html.parser")

    # Drop chrome that isn't the posting.
    for tag in soup(
        ["script", "style", "nav", "footer", "header", "aside", "form", "noscript"]
    ):
        tag.decompose()

    # Prefer a <main> / <article> if present; else the whole body.
    main = soup.find("main") or soup.find("article") or soup.body or soup
    text = main.get_text(separator="\n", strip=True)
    # Collapse runs of blank lines.
    text = "\n".join(line for line in text.splitlines() if line.strip())

    if len(text) < _MIN_TEXT:
        return {
            "ok": False,
            "error": (
                "This page didn't return readable text — sites like LinkedIn "
                "and Workday load their content with JavaScript, which this "
                "tool can't see. Please copy the job description and paste it "
                "into the box instead."
            ),
        }

    return {"ok": True, "text": text}
