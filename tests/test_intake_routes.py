"""Route-level test for the intake duplicate warning: seed an application, stub
the AI to propose the same role, and confirm the review screen flags it."""

import pytest
from fastapi.testclient import TestClient

from app import db, main


@pytest.fixture
def client(tmp_path, monkeypatch):
    """A TestClient backed by a throwaway DB, so tests never touch real data."""
    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    db.init_db()
    with TestClient(main.app) as c:
        yield c


def _stub_ai(monkeypatch, company, position):
    monkeypatch.setattr(main.ai, "analyze", lambda jd, resume: {
        "ok": True,
        "data": {
            "company": company, "position": position, "match_score": 70,
            "keywords": [], "keyword_gap": [], "match_reason": "", "visa_note": "",
        },
    })


def test_intake_flags_duplicate(client, monkeypatch):
    existing = db.create_application(
        {"company": "Acme Inc.", "position": "Software Engineer", "status": "applied"}
    )
    _stub_ai(monkeypatch, "Acme", "Sr. Software Engineer")

    r = client.post("/intake/analyze", data={"jd_text": "Acme senior engineer role"})
    assert r.status_code == 200
    assert "already have a card" in r.text
    assert f'/applications/{existing}"' in r.text


def test_intake_no_warning_for_new_role(client, monkeypatch):
    db.create_application(
        {"company": "Acme", "position": "Software Engineer", "status": "applied"}
    )
    _stub_ai(monkeypatch, "Globex", "Account Executive")

    r = client.post("/intake/analyze", data={"jd_text": "Globex sales role"})
    assert r.status_code == 200
    assert "already have a card" not in r.text
