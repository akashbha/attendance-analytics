from datetime import datetime, timedelta, timezone
from app.main import (
    IST,
    ms_to_dt,
    dt_to_ms,
    shift_window,
    compute_late_minutes,
    compute_overtime,
    compute_work_hours,
    compute_half_day,
    record_out,
)


def test_late_minutes_rule_r2():
    # Shift start is 09:30:00 IST
    shift_date = "2026-07-06"
    start_dt = datetime(2026, 7, 6, 9, 30, 0, tzinfo=IST)

    # 09:40:00 -> 0 (exactly 10 min, within grace period)
    pin_40_00 = datetime(2026, 7, 6, 9, 40, 0, tzinfo=IST)
    assert compute_late_minutes(pin_40_00, start_dt) == 0

    # 09:40:01 -> 10 (strictly > 10 min, floor minutes since shift start)
    pin_40_01 = datetime(2026, 7, 6, 9, 40, 1, tzinfo=IST)
    assert compute_late_minutes(pin_40_01, start_dt) == 10

    # 10:15:59 -> 45 (45 min 59 sec after shift start -> floor to 45)
    pin_15_59 = datetime(2026, 7, 6, 10, 15, 59, tzinfo=IST)
    assert compute_late_minutes(pin_15_59, start_dt) == 45


def test_overnight_shift_window_rule_r1():
    # Overnight shift 22:00 to 06:00
    shift_start = "22:00"
    shift_end = "06:00"

    # Punch-in at 05:00 IST on 2026-07-07 belongs to previous day (2026-07-06)
    pin_05_00 = datetime(2026, 7, 7, 5, 0, 0, tzinfo=IST)
    date_str, start_dt, end_dt = shift_window(pin_05_00, shift_start, shift_end)

    assert date_str == "2026-07-06"
    assert start_dt == datetime(2026, 7, 6, 22, 0, tzinfo=IST)
    assert end_dt == datetime(2026, 7, 7, 6, 0, tzinfo=IST)


def test_work_hours_round_half_up_rule_r4():
    # 9.005 hours = 9.005 * 3600 = 32418 seconds
    pin = datetime(2026, 7, 6, 9, 0, 0, tzinfo=IST)
    pout = pin + timedelta(seconds=32418)

    # Decimal ROUND_HALF_UP should round 9.005 to 9.01 (whereas Banker's rounding rounds to 9.00)
    assert compute_work_hours(pin, pout) == 9.01


def test_overtime_rule_r3():
    end_dt = datetime(2026, 7, 6, 18, 30, tzinfo=IST)
    # 29 minutes after shift end -> 0 (below 30 min minimum)
    pout_29 = datetime(2026, 7, 6, 18, 59, tzinfo=IST)
    assert compute_overtime(pout_29, end_dt) == 0

    # 30 minutes after shift end -> 30
    pout_30 = datetime(2026, 7, 6, 19, 0, tzinfo=IST)
    assert compute_overtime(pout_30, end_dt) == 30

    # 45 minutes after shift end -> 45
    pout_45 = datetime(2026, 7, 6, 19, 15, tzinfo=IST)
    assert compute_overtime(pout_45, end_dt) == 45


def test_half_day_rule_r5():
    assert compute_half_day(4.49) is True
    assert compute_half_day(4.50) is False
    assert compute_half_day(8.0) is False
    assert compute_half_day(None) is False


def test_record_out_formatting():
    pin_dt = datetime(2026, 7, 6, 9, 30, 0, tzinfo=timezone.utc)
    pout_dt = datetime(2026, 7, 6, 18, 30, 0, tzinfo=timezone.utc)

    doc = {
        "_id": "dummy_mongo_id",
        "id": "bad_id",
        "emp_code": "EMP0001",
        "date": "2026-07-06",
        "status": "PRESENT",
        "punch_in": pin_dt,
        "punch_out": pout_dt,
        "work_hours": 9.0,
        "history": [
            {
                "at": pin_dt,
                "by": "admin",
                "reason": "correction",
                "changes": {
                    "punch_in": {"from": pin_dt, "to": pout_dt}
                }
            }
        ]
    }

    out = record_out(doc)
    assert "_id" not in out
    assert "id" not in out
    assert out["punch_in"] == int(pin_dt.timestamp()) * 1000
    assert out["punch_out"] == int(pout_dt.timestamp()) * 1000
    assert out["late_minutes"] == 0
    assert out["overtime_minutes"] == 0
    assert out["half_day"] is False
    assert out["history"][0]["at"] == int(pin_dt.timestamp()) * 1000
    assert out["history"][0]["changes"]["punch_in"]["from"] == int(pin_dt.timestamp()) * 1000
