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
    Isolate tests with an in-memory mongomock database and patch $setWindowFields
    for $rank (standard competition ranking: 1, 2, 2, 4).
    """
    def custom_set_window_fields(in_collection, database, options):
        sort_by = options.get("sortBy", {})
        output_spec = options.get("output", {})

        docs = list(in_collection)
        for field, order in reversed(list(sort_by.items())):
            docs.sort(key=lambda d: d.get(field, 0), reverse=(order == -1))

        for out_field, out_expr in output_spec.items():
            if "$rank" in out_expr:
                current_rank = 1
                for i, doc in enumerate(docs):
                    if i > 0:
                        prev = docs[i - 1]
                        same = all(doc.get(k) == prev.get(k) for k in sort_by)
                        if not same:
                            current_rank = i + 1
                    doc[out_field] = current_rank
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


def test_late_leaderboard_hand_calculated(mock_mongo):
    """
    Hand-calculated test scenario:
    - EMP06: Frank (Marketing), 50 late minutes (1 log)
    - EMP01: Alice (Engineering), 30 late minutes (1 log)
    - EMP02: Bob (Engineering), 20 late minutes on Tue + 10 late minutes on Sat (weekend!) = 30 late minutes (2 logs)
    - EMP04: David (Engineering), 10 late minutes (1 log)
    - EMP05: Eve (Engineering), 0 late minutes -> Excluded (total late minutes must be > 0)
    - EMP99: Unknown employee (no record in employees), 100 late minutes -> Ignored (unwind/lookup drops)

    Rankings across all departments:
    - Rank 1: EMP06 (50 min, Marketing)
    - Rank 2: EMP01 (30 min, Engineering) - tie
    - Rank 2: EMP02 (30 min, Engineering) - tie (weekend record counted)
    - Rank 4: EMP04 (10 min, Engineering) - competition ranking skips rank 3!

    Cutoff check (limit=2):
    - Should return rank <= 2, i.e., 3 rows (EMP06, EMP01, EMP02).

    Department filter (department=Engineering):
    - EMP06 is excluded before ranking.
    - Inside Engineering:
      - Rank 1: EMP01 (30 min)
      - Rank 1: EMP02 (30 min)
      - Rank 3: EMP04 (10 min)
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
            "joined_on": "2026-01-01",
            "created_at": datetime.now(timezone.utc),
        },
        {
            "emp_code": "EMP04",
            "name": "David",
            "email": "david@example.com",
            "department": "Engineering",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-01",
            "created_at": datetime.now(timezone.utc),
        },
        {
            "emp_code": "EMP05",
            "name": "Eve",
            "email": "eve@example.com",
            "department": "Engineering",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-01",
            "created_at": datetime.now(timezone.utc),
        },
        {
            "emp_code": "EMP06",
            "name": "Frank",
            "email": "frank@example.com",
            "department": "Marketing",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-01",
            "created_at": datetime.now(timezone.utc),
        },
    ])

    mock_mongo.attendance_logs.insert_many([
        # EMP06: 50 min
        {"emp_code": "EMP06", "date": "2026-07-06", "late_minutes": 50},
        # EMP01: 30 min
        {"emp_code": "EMP01", "date": "2026-07-06", "late_minutes": 30},
        # EMP02: 20 min on weekday + 10 min on weekend
        {"emp_code": "EMP02", "date": "2026-07-07", "late_minutes": 20},
        {"emp_code": "EMP02", "date": "2026-07-11", "late_minutes": 10},  # Saturday (weekend)
        # EMP04: 10 min
        {"emp_code": "EMP04", "date": "2026-07-08", "late_minutes": 10},
        # EMP05: 0 min (on time)
        {"emp_code": "EMP05", "date": "2026-07-09", "late_minutes": 0},
        # EMP99: Unknown employee
        {"emp_code": "EMP99", "date": "2026-07-10", "late_minutes": 100},
    ])

    client = TestClient(app)

    # 1. Full leaderboard (limit=10)
    res = client.get("/analytics/leaderboard/late?month=2026-07&limit=10")
    assert res.status_code == 200
    data = res.json()
    assert data["month"] == "2026-07"
    items = data["items"]

    # Unknown employee EMP99 and 0-minute employee EMP05 are excluded
    assert len(items) == 4

    # Rank 1: EMP06
    assert items[0]["rank"] == 1
    assert items[0]["emp_code"] == "EMP06"
    assert items[0]["total_late_minutes"] == 50
    assert items[0]["late_count"] == 1

    # Rank 2: EMP01 and EMP02 (tied, sorted by emp_code ascending)
    assert items[1]["rank"] == 2
    assert items[1]["emp_code"] == "EMP01"
    assert items[1]["total_late_minutes"] == 30
    assert items[1]["late_count"] == 1

    assert items[2]["rank"] == 2
    assert items[2]["emp_code"] == "EMP02"
    assert items[2]["total_late_minutes"] == 30
    assert items[2]["late_count"] == 2  # includes weekend log

    # Rank 4: EMP04 (competition ranking skips 3)
    assert items[3]["rank"] == 4
    assert items[3]["emp_code"] == "EMP04"
    assert items[3]["total_late_minutes"] == 10
    assert items[3]["late_count"] == 1

    # 2. Tie at the cutoff (limit=2 returns 3 rows)
    res_cutoff = client.get("/analytics/leaderboard/late?month=2026-07&limit=2")
    assert res_cutoff.status_code == 200
    cutoff_items = res_cutoff.json()["items"]
    assert len(cutoff_items) == 3
    assert [row["rank"] for row in cutoff_items] == [1, 2, 2]

    # 3. Department filter (re-ranking inside Engineering)
    res_dept = client.get("/analytics/leaderboard/late?month=2026-07&department=Engineering")
    assert res_dept.status_code == 200
    dept_items = res_dept.json()["items"]
    assert len(dept_items) == 3
    # Inside Engineering, 30 min becomes rank 1, and 10 min becomes rank 3
    assert dept_items[0]["rank"] == 1
    assert dept_items[0]["emp_code"] == "EMP01"
    assert dept_items[1]["rank"] == 1
    assert dept_items[1]["emp_code"] == "EMP02"
    assert dept_items[2]["rank"] == 3
    assert dept_items[2]["emp_code"] == "EMP04"

    # 4. Department with no late records
    res_empty = client.get("/analytics/leaderboard/late?month=2026-07&department=HR")
    assert res_empty.status_code == 200
    assert res_empty.json()["items"] == []


def test_late_leaderboard_validation():
    """Test validation constraints on month and limit."""
    client = TestClient(app)

    # Invalid month
    assert client.get("/analytics/leaderboard/late?month=2026-7").status_code == 422

    # Limit < 1
    assert client.get("/analytics/leaderboard/late?month=2026-07&limit=0").status_code == 422

    # Limit > 50
    assert client.get("/analytics/leaderboard/late?month=2026-07&limit=51").status_code == 422
