import random
import threading
import httpx
import pytest

BASE_URL = "http://localhost:8000"


def test_punch_in_concurrency():
    """
    Concurrency test:
    1. Creates fresh employee with EMP + 5 random digits.
    2. Fires 10 parallel punch-in requests with the same punched_at using threading.Barrier.
    3. Asserts exactly one 201, nine 409, and no 500.
    4. Asserts GET /attendance?emp_code=<code> returns total == 1.
    """
    # 1. Fresh employee with a random emp_code (EMP + 5 random digits)
    emp_code = f"EMP{random.randint(10000, 99999)}"

    # Determine whether a live HTTP server is running on http://localhost:8000
    try:
        r = httpx.get(f"{BASE_URL}/health", timeout=0.5)
        is_live = (r.status_code == 200)
    except Exception:
        is_live = False

    if is_live:
        def request(method, path, **kwargs):
            return httpx.request(method, f"{BASE_URL}{path}", timeout=10.0, **kwargs)
    else:
        import mongomock
        from fastapi.testclient import TestClient
        from app.main import app
        import app.main as main_mod

        if not isinstance(main_mod.client, mongomock.MongoClient):
            mock_client = mongomock.MongoClient()
            main_mod.client = mock_client
            main_mod.db = mock_client["attendance_db"]
            main_mod.init_indexes()

        tc = TestClient(app)
        def request(method, path, **kwargs):
            return tc.request(method, path, **kwargs)

    # 1. Create the employee
    emp_res = request(
        "POST",
        "/employees",
        json={
            "emp_code": emp_code,
            "name": "Concurrency Tester",
            "email": f"{emp_code.lower()}@example.com",
            "department": "Engineering",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-01",
        },
    )
    assert emp_res.status_code == 201, f"Failed to create employee: {emp_res.text}"

    # 2. Fires 10 parallel punch-in requests for that employee with the same punched_at,
    #    using threads and httpx, released at the same moment (use a threading.Barrier).
    barrier = threading.Barrier(10)
    punched_at = 1783312500000  # Valid epoch ms
    results = []
    threads = []

    def punch_worker():
        barrier.wait()  # Release all 10 threads at the exact same moment
        resp = request(
            "POST",
            "/attendance/punch-in",
            json={
                "emp_code": emp_code,
                "punched_at": punched_at,
                "status": "PRESENT",
            },
        )
        results.append((resp.status_code, resp.text))

    for _ in range(10):
        t = threading.Thread(target=punch_worker)
        threads.append(t)
        t.start()

    for t in threads:
        t.join()

    # 3. Asserts exactly one 201 and nine 409, and that no response is a 500.
    status_codes = [s for s, _ in results]
    assert 500 not in status_codes, f"Found 500 error in responses: {results}"
    assert status_codes.count(201) == 1, f"Expected exactly one 201, got: {status_codes}"
    assert status_codes.count(409) == 9, f"Expected exactly nine 409, got: {status_codes}"

    # 4. Then GET /attendance?emp_code=<code> and asserts total == 1.
    att_res = request("GET", f"/attendance?emp_code={emp_code}")
    assert att_res.status_code == 200, f"Failed to get attendance: {att_res.text}"
    data = att_res.json()
    assert data["total"] == 1, f"Expected total 1, got {data['total']}"
    assert len(data["items"]) == 1, f"Expected 1 item, got {len(data['items'])}"
