"""
Seed script to generate ~500 employees and ~100,000 attendance logs
in a dedicated benchmark database `attendance_perf`.
"""
import os
import random
from datetime import datetime, date, timedelta, timezone
from pymongo import MongoClient

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
PERF_DB = "attendance_perf"

client = MongoClient(MONGO_URI, tz_aware=True, serverSelectionTimeoutMS=2000)
db = client[PERF_DB]


def init_perf_indexes():
    indexes = [
        (db.employees, [("emp_code", 1)], {"unique": True}),
        (db.employees, [("department", 1), ("emp_code", 1)], {}),
        (db.employees, [("department", 1), ("joined_on", 1)], {}),
        (db.employees, [("joined_on", 1)], {}),
        (db.attendance_logs, [("emp_code", 1), ("date", 1)], {"unique": True}),
        (db.attendance_logs, [("date", -1), ("emp_code", 1)], {}),
    ]
    for coll, keys, kwargs in indexes:
        coll.create_index(keys, **kwargs)


def seed():
    print(f"Connecting to {MONGO_URI}, database '{PERF_DB}'...")
    db.employees.drop()
    db.attendance_logs.drop()
    init_perf_indexes()

    departments = ["Engineering", "Product", "Design", "Sales", "HR"]
    employees = []
    num_employees = 500

    print("Generating 500 employees...")
    for i in range(1, num_employees + 1):
        emp_code = f"EMP{i:04d}"
        dept = departments[(i - 1) % len(departments)]
        # joined_on between 2025-01-01 and 2026-03-01
        days_offset = random.randint(0, 420)
        joined_date = date(2025, 1, 1) + timedelta(days=days_offset)
        employees.append({
            "emp_code": emp_code,
            "name": f"Employee {i}",
            "email": f"emp{i}@example.com",
            "department": dept,
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": joined_date.isoformat(),
            "created_at": datetime.now(timezone.utc),
        })

    db.employees.insert_many(employees)
    print(f"Inserted {len(employees)} employees.")

    # Generate dates from 2025-11-01 to 2026-07-31 (~273 days) to yield ~100,000 logs
    start_date = date(2025, 11, 1)
    end_date = date(2026, 7, 31)
    all_dates = []
    curr = start_date
    while curr <= end_date:
        all_dates.append(curr)
        curr += timedelta(days=1)

    print(f"Generating attendance logs over {len(all_dates)} days...")
    batch = []
    total_logs = 0
    batch_size = 5000

    for d in all_dates:
        d_str = d.isoformat()
        is_wkend = d.weekday() >= 5

        for emp in employees:
            if emp["joined_on"] > d_str:
                continue

            if is_wkend:
                # 2% weekend shift
                if random.random() > 0.02:
                    continue
                status = "PRESENT"
                late_min = random.choice([0, 0, 15, 30])
                ot_min = random.choice([0, 30, 60])
                half = False
                work_h = 8.0
            else:
                # Weekdays: 80% PRESENT, 5% WFH, 5% ON_DUTY, 5% LEAVE, 5% ABSENT
                r = random.random()
                if r < 0.80:
                    status = "PRESENT"
                elif r < 0.85:
                    status = "WFH"
                elif r < 0.90:
                    status = "ON_DUTY"
                elif r < 0.95:
                    status = "LEAVE"
                else:
                    status = "ABSENT"

                if status in ("LEAVE", "ABSENT"):
                    late_min = 0
                    ot_min = 0
                    half = False
                    work_h = None
                else:
                    late_min = random.choice([0, 0, 0, 0, 10, 25, 45])
                    ot_min = random.choice([0, 0, 0, 30, 45, 60])
                    half = random.random() < 0.05
                    work_h = 4.0 if half else round(random.uniform(8.0, 9.5), 2)

            log_entry = {
                "emp_code": emp["emp_code"],
                "date": d_str,
                "status": status,
                "punch_in": datetime.combine(d, datetime.min.time(), tzinfo=timezone.utc) if work_h else None,
                "punch_out": datetime.combine(d, datetime.min.time(), tzinfo=timezone.utc) + timedelta(hours=work_h or 0) if work_h else None,
                "work_hours": work_h,
                "late_minutes": late_min,
                "overtime_minutes": ot_min,
                "half_day": half,
                "history": [],
            }
            batch.append(log_entry)

            if len(batch) >= batch_size:
                db.attendance_logs.insert_many(batch, ordered=False)
                total_logs += len(batch)
                batch.clear()
                print(f"Inserted {total_logs} logs...")

    if batch:
        db.attendance_logs.insert_many(batch, ordered=False)
        total_logs += len(batch)
        batch.clear()

    print(f"Total attendance logs inserted: {total_logs}")
    print("Database seeding complete!")


if __name__ == "__main__":
    seed()
