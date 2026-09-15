"""
Regression tests for tools.calendar_tools.parse_natural_language_datetime.

These cover three bugs found and fixed in one session:
  1. A bare/"this <weekday>" that matches today's actual weekday used to
     skip to next week instead of resolving to today.
  2. The dateutil fallback anchored "today" to the server's clock instead
     of the user's timezone.
  3. Genuinely unparseable input needs to raise cleanly instead of
     crashing on an unrelated exception type.
"""
import datetime as real_datetime
import sys
from pathlib import Path

import pytest
import pytz
import streamlit as st

PROJECT_ROOT = Path(__file__).resolve().parent.parent
MY_ADK_AGENT = PROJECT_ROOT / "my-adk-agent"
for base in (str(PROJECT_ROOT), str(MY_ADK_AGENT)):
    if base not in sys.path:
        sys.path.insert(0, base)

from tools import calendar_tools as ct  # noqa: E402

LA = pytz.timezone("America/Los_Angeles")
FIXED_TUESDAY = LA.localize(real_datetime.datetime(2026, 9, 8, 10, 0, 0))


@pytest.fixture(autouse=True)
def user_timezone():
    st.session_state["timezone"] = "America/Los_Angeles"
    yield
    st.session_state.pop("timezone", None)


@pytest.fixture
def frozen_tuesday(monkeypatch):
    """Pin calendar_tools' notion of "now" to a known Tuesday."""

    class FrozenDateTime(real_datetime.datetime):
        @classmethod
        def now(cls, tz=None):
            return FIXED_TUESDAY

    monkeypatch.setattr(ct.datetime, "datetime", FrozenDateTime)
    return FIXED_TUESDAY


def test_bare_weekday_matching_today_resolves_to_today(frozen_tuesday):
    start, end, _ = ct.parse_natural_language_datetime("Tuesday")
    assert start == "2026-09-08"
    assert end == "2026-09-09"


def test_this_weekday_matching_today_resolves_to_today(frozen_tuesday):
    start, _, _ = ct.parse_natural_language_datetime("this Tuesday")
    assert start == "2026-09-08"


def test_next_weekday_always_skips_to_next_week(frozen_tuesday):
    start, _, _ = ct.parse_natural_language_datetime("next Tuesday")
    assert start == "2026-09-15"


def test_bare_weekday_with_explicit_time(frozen_tuesday):
    start, end, window = ct.parse_natural_language_datetime("Tuesday at 3pm")
    assert start == "2026-09-08T15:00:00-07:00"
    assert end == "2026-09-08T16:00:00-07:00"
    assert window is None


def test_relative_phrase_advances_from_the_same_anchor(frozen_tuesday):
    # Goes through the dateparser path, not the fast weekday path — verifies
    # RELATIVE_BASE is actually wired to the same "now" as everything else.
    start, _, _ = ct.parse_natural_language_datetime("tomorrow at 5pm")
    assert start == "2026-09-09T17:00:00-07:00"


def test_loose_phrasing_falls_through_to_dateparser(frozen_tuesday):
    # Doesn't match the strict fast-path regex, so this exercises the
    # dateparser fallback and confirms it still resolves sensibly.
    start, _, _ = ct.parse_natural_language_datetime("sometime in Friday")
    assert start == "2026-09-11"


def test_unparseable_input_raises_clear_value_error():
    with pytest.raises(ValueError, match="Could not parse date/time"):
        ct.parse_natural_language_datetime("gibberish that means nothing at all zzz")
