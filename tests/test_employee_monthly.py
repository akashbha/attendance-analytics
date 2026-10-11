from datetime import datetime, timezone
import mongomock
import mongomock.aggregate
import pytest
from fastapi.testclient import TestClient

import app.main as main_module
from app.main import app


@pytest.fixture(autouse=True)
def mock_mongo(monkeypatch):
    """
    Isolate tests with an in-memory mongomock database and patch $dateFromString
    which is not implemented out of the box in mongomock.
    """
    orig_hdo = mongomock.aggregate._Parser._handle_date_operator

    def custom_hdo(self, operator, values):
        if operator == "$dateFromString":
            val = self.parse(values)
            d_str = val["dateString"] if isinstance(val, dict) else val
            return datetime.fromisoformat(d_str)
        return orig_hdo(self, operator, values)

    monkeypatch.setattr(
        mongomock.aggregate._Parser, "_handle_date_operator", custom_hdo
    )

    mock_client = mongomock.MongoClient()
    mock_db = mock_client["test_db"]
    monkeypatch.setattr(main_module, "client", mock_client)
    monkeypatch.setattr(main_module, "db", mock_db)
    return mock_db


def test_employee_monthly_full_scenarios(mock_mongo):
    """
    Tests:
    1. EMP1001: records with weekday present, half day, LEAVE, and weekend record.
       July 2026 has 23 working days (Mon-Fri).
       Hand-calculated expected values:
       - 2026-07-06 (Mon): PRESENT, half_day=False, late=0, OT=0 -> 1.0 present day
       - 2026-07-07 (Tue): PRESENT, half_day=True, late=15, OT=0 -> 0.5 present day, late_count=1, late_min=15
       - 2026-07-08 (Wed): LEAVE -> leave_days=1, 0 present days
       - 2026-07-11 (Sat, weekend): PRESENT, half_day=False, late=20, OT=45 -> 0.0 present day (R7),
         contributes to late (count=1, min=20) and overtime (45).
       Totals:
       working_days = 23
       present_days = 1.0 + 0.5 = 1.5
       leave_days = 1
       late_count = 1 (Tue) + 1 (Sat) = 2
       total_late_minutes = 15 + 20 = 35
       total_overtime_minutes = 45
       attendance_pct = (1.5 / 23) * 100 = 6.5217... -> 6.52
    """
    mock_mongo.employees.insert_one({
        "emp_code": "EMP1001",
        "name": "Alice Smith",
        "email": "alice@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-01",
        "created_at": datetime.now(timezone.utc),
    })

    mock_mongo.attendance_logs.insert_many([
        {
            "emp_code": "EMP1001",
            "date": "2026-07-06",  # Mon
            "status": "PRESENT",
            "half_day": False,
            "late_minutes": 0,
            "overtime_minutes": 0,
            "history": [],
        },
        {
            "emp_code": "EMP1001",
            "date": "2026-07-07",  # Tue
            "status": "PRESENT",
            "half_day": True,
            "late_minutes": 15,
            "overtime_minutes": 0,
            "history": [],
        },
        {
            "emp_code": "EMP1001",
            "date": "2026-07-08",  # Wed
            "status": "LEAVE",
            "half_day": False,
            "late_minutes": 0,
            "overtime_minutes": 0,
            "history": [],
        },
        {
            "emp_code": "EMP1001",
            "date": "2026-07-11",  # Sat (weekend)
            "status": "PRESENT",
            "half_day": False,
            "late_minutes": 20,
            "overtime_minutes": 45,
            "history": [],
        },
    ])

    client = TestClient(app)
    res = client.get("/analytics/employees/EMP1001/monthly?month=2026-07")
    assert res.status_code == 200
    data = res.json()

    assert data["emp_code"] == "EMP1001"
    assert data["month"] == "2026-07"
    assert data["working_days"] == 23
    assert data["present_days"] == 1.5
    assert data["leave_days"] == 1
    assert data["late_count"] == 2
    assert data["total_late_minutes"] == 35
    assert data["total_overtime_minutes"] == 45
    assert data["attendance_pct"] == 6.52


def test_employee_monthly_mid_month_joiner(mock_mongo):
    """
    2. EMP1002: Mid-month joiner on 2026-07-20 (Monday).
       Working days from July 20 to July 31 = 10 Mon-Fri days.
       1 log on 2026-07-20: PRESENT.
       Expected totals:
       working_days = 10
       present_days = 1.0
       leave_days = 0
       late_count = 0
       total_late_minutes = 0
       total_overtime_minutes = 0
       attendance_pct = (1.0 / 10) * 100 = 10.0
    """
    mock_mongo.employees.insert_one({
        "emp_code": "EMP1002",
        "name": "Bob Jones",
        "email": "bob@example.com",
        "department": "Design",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-07-20",
        "created_at": datetime.now(timezone.utc),
    })

    mock_mongo.attendance_logs.insert_one({
        "emp_code": "EMP1002",
        "date": "2026-07-20",
        "status": "PRESENT",
        "half_day": False,
        "late_minutes": 0,
        "overtime_minutes": 0,
        "history": [],
    })

    client = TestClient(app)
    res = client.get("/analytics/employees/EMP1002/monthly?month=2026-07")
    assert res.status_code == 200
    data = res.json()

    assert data["emp_code"] == "EMP1002"
    assert data["month"] == "2026-07"
    assert data["working_days"] == 10
    assert data["present_days"] == 1.0
    assert data["leave_days"] == 0
    assert data["late_count"] == 0
    assert data["total_late_minutes"] == 0
    assert data["total_overtime_minutes"] == 0
    assert data["attendance_pct"] == 10.0


def test_employee_monthly_no_logs(mock_mongo):
    """
    3. EMP1003: Employee with zero attendance logs.
       Joined on 2026-01-01.
       Expected:
       working_days = 23
       present_days = 0.0
       leave_days = 0
       late_count = 0
       total_late_minutes = 0
       total_overtime_minutes = 0
       attendance_pct = 0.0
    """
    mock_mongo.employees.insert_one({
        "emp_code": "EMP1003",
        "name": "Charlie Brown",
        "email": "charlie@example.com",
        "department": "HR",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-01",
        "created_at": datetime.now(timezone.utc),
    })

    client = TestClient(app)
    res = client.get("/analytics/employees/EMP1003/monthly?month=2026-07")
    assert res.status_code == 200
    data = res.json()

    assert data["emp_code"] == "EMP1003"
    assert data["month"] == "2026-07"
    assert data["working_days"] == 23
    assert data["present_days"] == 0.0
    assert data["leave_days"] == 0
    assert data["late_count"] == 0
    assert data["total_late_minutes"] == 0
    assert data["total_overtime_minutes"] == 0
    assert data["attendance_pct"] == 0.0


def test_employee_monthly_joined_after_month(mock_mongo):
    """
    Employee joined after the requested month -> working_days = 0, attendance_pct = null.
    """
    mock_mongo.employees.insert_one({
        "emp_code": "EMP1004",
        "name": "Diana Prince",
        "email": "diana@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-08-01",
        "created_at": datetime.now(timezone.utc),
    })

    client = TestClient(app)
    res = client.get("/analytics/employees/EMP1004/monthly?month=2026-07")
    assert res.status_code == 200
    data = res.json()

    assert data["working_days"] == 0
    assert data["attendance_pct"] is None


def test_employee_monthly_validation_and_not_found(mock_mongo):
    """
    - 404 if employee does not exist
    - 422 if month parameter is invalid (not strict YYYY-MM)
    """
    client = TestClient(app)

    # 404 for unknown employee
    res404 = client.get("/analytics/employees/EMP9999/monthly?month=2026-07")
    assert res404.status_code == 404

    # Existing employee for format tests
    mock_mongo.employees.insert_one({
        "emp_code": "EMP1001",
        "name": "Alice Smith",
        "email": "alice@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-01",
        "created_at": datetime.now(timezone.utc),
    })

    # 422 for bad month formats
    bad_months = ["2026-7", "2026-13", "2026/07", "2026-00", "invalid"]
    for bm in bad_months:
        res422 = client.get(f"/analytics/employees/EMP1001/monthly?month={bm}")
        assert res422.status_code == 422, f"Expected 422 for month {bm}, got {res422.status_code}"
