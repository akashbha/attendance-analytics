from datetime import datetime, timezone, timedelta
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
    1. $dateFromString
    2. $densify
    3. $setWindowFields (supporting $avg over window documents)
    """
    orig_hdo = mongomock.aggregate._Parser._handle_date_operator

    def custom_hdo(self, operator, values):
        if operator == "$dateFromString":
            val = self.parse(values)
            d_str = val["dateString"] if isinstance(val, dict) else val
            return datetime.fromisoformat(d_str).replace(tzinfo=timezone.utc)
        return orig_hdo(self, operator, values)

    monkeypatch.setattr(
        mongomock.aggregate._Parser, "_handle_date_operator", custom_hdo
    )

    def custom_densify(in_collection, database, options):
        field = options["field"]
        r = options["range"]
        bounds = r.get("bounds")
        start_dt, end_dt = bounds[0], bounds[1]

        existing = {}
        for doc in in_collection:
            d = doc[field]
            existing[d] = doc

        out = []
        curr = start_dt
        while curr < end_dt:
            if curr in existing:
                out.append(existing[curr])
            else:
                out.append({field: curr})
            curr += timedelta(days=1)
        return out

    monkeypatch.setitem(
        mongomock.aggregate._PIPELINE_HANDLERS, "$densify", custom_densify
    )

    def custom_set_window_fields(in_collection, database, options):
        sort_by = options.get("sortBy", {})
        output_spec = options.get("output", {})

        docs = list(in_collection)
        for field, order in reversed(list(sort_by.items())):
            docs.sort(key=lambda d: d.get(field), reverse=(order == -1))

        for out_field, out_expr in output_spec.items():
            if "$avg" in out_expr:
                val_field = (
                    out_expr["$avg"][1:]
                    if out_expr["$avg"].startswith("$")
                    else out_expr["$avg"]
                )
                window = out_expr.get("window", {}).get("documents", [-6, 0])
                w_start, w_end = window
                for i, doc in enumerate(docs):
                    start_idx = max(0, i + w_start)
                    end_idx = min(len(docs), i + w_end + 1)
                    window_docs = docs[start_idx:end_idx]
                    vals = [
                        d.get(val_field)
                        for d in window_docs
                        if d.get(val_field) is not None
                    ]
                    if vals:
                        doc[out_field] = sum(vals) / len(vals)
                    else:
                        doc[out_field] = None
        return docs

    monkeypatch.setitem(
        mongomock.aggregate._PIPELINE_HANDLERS,
        "$setWindowFields",
        custom_set_window_fields,
    )

    mock_client = mongomock.MongoClient()
    mock_db = mock_client["test_db"]
    monkeypatch.setattr(main_module, "client", mock_client)
    monkeypatch.setattr(main_module, "db", mock_db)
    return mock_db


def test_department_trend_hand_calculated(mock_mongo):
    """
    10-day hand-calculated scenario: 2026-07-03 to 2026-07-12
    Employees:
    - EMP01: joined 2026-01-01
    - EMP02: joined 2026-07-08 (joins mid-range!)

    Logs:
    - 2026-07-03 (Fri, working): EMP01 full day (1.0)
    - 2026-07-04 (Sat, weekend): EMP01 full day (1.0), late 15
    - 2026-07-05 (Sun, weekend): No logs
    - 2026-07-06 (Mon, working): EMP01 half day (0.5)
    - 2026-07-07 (Tue, working): No logs (rate 0.0)
    - 2026-07-08 (Wed, working): EMP02 joins (headcount 2). EMP01 full day (1.0)
    - 2026-07-09 (Thu, working): EMP01 full (1.0), EMP02 full (1.0) -> present 2.0
    - 2026-07-10 (Fri, working): EMP01 full (1.0), EMP02 half (0.5) -> present 1.5
    - 2026-07-11 (Sat, weekend): No logs
    - 2026-07-12 (Sun, weekend): No logs
    """
    mock_mongo.employees.insert_many([
        {
            "emp_code": "EMP01",
            "name": "Alice",
            "email": "alice@example.com",
            "department": "Engineering",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-01",
            "created_at": datetime.now(timezone.utc),
        },
        {
            "emp_code": "EMP02",
            "name": "Bob",
            "email": "bob@example.com",
            "department": "Engineering",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-07-08",
            "created_at": datetime.now(timezone.utc),
        },
    ])

    mock_mongo.attendance_logs.insert_many([
        {"emp_code": "EMP01", "date": "2026-07-03", "status": "PRESENT", "half_day": False, "late_minutes": 0},
        {"emp_code": "EMP01", "date": "2026-07-04", "status": "PRESENT", "half_day": False, "late_minutes": 15},
        {"emp_code": "EMP01", "date": "2026-07-06", "status": "PRESENT", "half_day": True, "late_minutes": 0},
        {"emp_code": "EMP01", "date": "2026-07-08", "status": "PRESENT", "half_day": False, "late_minutes": 0},
        {"emp_code": "EMP01", "date": "2026-07-09", "status": "PRESENT", "half_day": False, "late_minutes": 0},
        {"emp_code": "EMP02", "date": "2026-07-09", "status": "PRESENT", "half_day": False, "late_minutes": 0},
        {"emp_code": "EMP01", "date": "2026-07-10", "status": "PRESENT", "half_day": False, "late_minutes": 0},
        {"emp_code": "EMP02", "date": "2026-07-10", "status": "PRESENT", "half_day": True, "late_minutes": 0},
    ])

    client = TestClient(app)
    res = client.get("/analytics/departments/Engineering/trend?from=2026-07-03&to=2026-07-12")
    assert res.status_code == 200
    data = res.json()
    assert data["department"] == "Engineering"
    items = data["items"]
    assert len(items) == 10

    # Day 0: 2026-07-03 (Fri)
    assert items[0] == {
        "date": "2026-07-03",
        "is_working_day": True,
        "headcount": 1,
        "present_count": 1.0,
        "late_count": 0,
        "attendance_rate": 1.0,
        "moving_avg_7d": 1.0,
    }

    # Day 1: 2026-07-04 (Sat) - weekend row with weekend record
    assert items[1] == {
        "date": "2026-07-04",
        "is_working_day": False,
        "headcount": 1,
        "present_count": 1.0,
        "late_count": 1,
        "attendance_rate": None,
        "moving_avg_7d": 1.0,
    }

    # Day 2: 2026-07-05 (Sun) - weekend without records
    assert items[2] == {
        "date": "2026-07-05",
        "is_working_day": False,
        "headcount": 1,
        "present_count": 0.0,
        "late_count": 0,
        "attendance_rate": None,
        "moving_avg_7d": 1.0,
    }

    # Day 3: 2026-07-06 (Mon) - half day
    assert items[3] == {
        "date": "2026-07-06",
        "is_working_day": True,
        "headcount": 1,
        "present_count": 0.5,
        "late_count": 0,
        "attendance_rate": 0.5,
        "moving_avg_7d": 0.75,
    }

    # Day 4: 2026-07-07 (Tue) - working day with no records
    assert items[4] == {
        "date": "2026-07-07",
        "is_working_day": True,
        "headcount": 1,
        "present_count": 0.0,
        "late_count": 0,
        "attendance_rate": 0.0,
        "moving_avg_7d": 0.5,
    }

    # Day 5: 2026-07-08 (Wed) - EMP02 joins mid-range (headcount becomes 2)
    assert items[5] == {
        "date": "2026-07-08",
        "is_working_day": True,
        "headcount": 2,
        "present_count": 1.0,
        "late_count": 0,
        "attendance_rate": 0.5,
        "moving_avg_7d": 0.5,
    }

    # Day 6: 2026-07-09 (Thu)
    assert items[6] == {
        "date": "2026-07-09",
        "is_working_day": True,
        "headcount": 2,
        "present_count": 2.0,
        "late_count": 0,
        "attendance_rate": 1.0,
        "moving_avg_7d": 0.6,
    }

    # Day 7: 2026-07-10 (Fri) - window moves past Day 0
    assert items[7] == {
        "date": "2026-07-10",
        "is_working_day": True,
        "headcount": 2,
        "present_count": 1.5,
        "late_count": 0,
        "attendance_rate": 0.75,
        "moving_avg_7d": 0.55,
    }

    # Day 8: 2026-07-11 (Sat)
    assert items[8] == {
        "date": "2026-07-11",
        "is_working_day": False,
        "headcount": 2,
        "present_count": 0.0,
        "late_count": 0,
        "attendance_rate": None,
        "moving_avg_7d": 0.55,
    }

    # Day 9: 2026-07-12 (Sun)
    assert items[9] == {
        "date": "2026-07-12",
        "is_working_day": False,
        "headcount": 2,
        "present_count": 0.0,
        "late_count": 0,
        "attendance_rate": None,
        "moving_avg_7d": 0.55,
    }


def test_department_trend_validation_and_errors(mock_mongo):
    """
    Validate 422 before 404, date ordering, and maximum 92 days range.
    """
    client = TestClient(app)

    # 1. 422 for to < from (even on unknown department)
    res_inverted = client.get("/analytics/departments/UnknownDept/trend?from=2026-07-10&to=2026-07-01")
    assert res_inverted.status_code == 422

    # 2. 422 for range > 92 days
    res_too_long = client.get("/analytics/departments/UnknownDept/trend?from=2026-01-01&to=2026-04-10")
    assert res_too_long.status_code == 422

    # 3. 422 for bad date syntax
    res_bad_date = client.get("/analytics/departments/UnknownDept/trend?from=2026-7-1&to=2026-07-10")
    assert res_bad_date.status_code == 422

    # 4. 404 for unknown department when dates are valid
    res_not_found = client.get("/analytics/departments/UnknownDept/trend?from=2026-07-01&to=2026-07-10")
    assert res_not_found.status_code == 404
