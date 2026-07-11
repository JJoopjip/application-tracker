"""Adapter around the user's existing resume generator at
/home/joopjip/resume_generator (override with RESUME_GEN_DIR).

We do NOT modify that generator — we wrap it. Its interface (confirmed with the
user):

  * Invoked as `./resume-gen <job-description-file>` from its own directory.
  * Input is a single plain-text job-description file. All resume *content*
    comes from its own master.yaml; the JD only drives selection/tailoring.
  * It launches a headless `claude` session, then renders inside its Docker
    image, and writes a new folder:
        output/<company>-<role>-<date>/{resume.pdf, resume.docx, ...}

This adapter maps a tracker application → that JD-file input, runs the
generator, finds the freshly-created output folder, and copies resume.pdf /
resume.docx into the tracker's data/resumes/ with a stable, descriptive name.
Per the user's choice, we feed the raw saved JD unchanged (no keyword note).

Everything is best-effort and returns a structured result dict rather than
raising into the web layer, so a failure degrades to a clear on-screen message.
"""

import os
import re
import shutil
import subprocess
import tempfile
import time
from datetime import date
from pathlib import Path

from app.db import DATA_DIR

RESUME_GEN_DIR = Path(
    os.environ.get("RESUME_GEN_DIR", "/home/joopjip/resume_generator")
)
RESUMES_DIR = DATA_DIR / "resumes"

# The pipeline runs a headless Claude session + a Docker render — minutes, not
# seconds. Cap it so a wedged run can't hang forever.
TIMEOUT_SECONDS = 20 * 60


class ResumeGenError(Exception):
    """Raised for expected, user-facing failure conditions."""


def _slug(text: str, fallback: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "-", (text or "").strip()).strip("-").lower()
    return s or fallback


def preflight() -> str | None:
    """Return a user-facing reason the generator can't run, or None if it can."""
    if not RESUME_GEN_DIR.exists():
        return f"Resume generator not found at {RESUME_GEN_DIR}."
    if not (RESUME_GEN_DIR / "resume-gen").exists():
        return f"No 'resume-gen' entrypoint in {RESUME_GEN_DIR}."
    if shutil.which("docker") is None:
        return "Docker isn't available — the generator needs it to render the PDF."
    if shutil.which("claude") is None:
        return "The 'claude' command isn't on PATH — the generator needs it to tailor."
    return None


def generate(app_row) -> dict:
    """Run the generator for one application.

    Returns {"ok": True, "pdf_path": ..., "docx_path": ..., "folder": ...}
    or {"ok": False, "error": "..."}.
    """
    problem = preflight()
    if problem:
        return {"ok": False, "error": problem}

    jd_text = (app_row["jd_full_text"] or "").strip()
    if not jd_text:
        return {
            "ok": False,
            "error": (
                "This application has no saved job description to tailor against. "
                "Add the job text first (below), then try again."
            ),
        }

    output_root = RESUME_GEN_DIR / "output"
    before = _folder_snapshot(output_root)
    started = time.time()

    # Write the JD to a temp file the generator can read. It realpath()s the
    # arg, so location doesn't matter.
    with tempfile.NamedTemporaryFile(
        "w", suffix=".txt", prefix="tracker_jd_", delete=False, encoding="utf-8"
    ) as fh:
        fh.write(jd_text)
        jd_path = fh.name

    try:
        proc = subprocess.run(
            ["./resume-gen", jd_path],
            cwd=str(RESUME_GEN_DIR),
            capture_output=True,
            text=True,
            timeout=TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "error": f"The generator took longer than {TIMEOUT_SECONDS // 60} minutes and was stopped.",
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"Couldn't start the generator: {exc}"}
    finally:
        try:
            os.unlink(jd_path)
        except OSError:
            pass

    # Find the folder created by this run.
    new_folder = _newest_new_folder(output_root, before, started)
    if new_folder is None:
        tail = (proc.stderr or proc.stdout or "").strip()[-500:]
        return {
            "ok": False,
            "error": (
                "The generator finished but produced no output folder. "
                + (f"Last output: …{tail}" if tail else "No details available.")
            ),
        }

    src_pdf = new_folder / "resume.pdf"
    src_docx = new_folder / "resume.docx"
    if not src_pdf.exists() and not src_docx.exists():
        return {
            "ok": False,
            "error": f"The run finished but no resume files were found in {new_folder.name}.",
        }

    # Copy into the tracker, named descriptively and uniquely.
    RESUMES_DIR.mkdir(parents=True, exist_ok=True)
    company = _slug(app_row["company"], "company")
    position = _slug(app_row["position"], "role")
    stamp = date.today().isoformat()
    base = f"{company}_{position}_{stamp}"
    # Avoid clobbering an earlier same-day version.
    base = _unique_base(RESUMES_DIR, base)

    pdf_dest = docx_dest = None
    if src_pdf.exists():
        pdf_dest = RESUMES_DIR / f"{base}.pdf"
        shutil.copy2(src_pdf, pdf_dest)
    if src_docx.exists():
        docx_dest = RESUMES_DIR / f"{base}.docx"
        shutil.copy2(src_docx, docx_dest)

    return {
        "ok": True,
        "pdf_path": str(pdf_dest) if pdf_dest else None,
        "docx_path": str(docx_dest) if docx_dest else None,
        "folder": str(new_folder),
    }


# --- helpers ---------------------------------------------------------------
def _folder_snapshot(output_root: Path) -> set:
    if not output_root.exists():
        return set()
    return {p.name for p in output_root.iterdir() if p.is_dir()}


def _newest_new_folder(output_root: Path, before: set, started: float):
    """The output/ subfolder created by this run (new since `before`, or the
    most recently modified if the name pre-existed)."""
    if not output_root.exists():
        return None
    dirs = [p for p in output_root.iterdir() if p.is_dir()]
    fresh = [p for p in dirs if p.name not in before]
    candidates = fresh or [
        p for p in dirs if p.stat().st_mtime >= started - 2
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def _unique_base(directory: Path, base: str) -> str:
    if not (directory / f"{base}.pdf").exists() and not (directory / f"{base}.docx").exists():
        return base
    n = 2
    while (directory / f"{base}-v{n}.pdf").exists() or (directory / f"{base}-v{n}.docx").exists():
        n += 1
    return f"{base}-v{n}"
