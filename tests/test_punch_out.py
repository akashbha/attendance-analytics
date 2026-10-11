from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import mongomock
import pytest
from fastapi.testclient import TestClient

import app.main as main_module
from app.main import app, IST


@pytest.fixture(autouse=True)
def mock_mongo(monkeypatch):
    """Isolate tests with an in-memory mongomock database."""
    mock_client = mongomock.MongoClient()
    mock_db = mock_client["test_db"]
    monkeypatch.setattr(main_module, "client", mock_client)
    monkeypatch.setattr(main_module, "db", mock_db)
    return mock_db


def test_simultaneous_punch_outs_race_condition(mock_mongo):
    """
    Two or more simultaneous punch-outs must yield exactly one 200
    and the rest 409 Conflict.
    """
    mock_mongo.employees.insert_one({
        "emp_code": "EMP0001",
        "name": "Asha Rao",
        "email": "asha@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-01",
        "created_at": datetime.now(timezone.utc),
    })

    pin_dt = datetime(2026, 7, 6, 9, 30, 0, tzinfo=IST)
    mock_mongo.attendance_logs.insert_one({
        "emp_code": "EMP0001",
        "date": "2026-07-06",
        "status": "PRESENT",
        "punch_in": pin_dt,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": 0,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    })

    client = TestClient(app)
    pout_ms = int(datetime(2026, 7, 6, 18, 30, 0, tzinfo=IST).timestamp()) * 1000

    def do_punch_out(_):
        return client.post("/attendance/punch-out", json={
            "emp_code": "EMP0001",
            "punched_at": pout_ms,
        })

    with ThreadPoolExecutor(max_workers=10) as executor:
        responses = list(executor.map(do_punch_out, range(10)))

    status_codes = [r.status_code for r in responses]
    assert status_codes.count(200) == 1, f"Expected exactly one 200, got: {status_codes}"
    assert status_codes.count(409) == 9, f"Expected exactly nine 409s, got: {status_codes}"


def test_overnight_shift_punched_out_after_midnight(mock_mongo):
    """
    Overnight shift (22:00 - 06:00 next day).
    Punch-in at 21:55 IST on 2026-07-06, punch-out at 06:40 IST on 2026-07-07.
    Matches Sample 7 in DATA_MODEL.md.
    """
    mock_mongo.employees.insert_one({
        "emp_code": "EMP0005",
        "name": "Night Guard",
        "email": "night@example.com",
        "department": "Operations",
        "shift_start": "22:00",
        "shift_end": "06:00",
        "joined_on": "2026-01-01",
        "created_at": datetime.now(timezone.utc),
    })

    # Punch-in at 21:55 IST on 2026-07-06
    pin_dt = datetime(2026, 7, 6, 21, 55, 0, tzinfo=IST)
    mock_mongo.attendance_logs.insert_one({
        "emp_code": "EMP0005",
        "date": "2026-07-06",
        "status": "PRESENT",
        "punch_in": pin_dt,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": 0,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    })

    client = TestClient(app)
    # Punch out at 06:40 IST next morning (2026-07-07)
    pout_dt = datetime(2026, 7, 7, 6, 40, 0, tzinfo=IST)
    pout_ms = int(pout_dt.timestamp()) * 1000

    resp = client.post("/attendance/punch-out", json={
        "emp_code": "EMP0005",
        "punched_at": pout_ms,
    })

    assert resp.status_code == 200
    data = resp.json()
    assert data["date"] == "2026-07-06"
    assert data["work_hours"] == 8.75  # 8 hours 45 minutes = 8.75
    assert data["overtime_minutes"] == 40  # 40 min past 06:00 shift end
    assert data["half_day"] is False
    assert data["punch_out"] == pout_ms


def test_punch_out_validation_rules(mock_mongo):
    """Test 404, 409, and 422 validations for punch-out."""
    mock_mongo.employees.insert_one({
        "emp_code": "EMP0001",
        "name": "Test User",
        "email": "test@example.com",
        "department": "IT",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-01",
        "created_at": datetime.now(timezone.utc),
    })

    client = TestClient(app)

    # 1. 404 for unknown employee
    r = client.post("/attendance/punch-out", json={"emp_code": "EMP9999", "punched_at": 1783312500000})
    assert r.status_code == 404

    # 2. 404 when no punch-in exists for employee
    r = client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": 1783312500000})
    assert r.status_code == 404

    # Insert open record
    pin_dt = datetime(2026, 7, 6, 9, 30, 0, tzinfo=IST)
    pin_ms = int(pin_dt.timestamp()) * 1000
    mock_mongo.attendance_logs.insert_one({
        "emp_code": "EMP0001",
        "date": "2026-07-06",
        "status": "PRESENT",
        "punch_in": pin_dt,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": 0,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    })

    # 3. 422 if punched_at <= punch_in
    r = client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": pin_ms})
    assert r.status_code == 422

    # 4. 422 if punched_at > 24 hours after punch_in
    after_25h = pin_ms + (25 * 3600 * 1000)
    r = client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": after_25h})
    assert r.status_code == 422
