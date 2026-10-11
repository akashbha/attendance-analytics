import copy
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
    Isolate tests with an in-memory mongomock database and patch:
    1. $dateFromString which is not implemented in mongomock.
    2. $lookup with let & pipeline which is not implemented in mongomock.
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

    orig_lookup = mongomock.aggregate._PIPELINE_HANDLERS["$lookup"]

    def custom_lookup(in_collection, database, options):
        if "pipeline" in options:
            foreign_name = options["from"]
            foreign_coll = database.get_collection(foreign_name)
            let_defs = options.get("let", {})
            sub_pipeline = options["pipeline"]
            as_field = options["as"]

            for doc in in_collection:
                let_vals = {}
                for k, var_expr in let_defs.items():
                    if isinstance(var_expr, str) and var_expr.startswith("$"):
                        field = var_expr[1:]
                        let_vals["$$" + k] = doc.get(field)

                def replace_vars(obj):
                    if isinstance(obj, dict):
                        return {k: replace_vars(v) for k, v in obj.items()}
                    elif isinstance(obj, list):
                        return [replace_vars(x) for x in obj]
                    elif isinstance(obj, str) and obj in let_vals:
                        return let_vals[obj]
                    return obj

                concrete_pipeline = replace_vars(copy.deepcopy(sub_pipeline))
                res = list(foreign_coll.aggregate(concrete_pipeline))
                doc[as_field] = res
            return in_collection
        return orig_lookup(in_collection, database, options)

    monkeypatch.setitem(
        mongomock.aggregate._PIPELINE_HANDLERS, "$lookup", custom_lookup
    )

    mock_client = mongomock.MongoClient()
    mock_db = mock_client["test_db"]
    monkeypatch.setattr(main_module, "client", mock_client)
    monkeypatch.setattr(main_module, "db", mock_db)
    return mock_db


def test_department_summary_hand_calculated(mock_mongo):
    """
    Hand-calculated test scenario for month 2026-07:

    Engineering:
    - EMP01: joined 2026-01-01
      - 2026-07-06 (Mon): PRESENT, work_hours=8.0, late=0 -> 1.0 present day
    - EMP02: joined 2026-01-01
      - 2026-07-06 (Mon): PRESENT, work_hours=6.0, late=0 -> 1.0 present day
      - 2026-07-07 (Tue): PRESENT, work_hours=6.0, late=15 -> 1.0 present day, late_count=1, late_min=15
    - EMP03: joined 2026-01-01 (zero attendance logs)
      - Counts in headcount (R9), contributes nothing else.
    - EMP04: joined 2026-08-01 (joined after July 2026)
      - Excluded from July headcount (R9).

    Engineering totals:
    - headcount = 3 (EMP01, EMP02, EMP03)
    - present_days = 1.0 + 1.0 + 1.0 = 3.0
    - avg_work_hours = (8.0 + 6.0 + 6.0) / 3 = 20.0 / 3 = 6.666... -> 6.67
      (Note: average of averages would have been (8.0 + 6.0) / 2 = 7.00, demonstrating record-average)
    - late_count = 1
    - total_late_minutes = 15
    - leave_count = 0
    - on_duty_count = 0

    Sales:
    - EMP05: joined 2026-05-01
      - 2026-07-08 (Wed): ON_DUTY, work_hours=4.0, half_day=True, late=0
        -> on_duty_count=1, present_days=0.5, work_hours counted (4.0)
      - 2026-07-09 (Thu): LEAVE
        -> leave_count=1, work_hours not counted
      - 2026-07-11 (Sat, weekend): PRESENT, work_hours=5.0, half_day=False, late=25
        -> weekend record! present_days=0.0 (per R7), work_hours counted (5.0), late_count=1, late_min=25

    Sales totals:
    - headcount = 1 (EMP05)
    - present_days = 0.5 + 0.0 + 0.0 = 0.5
    - avg_work_hours = (4.0 + 5.0) / 2 = 9.0 / 2 = 4.5
    - late_count = 1
    - total_late_minutes = 25
    - leave_count = 1
    - on_duty_count = 1
    """
    mock_mongo.employees.insert_many([
        {
            "emp_code": "EMP01",
            "name": "Dev One",
            "email": "dev1@example.com",
            "department": "Engineering",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-01",
            "created_at": datetime.now(timezone.utc),
        },
        {
            "emp_code": "EMP02",
            "name": "Dev Two",
            "email": "dev2@example.com",
            "department": "Engineering",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-01",
            "created_at": datetime.now(timezone.utc),
        },
        {
            "emp_code": "EMP03",
            "name": "Dev Three (No logs)",
            "email": "dev3@example.com",
            "department": "Engineering",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-01",
            "created_at": datetime.now(timezone.utc),
        },
        {
            "emp_code": "EMP04",
            "name": "Dev Four (Future Joiner)",
            "email": "dev4@example.com",
            "department": "Engineering",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-08-01",  # Future joiner
            "created_at": datetime.now(timezone.utc),
        },
        {
            "emp_code": "EMP05",
            "name": "Sales Rep",
            "email": "sales@example.com",
            "department": "Sales",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-05-01",
            "created_at": datetime.now(timezone.utc),
        },
    ])

    mock_mongo.attendance_logs.insert_many([
        # EMP01 logs
        {
            "emp_code": "EMP01",
            "date": "2026-07-06",
            "status": "PRESENT",
            "work_hours": 8.0,
            "late_minutes": 0,
            "half_day": False,
        },
        # EMP02 logs
        {
            "emp_code": "EMP02",
            "date": "2026-07-06",
            "status": "PRESENT",
            "work_hours": 6.0,
            "late_minutes": 0,
            "half_day": False,
        },
        {
            "emp_code": "EMP02",
            "date": "2026-07-07",
            "status": "PRESENT",
            "work_hours": 6.0,
            "late_minutes": 15,
            "half_day": False,
        },
        # EMP05 logs
        {
            "emp_code": "EMP05",
            "date": "2026-07-08",  # Wed
            "status": "ON_DUTY",
            "work_hours": 4.0,
            "late_minutes": 0,
            "half_day": True,
        },
        {
            "emp_code": "EMP05",
            "date": "2026-07-09",  # Thu
            "status": "LEAVE",
            "work_hours": None,
            "late_minutes": 0,
            "half_day": False,
        },
        {
            "emp_code": "EMP05",
            "date": "2026-07-11",  # Sat (weekend)
            "status": "PRESENT",
            "work_hours": 5.0,
            "late_minutes": 25,
            "half_day": False,
        },
    ])

    client = TestClient(app)

    # 1. Unfiltered request (returns all departments sorted ascending)
    res = client.get("/analytics/departments/summary?month=2026-07")
    assert res.status_code == 200
    data = res.json()
    assert data["month"] == "2026-07"
    assert len(data["items"]) == 2

    eng = data["items"][0]
    assert eng["department"] == "Engineering"
    assert eng["headcount"] == 3
    assert eng["present_days"] == 3.0
    assert eng["avg_work_hours"] == 6.67
    assert eng["late_count"] == 1
    assert eng["total_late_minutes"] == 15
    assert eng["leave_count"] == 0
    assert eng["on_duty_count"] == 0

    sales = data["items"][1]
    assert sales["department"] == "Sales"
    assert sales["headcount"] == 1
    assert sales["present_days"] == 0.5
    assert sales["avg_work_hours"] == 4.5
    assert sales["late_count"] == 1
    assert sales["total_late_minutes"] == 25
    assert sales["leave_count"] == 1
    assert sales["on_duty_count"] == 1

    # 2. Filter by department=Engineering
    res_eng = client.get(
        "/analytics/departments/summary?month=2026-07&department=Engineering"
    )
    assert res_eng.status_code == 200
    data_eng = res_eng.json()
    assert len(data_eng["items"]) == 1
    assert data_eng["items"][0]["department"] == "Engineering"

    # 3. Filter by department=Sales
    res_sales = client.get(
        "/analytics/departments/summary?month=2026-07&department=Sales"
    )
    assert res_sales.status_code == 200
    data_sales = res_sales.json()
    assert len(data_sales["items"]) == 1
    assert data_sales["items"][0]["department"] == "Sales"

    # 4. Filter by department with 0 headcount in that month
    res_none = client.get(
        "/analytics/departments/summary?month=2026-07&department=Marketing"
    )
    assert res_none.status_code == 200
    assert res_none.json()["items"] == []


def test_department_summary_validation():
    """Invalid month yields 422."""
    client = TestClient(app)
    res = client.get("/analytics/departments/summary?month=2026-7")
    assert res.status_code == 422
