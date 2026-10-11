import json
import os
import sys
import time

sys.path.insert(0, ".")

os.environ["MONGO_DB"] = "attendance_perf"
os.environ["MONGO_URI"] = "mongodb://localhost:27017"

from fastapi.testclient import TestClient
import app.main as main_module
from app.main import app, init_indexes

# Ensure main_module connects to attendance_perf
main_module.db = main_module.client["attendance_perf"]
init_indexes()

client = TestClient(app)

endpoints_to_test = [
    {
        "name": "attendance_list",
        "explain_url": "/admin/explain/attendance_list?emp_code=EMP0001&date_from=2026-06-01&date_to=2026-06-30",
        "real_url": "/attendance?emp_code=EMP0001&date_from=2026-06-01&date_to=2026-06-30",
    },
    {
        "name": "employee_monthly",
        "explain_url": "/admin/explain/employee_monthly?emp_code=EMP0001&month=2026-06",
        "real_url": "/analytics/employees/EMP0001/monthly?month=2026-06",
    },
    {
        "name": "department_summary",
        "explain_url": "/admin/explain/department_summary?month=2026-06&department=Engineering",
        "real_url": "/analytics/departments/summary?month=2026-06&department=Engineering",
    },
    {
        "name": "late_leaderboard",
        "explain_url": "/admin/explain/late_leaderboard?month=2026-06&limit=10&department=Engineering",
        "real_url": "/analytics/leaderboard/late?month=2026-06&limit=10&department=Engineering",
    },
    {
        "name": "department_trend",
        "explain_url": "/admin/explain/department_trend?department=Engineering&from=2026-06-01&to=2026-06-30",
        "real_url": "/analytics/departments/Engineering/trend?from=2026-06-01&to=2026-06-30",
    },
]


def run_benchmark():
    print("=" * 80)
    print(f"BENCHMARK RESULTS ON 'attendance_perf' (~92k records)")
    print("=" * 80)

    for item in endpoints_to_test:
        name = item["name"]

        # 1. Test explain endpoint
        res_explain = client.get(item["explain_url"])
        if res_explain.status_code != 200:
            print(f"[{name}] EXPLAIN FAILED ({res_explain.status_code}): {res_explain.text}")
            continue

        explain_json = res_explain.json()
        raw_str = json.dumps(explain_json)

        has_ixscan = "IXSCAN" in raw_str
        has_collscan = "COLLSCAN" in raw_str
        coll_name = explain_json.get("collection")

        # 2. Test real endpoint & measure latency
        t0 = time.perf_counter()
        res_real = client.get(item["real_url"])
        latency_ms = (time.perf_counter() - t0) * 1000

        print(f"Endpoint: {name}")
        print(f"  Target Collection: {coll_name}")
        print(f"  IXSCAN Present?    {has_ixscan}")
        print(f"  COLLSCAN Present?  {has_collscan}")
        print(f"  Real Status:       {res_real.status_code}")
        print(f"  Response Time:     {latency_ms:.2f} ms")
        print("-" * 80)


if __name__ == "__main__":
    run_benchmark()
