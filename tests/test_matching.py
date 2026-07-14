"""Tests for app/matching.py — email-to-application matching and the
classification -> proposed-status map."""

from app import matching

from .conftest import make_row


# --- _norm -----------------------------------------------------------------
def test_norm_strips_suffixes_and_punctuation():
    assert matching._norm("Acme Inc.") == "acme"
    assert matching._norm("The Foo Group") == "foo"
    assert matching._norm("Bar Technologies, LLC") == "bar"


def test_norm_handles_none_and_empty():
    assert matching._norm(None) == ""
    assert matching._norm("   ") == ""


# --- proposed_status -------------------------------------------------------
def test_proposed_status_mapping():
    assert matching.proposed_status("rejection") == "rejected"
    assert matching.proposed_status("interview_invite") == "interview"
    assert matching.proposed_status("assessment") == "screening"
    assert matching.proposed_status("recruiter_outreach") is None
    assert matching.proposed_status("not_job_related") is None
    assert matching.proposed_status("unknown_label") is None


# --- find_match ------------------------------------------------------------
def _patch_apps(monkeypatch, apps):
    monkeypatch.setattr(matching.db, "all_applications", lambda: apps)


def test_find_match_by_thread_id_wins_first(monkeypatch):
    apps = [
        make_row(id=10, email_thread_id="thread-abc", contact_email="x@y.com"),
        make_row(id=11, company="Acme"),
    ]
    _patch_apps(monkeypatch, apps)
    assert matching.find_match("thread-abc", "someone@else.com", "Acme") == 10


def test_find_match_by_contact_email(monkeypatch):
    apps = [make_row(id=20, contact_email="Recruiter@Acme.com", company="Zzz")]
    _patch_apps(monkeypatch, apps)
    # case-insensitive, no thread link
    assert matching.find_match("", "recruiter@acme.com", "Nope") == 20


def test_find_match_fuzzy_company(monkeypatch):
    apps = [make_row(id=30, company="Acme")]
    _patch_apps(monkeypatch, apps)
    assert matching.find_match("", "", "Acme Technologies") == 30


def test_find_match_returns_none_below_threshold(monkeypatch):
    apps = [make_row(id=40, company="Acme")]
    _patch_apps(monkeypatch, apps)
    assert matching.find_match("", "", "Globex Corporation") is None


def test_find_match_none_when_no_signals(monkeypatch):
    _patch_apps(monkeypatch, [make_row(id=50, company="Acme")])
    assert matching.find_match("", "", "") is None


# --- find_possible_duplicate ----------------------------------------------
def test_duplicate_same_company_and_similar_title(monkeypatch):
    apps = [make_row(id=60, company="Acme Inc.", position="Software Engineer")]
    _patch_apps(monkeypatch, apps)
    dup = matching.find_possible_duplicate("Acme", "Sr. Software Engineer")
    assert dup is not None and dup["id"] == 60


def test_duplicate_different_title_same_company_is_not_flagged(monkeypatch):
    apps = [make_row(id=61, company="Acme", position="Software Engineer")]
    _patch_apps(monkeypatch, apps)
    assert matching.find_possible_duplicate("Acme", "Account Executive") is None


def test_duplicate_different_company_is_not_flagged(monkeypatch):
    apps = [make_row(id=62, company="Globex", position="Software Engineer")]
    _patch_apps(monkeypatch, apps)
    assert matching.find_possible_duplicate("Acme", "Software Engineer") is None


def test_duplicate_excludes_self(monkeypatch):
    apps = [make_row(id=63, company="Acme", position="Engineer")]
    _patch_apps(monkeypatch, apps)
    assert matching.find_possible_duplicate("Acme", "Engineer", exclude_id=63) is None


def test_duplicate_blank_title_collapses_to_same(monkeypatch):
    apps = [make_row(id=64, company="Acme", position="")]
    _patch_apps(monkeypatch, apps)
    dup = matching.find_possible_duplicate("Acme", "Anything")
    assert dup is not None and dup["id"] == 64


def test_duplicate_none_when_company_blank(monkeypatch):
    _patch_apps(monkeypatch, [make_row(id=65, company="Acme")])
    assert matching.find_possible_duplicate("", "Engineer") is None
