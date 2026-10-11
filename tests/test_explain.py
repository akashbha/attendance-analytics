from unittest.mock import MagicMock
import pytest
from fastapi.testclient import TestClient

import app.main as main_module
from app.main import app, ExplainEndpoint


@pytest.fixture(autouse=True)
def mock_explain_mongo(monkeypatch):
    """
    Mock db.command for explain queries in unit tests.
    """
    mock_db = MagicMock()
    mock_client = MagicMock()
    mock_client.__getitem__.return_value = mock_db

    mock_db.command.return_value = {
        "queryPlanner": {
            "winningPlan": {
                "stage": "IXSCAN",
                "indexName": "emp_code_1_date_1",
            }
        },
        "executionStats": {
            "executionSuccess": True,
            "nReturned": 10,
            "executionTimeMillis": 2,
        },
    }

    monkeypatch.setattr(main_module, "client", mock_client)
    monkeypatch.setattr(main_module, "db", mock_db)
    return mock_db


def test_explain_validation_errors():
    client = TestClient(app)

    # 1. Unknown endpoint enum -> 422
    assert client.get("/admin/explain/unknown_endpoint").status_code == 422

    # 2. employee_monthly missing params -> 422
    assert client.get("/admin/explain/employee_monthly").status_code == 422
    assert client.get("/admin/explain/employee_monthly?emp_code=EMP0001").status_code == 422
    assert client.get("/admin/explain/employee_monthly?month=2026-06").status_code == 422

    # 3. department_summary missing month -> 422
    assert client.get("/admin/explain/department_summary").status_code == 422

    # 4. late_leaderboard missing month -> 422
    assert client.get("/admin/explain/late_leaderboard").status_code == 422

    # 5. department_trend missing department/from/to -> 422
    assert client.get("/admin/explain/department_trend").status_code == 422
    assert client.get("/admin/explain/department_trend?department=Engineering").status_code == 422


def test_explain_successful_calls(mock_explain_mongo):
    client = TestClient(app)

    # 1. attendance_list (find command on attendance_logs)
    res1 = client.get("/admin/explain/attendance_list?emp_code=EMP0001&date_from=2026-06-01&date_to=2026-06-30")
    assert res1.status_code == 200
    data1 = res1.json()
    assert data1["endpoint"] == "attendance_list"
    assert data1["collection"] == "attendance_logs"
    assert "explain" in data1

    # 2. employee_monthly (aggregate command on attendance_logs)
    res2 = client.get("/admin/explain/employee_monthly?emp_code=EMP0001&month=2026-06")
    assert res2.status_code == 200
    data2 = res2.json()
    assert data2["endpoint"] == "employee_monthly"
    assert data2["collection"] == "attendance_logs"

    # 3. department_summary (aggregate command on employees)
    res3 = client.get("/admin/explain/department_summary?month=2026-06&department=Engineering")
    assert res3.status_code == 200
    data3 = res3.json()
    assert data3["endpoint"] == "department_summary"
    assert data3["collection"] == "employees"

    # 4. late_leaderboard (aggregate command on attendance_logs)
    res4 = client.get("/admin/explain/late_leaderboard?month=2026-06&limit=10")
    assert res4.status_code == 200
    data4 = res4.json()
    assert data4["endpoint"] == "late_leaderboard"
    assert data4["collection"] == "attendance_logs"

    # 5. department_trend (aggregate command on attendance_logs)
    res5 = client.get("/admin/explain/department_trend?department=Engineering&from=2026-06-01&to=2026-06-30")
    assert res5.status_code == 200
    data5 = res5.json()
    assert data5["endpoint"] == "department_trend"
    assert data5["collection"] == "attendance_logs"
