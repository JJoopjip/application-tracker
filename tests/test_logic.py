"""Tests for the derived views in app/logic.py — the stats strip, the
needs-attention rule, the Today sentence, and the small formatting helpers."""

from datetime import date

from app import logic

from .conftest import date_in, days_ago_iso, make_row


# --- compute_stats ---------------------------------------------------------
def test_compute_stats_counts_and_response_rate():
    rows = [
        make_row(status="wishlist"),
        make_row(status="applied"),
        make_row(status="screening"),
        make_row(status="interview"),
        make_row(status="final"),
        make_row(status="offer"),
        make_row(status="rejected"),
        make_row(status="ghosted"),
        make_row(status="withdrawn"),
    ]
    stats = logic.compute_stats(rows)
    assert stats["active"] == 5          # applied, screening, interview, final, offer
    assert stats["applied"] == 7         # the five active + rejected + ghosted
    assert stats["interviews"] == 3      # interview, final, offer
    assert stats["offers"] == 1
    # responded (screening..offer) = 4 over applied denominator 7 -> 57%
    assert stats["response_rate"] == 57


def test_compute_stats_empty_has_zero_response_rate():
    assert logic.compute_stats([])["response_rate"] == 0


# --- needs_attention -------------------------------------------------------
def test_due_today_and_overdue_need_attention():
    due_today = make_row(status="applied", next_action_due=date_in(0))
    overdue = make_row(status="wishlist", next_action_due=date_in(-3))
    out = logic.needs_attention([due_today, overdue])
    assert len(out) == 2


def test_future_due_date_does_not_need_attention():
    row = make_row(status="applied", next_action_due=date_in(5))
    assert logic.needs_attention([row]) == []


def test_stale_active_application_needs_attention():
    row = make_row(status="screening", last_activity=days_ago_iso(8),
                   next_action_due=None)
    assert len(logic.needs_attention([row])) == 1


def test_stale_but_inactive_status_is_ignored():
    # wishlist is not an ACTIVE status, so staleness does not apply.
    row = make_row(status="wishlist", last_activity=days_ago_iso(30),
                   next_action_due=None)
    assert logic.needs_attention([row]) == []


def test_recent_active_application_is_not_stale():
    row = make_row(status="interview", last_activity=days_ago_iso(2),
                   next_action_due=None)
    assert logic.needs_attention([row]) == []


def test_needs_attention_sorts_soonest_due_first():
    later = make_row(id=1, next_action_due=date_in(0))
    earlier = make_row(id=2, next_action_due=date_in(-5))
    undated_stale = make_row(id=3, status="applied",
                             last_activity=days_ago_iso(10), next_action_due=None)
    out = logic.needs_attention([later, earlier, undated_stale])
    # most-overdue first, dated before undated
    assert [r["id"] for r in out] == [2, 1, 3]


# --- today_band ------------------------------------------------------------
def test_today_band_empty_is_fresh_start():
    assert "fresh start" in logic.today_band([], []).lower()


def test_today_band_nothing_waiting():
    rows = [make_row(status="applied", last_activity=days_ago_iso(1))]
    assert "right this second" in logic.today_band(rows, []).lower()


def test_today_band_one_waiting():
    row = make_row(status="applied", next_action_due=date_in(-1))
    band = logic.today_band([row], [row])
    assert band == "1 application is waiting on you."


def test_today_band_names_upcoming_interview():
    row = make_row(company="Acme", status="interview", next_action_due=date_in(1))
    band = logic.today_band([row], [])
    assert "interview is tomorrow" in band.lower()


# --- attn_reason -----------------------------------------------------------
def test_attn_reason_due_today():
    assert logic.attn_reason(make_row(next_action_due=date_in(0))) == "Due today"


def test_attn_reason_overdue_pluralization():
    assert logic.attn_reason(make_row(next_action_due=date_in(-3))) == "Overdue by 3 days"
    assert logic.attn_reason(make_row(next_action_due=date_in(-1))) == "Overdue by 1 day"


def test_attn_reason_quiet_when_no_due_date():
    row = make_row(next_action_due=None, last_activity=days_ago_iso(9))
    assert logic.attn_reason(row) == "Quiet for 9 days"


# --- fmt_date --------------------------------------------------------------
def test_fmt_date_other_year_includes_year():
    assert logic.fmt_date("2020-03-05") == "Mar 5, 2020"


def test_fmt_date_current_year_omits_year():
    d = date(date.today().year, 1, 15).isoformat()
    assert logic.fmt_date(d) == "Jan 15"


def test_fmt_date_garbage_passthrough():
    assert logic.fmt_date("not-a-date") == "not-a-date"
    assert logic.fmt_date(None) == ""
