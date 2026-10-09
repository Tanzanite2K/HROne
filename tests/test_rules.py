"""Unit tests for the business-rule helpers (R1-R5). No database needed."""
from datetime import date, datetime, timedelta, timezone

import pytest

from app.main import (
    attendance_date,
    compute_late_minutes,
    compute_overtime,
    compute_work_hours,
    instant_to_ms,
    month_bounds,
    ms_to_instant,
    punch_out_fields,
)

IST = timezone(timedelta(hours=5, minutes=30))
DAY = date(2026, 7, 6)  # a Monday
DAY_SHIFT = {"shift_start": "09:30", "shift_end": "18:30"}
NIGHT_SHIFT = {"shift_start": "22:00", "shift_end": "06:00"}


def ist(y, mo, d, h, mi, s=0):
    return datetime(y, mo, d, h, mi, s, tzinfo=IST)


# ---------------------------------------------------------------- R1: attendance date, truncation
@pytest.mark.parametrize("local, shift, expected", [
    (ist(2026, 7, 6, 9, 28), DAY_SHIFT, date(2026, 7, 6)),
    # 00:30 IST on the 6th is 19:00 UTC on the 5th: the IST date wins.
    (ist(2026, 7, 6, 0, 30), DAY_SHIFT, date(2026, 7, 6)),
    (ist(2026, 7, 6, 23, 59, 59), DAY_SHIFT, date(2026, 7, 6)),
    # Overnight 22:00-06:00: before 06:00 belongs to the shift that started the previous day.
    (ist(2026, 7, 7, 1, 0), NIGHT_SHIFT, date(2026, 7, 6)),
    (ist(2026, 7, 7, 5, 59, 59), NIGHT_SHIFT, date(2026, 7, 6)),
    (ist(2026, 7, 7, 6, 0, 0), NIGHT_SHIFT, date(2026, 7, 7)),  # not *earlier than* shift_end
    (ist(2026, 7, 6, 21, 55), NIGHT_SHIFT, date(2026, 7, 6)),
    (ist(2026, 7, 7, 0, 0, 0), NIGHT_SHIFT, date(2026, 7, 6)),  # exactly midnight
])
def test_r1_attendance_date(local, shift, expected):
    assert attendance_date(local.astimezone(timezone.utc), shift["shift_start"], shift["shift_end"]) == expected


def test_r1_truncates_to_whole_seconds():
    ms = instant_to_ms(ist(2026, 7, 6, 9, 40)) + 900  # 09:40:00.900
    instant = ms_to_instant(ms)
    assert instant == ist(2026, 7, 6, 9, 40)
    assert instant_to_ms(instant) % 1000 == 0


def test_instant_round_trip_and_naive_is_utc():
    aware = datetime(2026, 7, 6, 3, 58, tzinfo=timezone.utc)
    assert instant_to_ms(aware) == 1783310280000  # the example value in openapi.yaml
    assert instant_to_ms(aware.replace(tzinfo=None)) == 1783310280000
    assert ms_to_instant(1783310280000) == aware


# ---------------------------------------------------------------- R2: late
@pytest.mark.parametrize("local, expected", [
    (ist(2026, 7, 6, 9, 0), 0),         # early
    (ist(2026, 7, 6, 9, 30), 0),        # exactly at shift start
    (ist(2026, 7, 6, 9, 40, 0), 0),     # exactly 10:00 after -> not late
    (ist(2026, 7, 6, 9, 40, 1), 10),    # strictly more than 10:00 -> minutes since shift START
    (ist(2026, 7, 6, 9, 40, 59), 10),
    (ist(2026, 7, 6, 9, 41, 0), 11),
    (ist(2026, 7, 6, 10, 15, 59), 45),  # floor
    (ist(2026, 7, 6, 10, 5), 35),
])
def test_r2_late_day_shift(local, expected):
    assert compute_late_minutes(local.astimezone(timezone.utc), DAY, "09:30") == expected


def test_r2_late_overnight_after_midnight():
    # Attendance date 07-06 (shift started 22:00 on the 6th); punch at 01:00 on the 7th = 180 min late.
    assert compute_late_minutes(ist(2026, 7, 7, 1, 0), DAY, "22:00") == 180
    assert compute_late_minutes(ist(2026, 7, 6, 22, 10, 0), DAY, "22:00") == 0
    assert compute_late_minutes(ist(2026, 7, 6, 22, 10, 1), DAY, "22:00") == 10
    assert compute_late_minutes(ist(2026, 7, 6, 21, 55), DAY, "22:00") == 0


# ---------------------------------------------------------------- R3: overtime
@pytest.mark.parametrize("local, shift, expected", [
    (ist(2026, 7, 6, 18, 35), DAY_SHIFT, 0),           # 5 min: below the 30-min minimum
    (ist(2026, 7, 6, 18, 59, 59), DAY_SHIFT, 0),       # 29:59 -> floor 29 -> 0
    (ist(2026, 7, 6, 19, 0, 0), DAY_SHIFT, 30),        # exactly 30
    (ist(2026, 7, 6, 19, 10), DAY_SHIFT, 40),
    (ist(2026, 7, 6, 17, 0), DAY_SHIFT, 0),            # left early
    (ist(2026, 7, 7, 6, 40), NIGHT_SHIFT, 40),         # overnight: shift end is on the NEXT day
    (ist(2026, 7, 7, 6, 29, 59), NIGHT_SHIFT, 0),
    (ist(2026, 7, 7, 5, 0), NIGHT_SHIFT, 0),
])
def test_r3_overtime(local, shift, expected):
    assert compute_overtime(local, DAY, shift["shift_start"], shift["shift_end"]) == expected


# ---------------------------------------------------------------- R4/R5: work hours, half day
@pytest.mark.parametrize("seconds, hours", [
    (29250, 8.13),      # 8.125 -> 8.13 half-up (Python round() gives 8.12)
    (28962, 8.05),      # 8.045 -> 8.05 half-up (Python round() gives 8.04)
    (9045, 2.51),       # 2.5125 -> 2.51
    (32820, 9.12),      # sample 1: 09:28 -> 18:35
    (31500, 8.75),      # sample 7, overnight
    (16181, 4.49),
    (16182, 4.5),       # 4.495 -> 4.50
    (18, 0.01),         # 0.005 -> 0.01
    (17, 0.0),
    (86400, 24.0),
])
def test_r4_work_hours_half_up(seconds, hours):
    start = ist(2026, 7, 6, 9, 0)
    assert compute_work_hours(start, start + timedelta(seconds=seconds)) == hours


def test_r4_python_round_would_be_wrong():
    assert round(29250 / 3600, 2) == 8.12  # banker's/float rounding  # documents why the starter's round() was a defect


@pytest.mark.parametrize("seconds, half_day", [(16181, True), (16182, False), (12600, True), (32820, False)])
def test_r5_half_day_uses_rounded_hours(seconds, half_day):
    start = ist(2026, 7, 6, 9, 30)
    fields = punch_out_fields(DAY_SHIFT, DAY, start, start + timedelta(seconds=seconds))
    assert fields["half_day"] is half_day


def test_punch_out_fields_before_punch_out():
    assert punch_out_fields(DAY_SHIFT, DAY, ist(2026, 7, 6, 9, 30), None) == {
        "work_hours": None, "overtime_minutes": 0, "half_day": False}


def test_month_bounds():
    assert month_bounds("2026-07") == (date(2026, 7, 1), date(2026, 8, 1))
    assert month_bounds("2026-12") == (date(2026, 12, 1), date(2027, 1, 1))
    assert month_bounds("2028-02") == (date(2028, 2, 1), date(2028, 3, 1))
