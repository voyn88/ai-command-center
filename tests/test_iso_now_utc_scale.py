"""VOYN-W0-AICC-ISO-NOW-NAIVE-LOCAL: the recorded timestamp scale is UTC.

`models.iso_now()` used to return naive *local* time. Local time is not
monotonic: at the autumn DST fall-back the same wall clock is replayed, so a
run started later gets a strictly smaller string and every
`ORDER BY created_at DESC` answers with the earlier row. The defect is in the
recorded scale, so nothing downstream can repair it — and raising precision to
microseconds does not either, because the collision is an hour wide.

These tests pin the scale itself (a value that does not depend on the writing
host's zone) and the consequence that motivated the change (two runs inside a
real fold keep their true order). Every one of them sets a non-UTC `TZ`: on a
UTC host the old and new behaviour are indistinguishable, so a test that does
not move the process zone cannot fail on the defect it is guarding.
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from command_center import models

# Both sides of UTC, so a test cannot pass by accident of sign.
NON_UTC_TZS = ["America/Los_Angeles", "Europe/Moscow", "Asia/Kolkata"]

# America/Los_Angeles ends DST on 2026-11-01 at 02:00 local: 01:00-02:00 local
# happens twice, first as PDT (UTC-7) and again as PST (UTC-8). These two
# instants are 20 minutes apart in truth and inverted on that wall clock.
FOLD_TZ = "America/Los_Angeles"
FOLD_EARLIER_UTC = datetime(2026, 11, 1, 8, 50, tzinfo=timezone.utc)  # 01:50 PDT
FOLD_LATER_UTC = datetime(2026, 11, 1, 9, 10, tzinfo=timezone.utc)   # 01:10 PST


@pytest.fixture
def process_tz():
    """Set the process timezone for the duration of a test, then restore it."""
    original = os.environ.get("TZ")

    def _set(name: str) -> None:
        os.environ["TZ"] = name
        time.tzset()

    yield _set
    if original is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = original
    time.tzset()


def test_iso_now_is_utc_whatever_zone_the_writing_host_is_in(process_tz):
    """The value is a function of the instant, not of the host."""
    for tz in NON_UTC_TZS:
        process_tz(tz)
        before = datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)
        written = models.iso_now()
        after = datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)

        assert before.isoformat(timespec="seconds") <= written
        assert written <= after.isoformat(timespec="seconds")


def test_the_format_is_unchanged_naive_seconds_no_offset(process_tz):
    """Only the scale moved. A stored column full of pre-existing strings must
    stay directly comparable, so the text shape may not change."""
    process_tz("Europe/Moscow")
    written = models.iso_now()

    assert len(written) == len("2026-09-15T03:10:00")
    assert "+" not in written and not written.endswith("Z")
    parsed = datetime.fromisoformat(written)
    assert parsed.tzinfo is None
    assert parsed.microsecond == 0


def test_utc_now_is_the_naive_reference_iso_now_renders(process_tz):
    process_tz("Asia/Kolkata")
    reference = models.utc_now()

    assert reference.tzinfo is None
    # Kolkata is UTC+5:30, so a local clock would differ in the *minutes* too —
    # a half-hour zone catches an "off by whole hours" comparison.
    assert abs((reference - datetime.now(timezone.utc).replace(tzinfo=None))) < timedelta(
        seconds=5
    )


def test_two_runs_inside_a_dst_fall_back_keep_their_true_order(monkeypatch, process_tz):
    """The reported defect, reproduced at the seam and then asserted fixed.

    The premise is checked first: on that host's wall clock these two instants
    really are inverted (the `03:50`-against-a-later-`03:10` shape from the
    report). That is what made `ORDER BY created_at DESC` select the earlier
    run. On the UTC scale the same two instants are ordered correctly.
    """
    process_tz(FOLD_TZ)
    zone = ZoneInfo(FOLD_TZ)

    local_earlier = FOLD_EARLIER_UTC.astimezone(zone).replace(tzinfo=None).isoformat(
        timespec="seconds"
    )
    local_later = FOLD_LATER_UTC.astimezone(zone).replace(tzinfo=None).isoformat(
        timespec="seconds"
    )
    # Premise: the old scale inverted these. If this ever stops holding, the
    # test below is no longer testing anything.
    assert FOLD_EARLIER_UTC < FOLD_LATER_UTC
    assert local_earlier > local_later

    clock = iter([FOLD_EARLIER_UTC, FOLD_LATER_UTC])
    monkeypatch.setattr(
        models, "utc_now", lambda: next(clock).replace(tzinfo=None)
    )
    first = models.iso_now()
    second = models.iso_now()

    assert first < second
    # And "newest first" — the ordering every read path uses — now agrees with
    # the truth instead of contradicting it.
    assert sorted([first, second], reverse=True)[0] == second


def test_microsecond_precision_would_not_have_fixed_the_fold(process_tz):
    """Documents a verified non-fix, so it is not attempted again: the
    collision is an hour wide, so more digits still invert."""
    process_tz(FOLD_TZ)
    zone = ZoneInfo(FOLD_TZ)

    earlier = FOLD_EARLIER_UTC.astimezone(zone).replace(tzinfo=None).isoformat()
    later = FOLD_LATER_UTC.astimezone(zone).replace(tzinfo=None).isoformat()

    assert earlier > later  # still inverted at microsecond precision


def test_format_age_measures_against_the_stored_scale(process_tz):
    """A freshly written timestamp is zero seconds old on every host.

    With a local reference against a UTC stamp this read the host's UTC offset
    as the age — "8h 00m" for a run that had just started, on every screen that
    shows an age.
    """
    for tz in NON_UTC_TZS:
        process_tz(tz)
        assert models.format_age(models.iso_now()) == "0s"


def test_format_age_converts_an_aware_input_instead_of_truncating_it(process_tz):
    process_tz("Europe/Moscow")
    aware = datetime.now(timezone.utc).astimezone(ZoneInfo("Asia/Tokyo")) - timedelta(
        minutes=5
    )

    # Dropping the +09:00 offset (the old behaviour) would have reported this
    # as roughly nine hours in the future, i.e. "—"-adjacent nonsense.
    assert models.format_age(aware.isoformat(timespec="seconds")) == "5m 00s"


def test_format_age_accepts_an_aware_reference_clock(process_tz):
    process_tz("America/Los_Angeles")
    written = models.iso_now()

    assert models.format_age(written, now=datetime.now(timezone.utc)) == "0s"


def test_format_age_still_guards_missing_and_unparseable(process_tz):
    process_tz("Europe/Moscow")
    assert models.format_age(None) == "—"
    assert models.format_age("") == "—"
    assert models.format_age("не дата") == "—"


def test_to_local_renders_a_stored_stamp_on_the_readers_clock(process_tz):
    """The display-side inverse: an absolute time an operator reads off their
    own wall clock, and the calendar day a "runs today" KPI buckets by."""
    process_tz("Europe/Moscow")  # permanently UTC+3
    stored = datetime(2026, 9, 15, 3, 10, 0)

    assert models.to_local(stored) == datetime(2026, 9, 15, 6, 10, 0)

    process_tz("America/Los_Angeles")  # UTC-7 on that date (PDT)
    assert models.to_local(stored) == datetime(2026, 9, 14, 20, 10, 0)


def test_to_local_round_trips_the_current_instant(process_tz):
    process_tz("Asia/Kolkata")
    now = models.utc_now()

    assert models.to_local(now) - now == datetime.now().astimezone().utcoffset()
