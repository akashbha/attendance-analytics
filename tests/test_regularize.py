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


def test_late_fix_sample_8(mock_mongo):
    """
    Sample 8 from DATA_MODEL.md:
    HR corrects punch-in from 10:05 IST (late 35, work 8.42) to 09:28 IST (late 0, work 9.03).
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

    pin_dt = datetime(2026, 7, 7, 10, 5, 0, tzinfo=IST)
    pout_dt = datetime(2026, 7, 7, 18, 30, 0, tzinfo=IST)
    mock_mongo.attendance_logs.insert_one({
        "emp_code": "EMP0001",
        "date": "2026-07-07",
        "status": "PRESENT",
        "punch_in": pin_dt,
        "punch_out": pout_dt,
        "work_hours": 8.42,
        "late_minutes": 35,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    })

    client = TestClient(app)
    # Corrected punch_in: 09:28 IST on 2026-07-07
    new_pin_dt = datetime(2026, 7, 7, 9, 28, 0, tzinfo=IST)
    new_pin_ms = int(new_pin_dt.timestamp()) * 1000

    resp = client.patch(
        "/attendance/EMP0001/2026-07-07",
        json={
            "punch_in": new_pin_ms,
            "reason": "biometric glitch",
            "regularized_by": "hr.admin",
        },
    )

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["late_minutes"] == 0
    assert data["work_hours"] == 9.03
    assert len(data["history"]) == 1

    entry = data["history"][0]
    assert entry["by"] == "hr.admin"
    assert entry["reason"] == "biometric glitch"
    assert entry["changes"]["late_minutes"] == {"from": 35, "to": 0}
    assert entry["changes"]["work_hours"] == {"from": 8.42, "to": 9.03}
    assert entry["changes"]["punch_in"] == {
        "from": int(pin_dt.timestamp()) * 1000,
        "to": new_pin_ms,
    }


def test_absent_with_punch_times_gives_422(mock_mongo):
    """Setting status ABSENT with punch_in or punch_out must return 422."""
    mock_mongo.employees.insert_one({
        "emp_code": "EMP0001",
        "name": "Asha Rao",
        "email": "asha@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-01",
    })
    mock_mongo.attendance_logs.insert_one({
        "emp_code": "EMP0001",
        "date": "2026-07-07",
        "status": "PRESENT",
        "punch_in": datetime(2026, 7, 7, 9, 30, 0, tzinfo=IST),
        "punch_out": None,
        "work_hours": None,
        "late_minutes": 0,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    })

    client = TestClient(app)
    resp = client.patch(
        "/attendance/EMP0001/2026-07-07",
        json={
            "status": "ABSENT",
            "punch_in": int(datetime(2026, 7, 7, 9, 30, 0, tzinfo=IST).timestamp()) * 1000,
            "reason": "marked absent mistakenly",
            "regularized_by": "admin",
        },
    )
    assert resp.status_code == 422


def test_noop_gives_422(mock_mongo):
    """A request that changes nothing must return 422."""
    mock_mongo.employees.insert_one({
        "emp_code": "EMP0001",
        "name": "Asha Rao",
        "email": "asha@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-01",
    })
    mock_mongo.attendance_logs.insert_one({
        "emp_code": "EMP0001",
        "date": "2026-07-07",
        "status": "PRESENT",
        "punch_in": datetime(2026, 7, 7, 9, 30, 0, tzinfo=IST),
        "punch_out": None,
        "work_hours": None,
        "late_minutes": 0,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    })

    client = TestClient(app)
    resp = client.patch(
        "/attendance/EMP0001/2026-07-07",
        json={
            "status": "PRESENT",
            "reason": "no actual changes requested",
            "regularized_by": "admin",
        },
    )
    assert resp.status_code == 422


def test_parallel_patches_optimistic_lock(mock_mongo):
    """
    10 parallel PATCHes released at the same moment.
    All 10 read the document with n=0 history entries, and race to commit update_one.
    Expect exactly one 200 and nine 409, with exactly one history entry saved.
    """
    mock_mongo.employees.insert_one({
        "emp_code": "EMP0001",
        "name": "Asha Rao",
        "email": "asha@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-01",
    })
    pin_dt = datetime(2026, 7, 7, 10, 5, 0, tzinfo=IST)
    mock_mongo.attendance_logs.insert_one({
        "emp_code": "EMP0001",
        "date": "2026-07-07",
        "status": "PRESENT",
        "punch_in": pin_dt,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": 35,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    })

    import threading
    start_barrier = threading.Barrier(10)
    pre_update_barrier = threading.Barrier(10)

    orig_update = mock_mongo.attendance_logs.update_one
    def hooked_update_one(*args, **kwargs):
        pre_update_barrier.wait()
        return orig_update(*args, **kwargs)

    mock_mongo.attendance_logs.update_one = hooked_update_one
    new_pin_ms = int(datetime(2026, 7, 7, 9, 30, 0, tzinfo=IST).timestamp()) * 1000

    def patch_worker(i):
        client = TestClient(app)
        start_barrier.wait()
        return client.patch(
            "/attendance/EMP0001/2026-07-07",
            json={
                "punch_in": new_pin_ms,
                "reason": f"concurrent update attempt {i}",
                "regularized_by": f"admin_{i}",
            },
        )

    with ThreadPoolExecutor(max_workers=10) as executor:
        responses = list(executor.map(patch_worker, range(10)))

    status_codes = [r.status_code for r in responses]
    assert status_codes.count(200) == 1, f"Expected one 200, got: {status_codes}"
    assert status_codes.count(409) == 9, f"Expected nine 409, got: {status_codes}"

    # Verify database document has exactly one history entry
    mock_mongo.attendance_logs.update_one = orig_update
    saved = mock_mongo.attendance_logs.find_one({"emp_code": "EMP0001", "date": "2026-07-07"})
    assert len(saved["history"]) == 1


def test_explicit_null_validation_and_omitted_allowed(mock_mongo):
    """
    Explicit null for punch_in, punch_out, or status must return 422,
    while omitting them stays allowed.
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
    pin_dt = datetime(2026, 7, 7, 9, 30, 0, tzinfo=IST)
    pout_dt = datetime(2026, 7, 7, 18, 30, 0, tzinfo=IST)
    mock_mongo.attendance_logs.insert_one({
        "emp_code": "EMP0001",
        "date": "2026-07-07",
        "status": "PRESENT",
        "punch_in": pin_dt,
        "punch_out": pout_dt,
        "work_hours": 9.0,
        "late_minutes": 0,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    })

    client = TestClient(app)

    # {"punch_in": null} gives 422
    resp_pin = client.patch(
        "/attendance/EMP0001/2026-07-07",
        json={
            "punch_in": None,
            "reason": "testing explicit null punch_in",
            "regularized_by": "admin",
        },
    )
    assert resp_pin.status_code == 422, resp_pin.text

    # {"punch_out": null} gives 422
    resp_pout = client.patch(
        "/attendance/EMP0001/2026-07-07",
        json={
            "punch_out": None,
            "reason": "testing explicit null punch_out",
            "regularized_by": "admin",
        },
    )
    assert resp_pout.status_code == 422, resp_pout.text

    # {"status": null} gives 422
    resp_status = client.patch(
        "/attendance/EMP0001/2026-07-07",
        json={
            "status": None,
            "reason": "testing explicit null status",
            "regularized_by": "admin",
        },
    )
    assert resp_status.status_code == 422, resp_status.text

    # Omitting them still works
    new_pin_dt = datetime(2026, 7, 7, 9, 45, 0, tzinfo=IST)
    resp_omit = client.patch(
        "/attendance/EMP0001/2026-07-07",
        json={
            "punch_in": int(new_pin_dt.timestamp()) * 1000,
            "reason": "omitted optional fields update",
            "regularized_by": "admin",
        },
    )
    assert resp_omit.status_code == 200, resp_omit.text
    data = resp_omit.json()
    assert data["status"] == "PRESENT"
    assert data["punch_out"] == int(pout_dt.timestamp()) * 1000
    assert len(data["history"]) == 1

