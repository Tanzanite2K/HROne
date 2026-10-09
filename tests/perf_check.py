"""100k-record performance + explain check (not part of the pytest suite).

    TEST_MONGO_URI=mongodb://localhost:27017 python -m tests.perf_check

Creates a throwaway database hrone_perf_<random> with ~100,000 attendance records, times every list/analytics
endpoint in-process, prints whether each explain plan uses IXSCAN without COLLSCAN, then drops the database.
"""
import json
import os
import random
import statistics
import time
import uuid
from datetime import date, datetime, timedelta, timezone

URI = os.environ.get("TEST_MONGO_URI", "mongodb://localhost:27017")
DB = f"hrone_perf_{uuid.uuid4().hex[:8]}"
os.environ["MONGO_URI"], os.environ["MONGO_DB"] = URI, DB

from fastapi.testclient import TestClient  # noqa: E402
from pymongo import MongoClient  # noqa: E402

import app.main as main  # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))
DEPTS = ["Engineering", "Sales", "Support", "Finance", "HR", "Ops", "Legal", "Marketing"]


def seed(db):
    rng = random.Random(7)
    emps = [{"emp_code": f"EMP{n:05d}", "name": f"Person {n}", "email": f"p{n}@example.com",
             "department": DEPTS[n % len(DEPTS)], "shift_start": "09:30", "shift_end": "18:30",
             "joined_on": (date(2025, 6, 1) + timedelta(days=rng.randint(0, 420))).isoformat(),
             "created_at": datetime(2025, 1, 1, tzinfo=timezone.utc)} for n in range(400)]
    db.employees.insert_many(emps)
    logs, day = [], date(2025, 10, 1)
    for _ in range(250):
        for e in emps:
            status = rng.choices(["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"], [70, 12, 5, 5, 8])[0]
            doc = {"emp_code": e["emp_code"], "date": day.isoformat(), "status": status, "punch_in": None,
                   "punch_out": None, "work_hours": None, "late_minutes": 0, "overtime_minutes": 0,
                   "half_day": False, "history": []}
            if status in ("PRESENT", "WFH", "ON_DUTY"):
                late = rng.choice([0] * 6 + [rng.randint(11, 90)])
                pin = datetime.combine(day, datetime.min.time(), IST) + timedelta(hours=9, minutes=30 + late)
                hours = round(rng.uniform(3, 10), 2)
                doc.update(punch_in=pin, punch_out=pin + timedelta(hours=hours), work_hours=hours,
                           late_minutes=late, half_day=hours < 4.5)
            logs.append(doc)
        day += timedelta(days=1)
    db.attendance_logs.insert_many(logs, ordered=False)
    return len(logs)


def main_():
    client = MongoClient(URI)
    db = client[DB]
    try:
        n = seed(db)
        api = TestClient(main.app)
        with api:  # runs startup -> indexes
            cases = [
                ("GET /attendance", "/attendance", {}, "attendance_list"),
                ("GET /attendance?status=LEAVE", "/attendance", {"status": "LEAVE", "page": 3}, "attendance_list"),
                ("GET /attendance?emp_code", "/attendance", {"emp_code": "EMP00042"}, "attendance_list"),
                ("monthly", "/analytics/employees/EMP00042/monthly", {"month": "2026-03"}, "employee_monthly"),
                ("summary", "/analytics/departments/summary", {"month": "2026-03"}, "department_summary"),
                ("leaderboard", "/analytics/leaderboard/late", {"month": "2026-03", "limit": 10}, "late_leaderboard"),
                ("trend 92d", "/analytics/departments/Sales/trend", {"from": "2026-01-01", "to": "2026-04-02"},
                 "department_trend"),
            ]
            print(f"database {DB}: {n} attendance records, MongoDB {client.server_info()['version']}")
            for label, path, params, explain_name in cases:
                timings = []
                for _ in range(3):
                    t = time.perf_counter()
                    r = api.get(path, params=params)
                    timings.append((time.perf_counter() - t) * 1000)
                    assert r.status_code == 200, r.text
                xparams = dict(params)
                if explain_name == "employee_monthly":
                    xparams["emp_code"] = "EMP00042"
                if explain_name == "department_trend":
                    xparams["department"] = "Sales"
                plan = json.dumps(api.get(f"/admin/explain/{explain_name}", params=xparams).json())
                ok = "IXSCAN" in plan and "COLLSCAN" not in plan
                print(f"{label:32s} median {statistics.median(timings):7.1f} ms   explain IXSCAN-only: {ok}")
    finally:
        client.drop_database(DB)
        client.close()


if __name__ == "__main__":
    main_()
