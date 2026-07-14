"""Import a finished résumé + cover letter *from* the resume generator into the
tracker (the reverse of integrations/resume_gen.py, which pushes the other way).

The generator's web UI has an "Add to tracker" button. It hands us the absolute
path of one output/<slug>/ folder it just produced. Because both apps share this
host's filesystem, we don't receive uploads — we read that folder directly and
copy its resume.pdf/docx + cover_letter.pdf/docx into the tracker's
data/resumes/, exactly as the existing integration copies generator output.

Flow (per the user's choices):
  * Company/position come from the tracker's own AI extraction over the JD
    (ai.analyze) — falling back to the folder name if the AI backend is absent.
  * If an application for the same company+role already exists, we attach a new
    résumé version to it (history preserved) rather than creating a duplicate.

Best-effort: returns a structured dict; never raises into the web layer.
"""

import shutil
from datetime import date
from difflib import SequenceMatcher
from pathlib import Path

from app import ai, db, matching, settings_store

# Reuse the naming/dedup file helpers the other direction already established.
from integrations.resume_gen import (
    RESUME_GEN_DIR,
    RESUMES_DIR,
    _slug,
    _unique_base,
)

# A same-company, same-role application counts as "the same posting" and gets a
# new résumé version instead of a new row.
_MATCH_THRESHOLD = 0.82


def _norm_position(text: str) -> str:
    return " ".join((text or "").lower().split())


def _find_existing(company: str, position: str) -> int | None:
    """An application id for this company+role, or None. Company match reuses the
    tracker's own fuzzy normalizer; position is matched fuzzily too so 'Sr. Data
    Analyst' and 'Senior Data Analyst' collapse, but two different roles at the
    same employer stay separate."""
    tgt_co = matching._norm(company)
    tgt_pos = _norm_position(position)
    if not tgt_co:
        return None
    for a in db.all_applications():
        if SequenceMatcher(None, tgt_co, matching._norm(a["company"])).ratio() < _MATCH_THRESHOLD:
            continue
        if SequenceMatcher(None, tgt_pos, _norm_position(a["position"])).ratio() >= _MATCH_THRESHOLD:
            return a["id"]
    return None


def import_folder(folder: str, jd_text: str | None = None) -> dict:
    """Import one generator output folder. Returns
    {"ok": True, "app_id": int, "url": str, "created": bool, "warning": str|None}
    or {"ok": False, "error": "..."}.
    """
    src = Path(folder or "").resolve()

    # Containment guard: only fold in folders under the generator's output/ tree.
    output_root = (RESUME_GEN_DIR / "output").resolve()
    if output_root not in src.parents or not src.is_dir():
        return {"ok": False, "error": f"Not a generator output folder: {folder}"}

    pdf = src / "resume.pdf"
    docx = src / "resume.docx"
    if not pdf.exists() and not docx.exists():
        return {"ok": False, "error": f"No resume.pdf/.docx in {src.name}."}

    # Fall back to the JD saved alongside the resume if none was passed.
    if not (jd_text or "").strip():
        jd_file = src / "job_description.txt"
        jd_text = jd_file.read_text(encoding="utf-8") if jd_file.exists() else ""
    jd_text = (jd_text or "").strip()

    # --- company/position via the tracker's AI (folder name as fallback) ------
    warning = None
    extracted = {}
    if jd_text:
        result = ai.analyze(jd_text, settings_store.get_resume())
        if result.get("ok"):
            extracted = result["data"]
        else:
            warning = f"AI extraction unavailable ({result.get('error')}); used the folder name."
    else:
        warning = "No job description found; used the folder name for company/role."

    company = (extracted.get("company") or "").strip()
    position = (extracted.get("position") or "").strip()
    if not company or not position:
        # Derive from '<company>-<role>-<date>' — strip the trailing ISO date.
        stem = src.name.rsplit("-", 3)[0] if src.name[-10:].count("-") == 2 else src.name
        guess = stem.replace("-", " ").title()
        company = company or guess
        position = position or guess

    # --- create the application, or reuse a matching one ----------------------
    existing = _find_existing(company, position)
    if existing is not None:
        app_id, created = existing, False
    else:
        data = {
            "company": company,
            "position": position,
            "location": extracted.get("location"),
            "salary": extracted.get("salary"),
            "seniority": extracted.get("seniority"),
            "industry": extracted.get("industry"),
            "company_size": extracted.get("company_size"),
            "jd_full_text": jd_text or None,
            "keywords": _json_or_none(extracted.get("keywords")),
            "keyword_gap": _json_or_none(extracted.get("keyword_gap")),
            "match_score": extracted.get("match_score"),
            "match_reason": extracted.get("match_reason"),
            "status": "wishlist",
        }
        app_id = db.create_application(data)
        created = True

    # --- copy the files in and record a résumé version ------------------------
    RESUMES_DIR.mkdir(parents=True, exist_ok=True)
    base = _unique_base(
        RESUMES_DIR,
        f"{_slug(company, 'company')}_{_slug(position, 'role')}_{date.today().isoformat()}",
    )
    pdf_dest = docx_dest = None
    if pdf.exists():
        pdf_dest = RESUMES_DIR / f"{base}.pdf"
        shutil.copy2(pdf, pdf_dest)
    if docx.exists():
        docx_dest = RESUMES_DIR / f"{base}.docx"
        shutil.copy2(docx, docx_dest)
    db.add_resume_version(
        app_id, str(pdf_dest) if pdf_dest else None,
        str(docx_dest) if docx_dest else None, str(src))

    # Cover letter (if the run produced one): copy + point the application at it.
    cover_pdf = src / "cover_letter.pdf"
    if cover_pdf.exists():
        cover_dest = RESUMES_DIR / f"{base}_cover.pdf"
        shutil.copy2(cover_pdf, cover_dest)
        cover_docx = src / "cover_letter.docx"
        if cover_docx.exists():
            shutil.copy2(cover_docx, RESUMES_DIR / f"{base}_cover.docx")
        db.set_fields(app_id, cover_letter_path=str(cover_dest))

    return {
        "ok": True,
        "app_id": app_id,
        "url": f"/applications/{app_id}",
        "created": created,
        "warning": warning,
    }


def _json_or_none(value):
    import json
    if not value:
        return None
    return json.dumps(value)
