"""End-to-end tests against a REAL MongoDB (6.0+, the grader uses 7.0) through a real uvicorn process.

Run:   TEST_MONGO_URI=mongodb://localhost:27017 pytest tests/test_integration.py -v
They are skipped (not failed) when no MongoDB answers at TEST_MONGO_URI. They only ever use and drop a freshly
named database `hrone_test_<random>` (see conftest.py).

Expected analytics values are computed independently: either written out by hand in the test (with the arithmetic
in a comment) or by the small reference functions at the bottom of this file, which loop over the fixture data in
plain Python and share no code with app/main.py.
"""
import json
import os
import socket
import subprocess
import sys
import time as clock
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal

import httpx
import pytest
from bson import json_util
from pymongo import MongoClient
from pymongo.errors import PyMongoError

from tests.conftest import ROOT, TEST_DB, TEST_MONGO_URI, redact
from tests.contract import check_response

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc


def _mongo_or_skip():
    try:
        probe = MongoClient(TEST_MONGO_URI, serverSelectionTimeoutMS=15000)  # Atlas: DNS + TLS handshake
        version = probe.server_info()["version"]
        probe.close()
        return version
    except PyMongoError as exc:
        if os.environ.get("REQUIRE_MONGO") == "1":  # CI: a missing database must fail the build, not skip
            pytest.fail(f"MongoDB required but not reachable at {redact(TEST_MONGO_URI)}: {type(exc).__name__}",
                        pytrace=False)
        pytest.skip(f"MongoDB not reachable at {redact(TEST_MONGO_URI)} ({type(exc).__name__}); integration tests skipped",
                    allow_module_level=True)


MONGO_VERSION = _mongo_or_skip()


# =========================================================================== fixtures
@pytest.fixture(scope="session")
def mdb():
    client = MongoClient(TEST_MONGO_URI, tz_aware=True)
    database = client[TEST_DB]
    yield database
    # Drop ONLY the exact database this run created (name checked against the generated one).
    assert database.name == TEST_DB and database.name.startswith("hrone_test_")
    client.drop_database(TEST_DB)
    client.close()


@pytest.fixture(scope="session")
def api(mdb):
    """Start the app exactly as the grader does: `uvicorn app.main:app --port <port>` from the repo root."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    env = {**os.environ, "MONGO_URI": TEST_MONGO_URI, "MONGO_DB": TEST_DB}
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(port)], cwd=ROOT, env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    base = f"http://127.0.0.1:{port}"
    started = clock.monotonic()
    while True:
        try:
            if httpx.get(f"{base}/health", timeout=1).status_code == 200:
                break
        except httpx.HTTPError:
            pass
        if clock.monotonic() - started > 20:
            proc.kill()
            pytest.fail("app not healthy within 20 s: " + redact(proc.stderr.read().decode(errors="replace")[-2000:]))
        clock.sleep(0.2)
    with httpx.Client(base_url=base, timeout=30) as http:
        yield http
    proc.terminate()
    proc.wait(timeout=10)


@pytest.fixture(autouse=True)
def clean(mdb, api):
    mdb.employees.delete_many({})
    mdb.attendance_logs.delete_many({})
    yield


# =========================================================================== helpers
def ms(y, mo, d, h=0, mi=0, s=0, extra_ms=0):
    """Epoch milliseconds of an IST wall-clock time."""
    return int(datetime(y, mo, d, h, mi, s, tzinfo=IST).timestamp()) * 1000 + extra_ms


def at(y, mo, d, h=0, mi=0, s=0):
    return datetime(y, mo, d, h, mi, s, tzinfo=IST).astimezone(UTC)


def employee(code, dept="Engineering", joined="2026-01-05", start="09:30", end="18:30", name=None):
    return {"emp_code": code, "name": name or f"Name {code}", "email": f"{code.lower()}@example.com",
            "department": dept, "shift_start": start, "shift_end": end, "joined_on": joined,
            "created_at": datetime(2026, 1, 1, tzinfo=UTC)}


def record(code, day, status="PRESENT", late=0, ot=0, wh=None, half=False, punch_in=None, punch_out=None,
           legacy=False):
    doc = {"emp_code": code, "date": day, "status": status, "punch_in": punch_in, "punch_out": punch_out,
           "work_hours": wh, "late_minutes": late, "overtime_minutes": ot}
    if status in ("PRESENT", "WFH", "ON_DUTY") and punch_in is None:
        y, m, d = map(int, day.split("-"))
        doc["punch_in"] = at(y, m, d, 9, 30)
    if not legacy:
        doc["half_day"] = half
        doc["history"] = []
    return doc


def create(api, code, **kw):
    body = {"emp_code": code, "name": "Test", "email": f"{code.lower()}@example.com", "department": "Engineering",
            "joined_on": "2026-01-05", **kw}
    r = api.post("/employees", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def punch_in(api, code, when, status=None):
    body = {"emp_code": code, "punched_at": when}
    if status:
        body["status"] = status
    return api.post("/attendance/punch-in", json=body)


def punch_out(api, code, when):
    return api.post("/attendance/punch-out", json={"emp_code": code, "punched_at": when})


def half_up(value, places):
    return float(Decimal(value).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP))


def plan_text(explain_response):
    return json.dumps(explain_response["explain"])


# =========================================================================== system
def test_health(api):
    r = api.get("/health")
    assert r.status_code == 200 and check_response(r, "/health", "get") == {"status": "ok"}


def test_indexes_created_at_startup(mdb, api):
    att = mdb.attendance_logs.index_information()
    emp = mdb.employees.index_information()
    assert emp["uniq_emp_code"]["unique"] and att["uniq_emp_code_date"]["unique"]
    assert {"date_desc_emp_code", "emp_code_punch_in_desc", "status_date_emp_code"} <= set(att)
    assert {"department_emp_code", "department_joined_on", "joined_on"} <= set(emp)


# =========================================================================== employees
def test_create_employee_contract_and_storage(api, mdb):
    before = int(clock.time() * 1000)
    r = api.post("/employees", json={"emp_code": "EMP0001", "name": "Asha Rao", "email": "asha@example.com",
                                     "department": "Engineering", "joined_on": "2026-01-05", "_id": "x",
                                     "created_at": 5})
    assert r.status_code == 201
    body = check_response(r, "/employees", "post")
    assert body["shift_start"] == "09:30" and body["shift_end"] == "18:30"  # defaults
    assert before - 1000 <= body["created_at"] <= int(clock.time() * 1000) + 1000  # server clock, not client
    stored = mdb.employees.find_one({"emp_code": "EMP0001"})
    assert isinstance(stored["created_at"], datetime) and stored["joined_on"] == "2026-01-05"


def test_duplicate_employee_is_409(api):
    create(api, "EMP0001")
    r = api.post("/employees", json={"emp_code": "EMP0001", "name": "X", "email": "x@y.io", "department": "D",
                                     "joined_on": "2026-01-01"})
    assert r.status_code == 409
    check_response(r, "/employees", "post")


def test_concurrent_duplicate_creates_yield_one_201(api, mdb):
    body = {"emp_code": "EMP0042", "name": "X", "email": "x@y.io", "department": "D", "joined_on": "2026-01-01"}
    with ThreadPoolExecutor(12) as pool:
        codes = sorted(r.status_code for r in pool.map(lambda _: api.post("/employees", json=body), range(12)))
    assert codes == [201] + [409] * 11
    assert mdb.employees.count_documents({"emp_code": "EMP0042"}) == 1


def test_overnight_employee_allowed(api):
    assert create(api, "EMP0005", shift_start="22:00", shift_end="06:00")["shift_end"] == "06:00"


def test_list_employees_filter_sort_pagination(api, mdb):
    mdb.employees.insert_many([employee(f"EMP{n:04d}", dept="Sales" if n % 2 else "Engineering")
                               for n in [5, 3, 1, 4, 2, 6, 7]])
    r = api.get("/employees")
    body = check_response(r, "/employees", "get")
    assert [e["emp_code"] for e in body["items"]] == [f"EMP{n:04d}" for n in range(1, 8)]
    assert body["total"] == 7 and body["page"] == 1 and body["page_size"] == 20

    page1 = api.get("/employees", params={"department": "Sales", "page_size": 2}).json()
    page2 = api.get("/employees", params={"department": "Sales", "page": 2, "page_size": 2}).json()
    page3 = api.get("/employees", params={"department": "Sales", "page": 3, "page_size": 2}).json()
    assert [e["emp_code"] for e in page1["items"]] == ["EMP0001", "EMP0003"]  # page 1 is the FIRST page
    assert [e["emp_code"] for e in page2["items"]] == ["EMP0005", "EMP0007"]
    assert page3["items"] == [] and page3["total"] == 4  # total reflects the filter, not the collection
    assert api.get("/employees", params={"department": "sales"}).json()["total"] == 0  # case-sensitive
    assert api.get("/employees", params={"page_size": 100}).status_code == 200


# =========================================================================== punch-in
def test_punch_in_creates_record(api, mdb):
    create(api, "EMP0001")
    r = punch_in(api, "EMP0001", ms(2026, 7, 6, 10, 5, 0, extra_ms=900))
    assert r.status_code == 201
    rec = check_response(r, "/attendance/punch-in", "post")
    assert rec == {"emp_code": "EMP0001", "date": "2026-07-06", "status": "PRESENT",
                   "punch_in": ms(2026, 7, 6, 10, 5), "punch_out": None, "work_hours": None, "late_minutes": 35,
                   "overtime_minutes": 0, "half_day": False, "history": []}
    stored = mdb.attendance_logs.find_one({"emp_code": "EMP0001"})
    assert stored["punch_in"] == at(2026, 7, 6, 10, 5)  # BSON date, truncated to the second
    assert stored["history"] == [] and "id" not in stored


@pytest.mark.parametrize("hh, mm, ss, late", [(9, 30, 0, 0), (9, 40, 0, 0), (9, 40, 1, 10), (10, 15, 59, 45)])
def test_punch_in_grace_boundaries(api, hh, mm, ss, late):
    create(api, "EMP0001")
    assert punch_in(api, "EMP0001", ms(2026, 7, 6, hh, mm, ss)).json()["late_minutes"] == late


def test_punch_in_uses_ist_date_not_utc(api):
    create(api, "EMP0001")
    rec = punch_in(api, "EMP0001", ms(2026, 7, 7, 0, 30)).json()  # 2026-07-06 19:00 UTC
    assert rec["date"] == "2026-07-07"


def test_punch_in_overnight_shift(api):
    create(api, "EMP0005", shift_start="22:00", shift_end="06:00")
    rec = punch_in(api, "EMP0005", ms(2026, 7, 7, 1, 0)).json()  # 01:00 on the 7th -> shift of the 6th
    assert rec["date"] == "2026-07-06" and rec["late_minutes"] == 180
    rec = punch_in(api, "EMP0005", ms(2026, 7, 7, 21, 55)).json()
    assert rec["date"] == "2026-07-07" and rec["late_minutes"] == 0


def test_punch_in_status_and_default_now(api):
    create(api, "EMP0001")
    assert punch_in(api, "EMP0001", ms(2026, 7, 6, 9, 0), status="WFH").json()["status"] == "WFH"
    r = api.post("/attendance/punch-in", json={"emp_code": "EMP0001"})
    assert r.status_code == 201
    assert abs(r.json()["punch_in"] - int(clock.time() * 1000)) < 5000 and r.json()["punch_in"] % 1000 == 0


def test_punch_in_unknown_employee_404(api):
    r = punch_in(api, "EMP9999", ms(2026, 7, 6, 9, 0))
    assert r.status_code == 404 and check_response(r, "/attendance/punch-in", "post")["detail"]


def test_punch_in_duplicate_409(api):
    create(api, "EMP0001")
    assert punch_in(api, "EMP0001", ms(2026, 7, 6, 9, 0)).status_code == 201
    r = punch_in(api, "EMP0001", ms(2026, 7, 6, 14, 0))
    assert r.status_code == 409
    check_response(r, "/attendance/punch-in", "post")


def test_concurrent_identical_punch_ins_yield_one_201(api, mdb):
    create(api, "EMP0001")
    when = ms(2026, 7, 6, 9, 31)
    with ThreadPoolExecutor(16) as pool:
        codes = sorted(r.status_code for r in pool.map(lambda _: punch_in(api, "EMP0001", when), range(16)))
    assert codes == [201] + [409] * 15
    assert mdb.attendance_logs.count_documents({"emp_code": "EMP0001", "date": "2026-07-06"}) == 1


# =========================================================================== punch-out
def test_punch_out_computes_fields(api):
    create(api, "EMP0003")
    punch_in(api, "EMP0003", ms(2026, 7, 6, 10, 5))
    r = punch_out(api, "EMP0003", ms(2026, 7, 6, 19, 10, 0, extra_ms=999))
    assert r.status_code == 200
    rec = check_response(r, "/attendance/punch-out", "post")
    assert rec["punch_out"] == ms(2026, 7, 6, 19, 10)
    assert (rec["work_hours"], rec["overtime_minutes"], rec["half_day"], rec["late_minutes"]) == (9.08, 40, False, 35)
    assert rec["history"] == []  # punches never write history


@pytest.mark.parametrize("seconds, hours, half", [(16181, 4.49, True), (16182, 4.5, False), (29250, 8.13, False)])
def test_punch_out_half_up_and_half_day(api, seconds, hours, half):
    create(api, "EMP0001")
    start = ms(2026, 7, 6, 9, 0)
    punch_in(api, "EMP0001", start)
    rec = punch_out(api, "EMP0001", start + seconds * 1000).json()
    assert (rec["work_hours"], rec["half_day"]) == (hours, half)


@pytest.mark.parametrize("out, overtime", [((18, 59, 59), 0), ((19, 0, 0), 30), ((18, 35, 0), 0)])
def test_punch_out_overtime_threshold(api, out, overtime):
    create(api, "EMP0001")
    punch_in(api, "EMP0001", ms(2026, 7, 6, 9, 28))
    assert punch_out(api, "EMP0001", ms(2026, 7, 6, *out)).json()["overtime_minutes"] == overtime


def test_punch_out_across_midnight(api):
    create(api, "EMP0005", shift_start="22:00", shift_end="06:00")
    punch_in(api, "EMP0005", ms(2026, 7, 6, 21, 55))
    rec = punch_out(api, "EMP0005", ms(2026, 7, 7, 6, 40)).json()
    assert (rec["date"], rec["work_hours"], rec["overtime_minutes"]) == ("2026-07-06", 8.75, 40)


def test_punch_out_24h_limit(api):
    create(api, "EMP0001")
    start = ms(2026, 7, 6, 9, 0)
    punch_in(api, "EMP0001", start)
    r = punch_out(api, "EMP0001", start + 86_400_000 + 1000)
    assert r.status_code == 422 and check_response(r, "/attendance/punch-out", "post")
    r = punch_out(api, "EMP0001", start + 86_400_000)  # exactly 24 h is allowed
    assert r.status_code == 200 and r.json()["work_hours"] == 24.0


def test_punch_out_not_after_punch_in(api):
    create(api, "EMP0001")
    start = ms(2026, 7, 6, 9, 0)
    punch_in(api, "EMP0001", start)
    assert punch_out(api, "EMP0001", start + 500).status_code == 422  # truncates to the same second
    assert punch_out(api, "EMP0001", start - 60_000).status_code == 404  # no punch-in at or before


def test_punch_out_errors(api):
    assert punch_out(api, "EMP9999", ms(2026, 7, 6, 18, 0)).status_code == 404
    create(api, "EMP0001")
    assert punch_out(api, "EMP0001", ms(2026, 7, 6, 18, 0)).status_code == 404
    punch_in(api, "EMP0001", ms(2026, 7, 6, 9, 0))
    assert punch_out(api, "EMP0001", ms(2026, 7, 6, 18, 0)).status_code == 200
    r = punch_out(api, "EMP0001", ms(2026, 7, 6, 18, 5))
    assert r.status_code == 409 and check_response(r, "/attendance/punch-out", "post")


def test_punch_out_picks_most_recent_record(api):
    create(api, "EMP0001")
    punch_in(api, "EMP0001", ms(2026, 7, 6, 9, 0))
    punch_in(api, "EMP0001", ms(2026, 7, 7, 9, 0))
    assert punch_out(api, "EMP0001", ms(2026, 7, 7, 18, 0)).json()["date"] == "2026-07-07"
    assert punch_out(api, "EMP0001", ms(2026, 7, 6, 18, 0)).json()["date"] == "2026-07-06"


def test_concurrent_punch_outs_yield_one_200(api, mdb):
    create(api, "EMP0001")
    punch_in(api, "EMP0001", ms(2026, 7, 6, 9, 0))
    with ThreadPoolExecutor(16) as pool:
        codes = sorted(r.status_code for r in pool.map(
            lambda i: punch_out(api, "EMP0001", ms(2026, 7, 6, 18, 0, i)), range(16)))
    assert codes == [200] + [409] * 15


# =========================================================================== GET /attendance
def test_list_attendance_sort_filter_pagination(api, mdb):
    mdb.employees.insert_many([employee("EMP0001"), employee("EMP0002")])
    mdb.attendance_logs.insert_many([
        record("EMP0002", "2026-07-06"), record("EMP0001", "2026-07-06"), record("EMP0001", "2026-07-07"),
        record("EMP0001", "2026-07-08", status="LEAVE", punch_in=None), record("EMP0002", "2026-07-08", legacy=True),
    ])
    body = check_response(api.get("/attendance"), "/attendance", "get")
    assert [(i["date"], i["emp_code"]) for i in body["items"]] == [
        ("2026-07-08", "EMP0001"), ("2026-07-08", "EMP0002"), ("2026-07-07", "EMP0001"),
        ("2026-07-06", "EMP0001"), ("2026-07-06", "EMP0002")]
    legacy = body["items"][1]
    assert legacy["half_day"] is False and legacy["history"] == []
    q = {"emp_code": "EMP0001", "date_from": "2026-07-07", "date_to": "2026-07-08"}
    assert api.get("/attendance", params=q).json()["total"] == 2
    assert api.get("/attendance", params={"status": "LEAVE"}).json()["total"] == 1
    p2 = api.get("/attendance", params={"page": 2, "page_size": 2}).json()
    assert [(i["date"], i["emp_code"]) for i in p2["items"]] == [("2026-07-07", "EMP0001"), ("2026-07-06", "EMP0001")]
    assert p2["total"] == 5
    assert api.get("/attendance", params={"date_from": "2026-07-08", "date_to": "2026-07-08"}).json()["total"] == 2


def test_sample_data_round_trip(api, mdb):
    """The nine sample documents (Extended JSON) are served with epoch-ms instants, including inside history."""
    opts = json_util.JSONOptions(tz_aware=True)
    for name in ("employees", "attendance_logs"):
        mdb[name].insert_many(json_util.loads((ROOT / "sample_data" / f"{name}.json").read_text(), json_options=opts))
    body = check_response(api.get("/attendance", params={"page_size": 100}), "/attendance", "get")
    assert body["total"] == 9
    corrected = next(i for i in body["items"] if i["emp_code"] == "EMP0001" and i["date"] == "2026-07-07")
    change = corrected["history"][0]["changes"]["punch_in"]
    # sample doc 8: 2026-07-07T04:35Z -> 03:58Z (10:05 -> 09:28 IST), as epoch ms inside history
    assert change == {"from": int(datetime(2026, 7, 7, 4, 35, tzinfo=UTC).timestamp() * 1000),
                      "to": int(datetime(2026, 7, 7, 3, 58, tzinfo=UTC).timestamp() * 1000)}
    assert corrected["work_hours"] == 9.03 and corrected["history"][0]["changes"]["late_minutes"] == {"from": 35, "to": 0}
    assert corrected["history"][0]["at"] == int(datetime(2026, 7, 8, 5, 0, tzinfo=UTC).timestamp() * 1000)
    emp = check_response(api.get("/employees"), "/employees", "get")["items"][0]
    assert emp["created_at"] == int(datetime(2026, 1, 5, 4, 30, tzinfo=UTC).timestamp() * 1000)


# =========================================================================== PATCH (regularize)
def _setup_closed_day(api):
    create(api, "EMP0001")
    punch_in(api, "EMP0001", ms(2026, 7, 7, 10, 5))
    punch_out(api, "EMP0001", ms(2026, 7, 7, 18, 30))
    return "/attendance/EMP0001/2026-07-07"


def test_regularize_punch_in_recomputes_and_audits(api):
    url = _setup_closed_day(api)
    r = api.patch(url, json={"punch_in": ms(2026, 7, 7, 9, 28), "reason": "biometric glitch",
                             "regularized_by": "hr.admin", "late_minutes": 999, "work_hours": 1})
    assert r.status_code == 200
    rec = check_response(r, "/attendance/{emp_code}/{date}", "patch")
    assert (rec["late_minutes"], rec["work_hours"], rec["status"]) == (0, 9.03, "PRESENT")  # client values ignored
    assert rec["punch_out"] == ms(2026, 7, 7, 18, 30)  # omitted field unchanged
    [entry] = rec["history"]
    assert entry["by"] == "hr.admin" and entry["reason"] == "biometric glitch"
    assert abs(entry["at"] - clock.time() * 1000) < 10_000
    assert entry["changes"] == {
        "punch_in": {"from": ms(2026, 7, 7, 10, 5), "to": ms(2026, 7, 7, 9, 28)},
        "late_minutes": {"from": 35, "to": 0},
        "work_hours": {"from": 8.42, "to": 9.03},
    }  # overtime_minutes / half_day / status did not change, so they are absent


def test_regularize_history_is_append_only_and_stored_as_dates(api, mdb):
    url = _setup_closed_day(api)
    api.patch(url, json={"status": "WFH", "reason": "worked from home", "regularized_by": "a"})
    rec = api.patch(url, json={"punch_out": ms(2026, 7, 7, 19, 30), "reason": "forgot to punch",
                               "regularized_by": "b"}).json()
    assert [h["by"] for h in rec["history"]] == ["a", "b"]
    assert rec["history"][0]["changes"] == {"status": {"from": "PRESENT", "to": "WFH"}}
    assert rec["history"][1]["changes"]["overtime_minutes"] == {"from": 0, "to": 60}
    stored = mdb.attendance_logs.find_one({"emp_code": "EMP0001"})
    assert isinstance(stored["history"][1]["changes"]["punch_out"]["to"], datetime)
    assert isinstance(stored["history"][0]["at"], datetime)


def test_regularize_to_absent_clears_everything(api):
    url = _setup_closed_day(api)
    rec = api.patch(url, json={"status": "ABSENT", "reason": "did not work", "regularized_by": "hr"}).json()
    assert (rec["punch_in"], rec["punch_out"], rec["work_hours"], rec["late_minutes"]) == (None, None, None, 0)
    assert set(rec["history"][0]["changes"]) == {"status", "punch_in", "punch_out", "work_hours", "late_minutes"}
    # back to present needs a punch_in
    r = api.patch(url, json={"status": "PRESENT", "reason": "was present", "regularized_by": "hr"})
    assert r.status_code == 422
    rec = api.patch(url, json={"status": "PRESENT", "punch_in": ms(2026, 7, 7, 9, 45), "reason": "was present",
                               "regularized_by": "hr"}).json()
    assert (rec["late_minutes"], rec["punch_out"], rec["work_hours"]) == (15, None, None)


@pytest.mark.parametrize("extra", [{"status": "LEAVE", "punch_in": 1}, {"status": "ABSENT", "punch_out": 1}])
def test_regularize_absent_with_punches_is_422(api, extra):
    url = _setup_closed_day(api)
    key = "punch_in" if "punch_in" in extra else "punch_out"
    body = {**extra, key: ms(2026, 7, 7, 9, 30), "reason": "some reason", "regularized_by": "hr"}
    assert api.patch(url, json=body).status_code == 422


def test_regularize_validation_of_final_record(api):
    url = _setup_closed_day(api)
    base = {"reason": "some reason", "regularized_by": "hr"}
    # punch_in moved to another attendance date
    assert api.patch(url, json={**base, "punch_in": ms(2026, 7, 8, 9, 30)}).status_code == 422
    # punch_out before punch_in / equal / beyond 24 h
    assert api.patch(url, json={**base, "punch_out": ms(2026, 7, 7, 10, 0)}).status_code == 422
    assert api.patch(url, json={**base, "punch_out": ms(2026, 7, 7, 10, 5)}).status_code == 422
    assert api.patch(url, json={**base, "punch_out": ms(2026, 7, 8, 10, 5, 1)}).status_code == 422
    # a new punch_in that would leave the STORED punch_out before it
    assert api.patch(url, json={**base, "punch_in": ms(2026, 7, 7, 19, 0)}).status_code == 422
    # no-op: same values, or nothing editable supplied
    assert api.patch(url, json={**base, "status": "PRESENT"}).status_code == 422
    assert api.patch(url, json={**base, "punch_in": ms(2026, 7, 7, 10, 5, 0, extra_ms=400)}).status_code == 422
    assert api.patch(url, json=base).status_code == 422
    r = api.get("/attendance").json()["items"][0]
    assert r["history"] == []  # none of the rejected requests wrote anything


def test_regularize_404s(api):
    create(api, "EMP0001")
    body = {"status": "LEAVE", "reason": "some reason", "regularized_by": "hr"}
    r = api.patch("/attendance/EMP9999/2026-07-07", json=body)
    assert r.status_code == 404 and check_response(r, "/attendance/{emp_code}/{date}", "patch")
    assert api.patch("/attendance/EMP0001/2026-07-07", json=body).status_code == 404


def test_regularize_overnight_punch_in_stays_on_shift_date(api):
    create(api, "EMP0005", shift_start="22:00", shift_end="06:00")
    punch_in(api, "EMP0005", ms(2026, 7, 6, 22, 30))
    body = {"punch_in": ms(2026, 7, 7, 0, 30), "reason": "late bus", "regularized_by": "hr"}
    rec = api.patch("/attendance/EMP0005/2026-07-06", json=body).json()  # 00:30 on the 7th = shift of the 6th
    assert rec["late_minutes"] == 150


def test_regularize_legacy_record(api, mdb):
    mdb.employees.insert_one(employee("EMP0002"))
    mdb.attendance_logs.insert_one(record("EMP0002", "2026-07-14", wh=8.98, legacy=True,
                                          punch_in=at(2026, 7, 14, 9, 32), punch_out=at(2026, 7, 14, 18, 31)))
    rec = api.patch("/attendance/EMP0002/2026-07-14", json={"status": "ON_DUTY", "reason": "client visit",
                                                           "regularized_by": "hr"}).json()
    assert rec["history"][0]["changes"] == {"status": {"from": "PRESENT", "to": "ON_DUTY"}}
    assert rec["half_day"] is False


def test_concurrent_regularizations_never_lose_history(api, mdb):
    url = _setup_closed_day(api)
    bodies = [{"punch_in": ms(2026, 7, 7, 9, 30 + i), "reason": f"fix number {i}", "regularized_by": f"u{i}"}
              for i in range(10)]
    with ThreadPoolExecutor(10) as pool:
        results = list(pool.map(lambda b: api.patch(url, json=b), bodies))
    codes = [r.status_code for r in results]
    assert set(codes) <= {200, 409} and 200 in codes
    stored = mdb.attendance_logs.find_one({"emp_code": "EMP0001"})
    assert len(stored["history"]) == codes.count(200)  # every success is in the audit trail, none overwritten
    winners = {r.json()["history"][-1]["by"] for r in results if r.status_code == 200}
    assert winners == {h["by"] for h in stored["history"]}


# =========================================================================== analytics fixture
# July 2026: the 1st is a Wednesday; weekdays = 1-3, 6-10, 13-17, 20-24, 27-31 = 23 working days.
EMPLOYEES = [
    employee("EMP1001", "Engineering", "2026-01-05"),
    employee("EMP1002", "Engineering", "2026-07-15"),   # joins mid-month: 13 working days
    employee("EMP1003", "Engineering", "2026-08-03"),   # joins after the month: not in July headcount
    employee("EMP1004", "Sales", "2026-02-01"),         # no logs at all, still counted
    employee("EMP1005", "Sales", "2026-02-01"),
    employee("EMP1006", "Support", "2026-03-10", start="22:00", end="06:00"),
    employee("EMP1007", "Legal", "2026-09-01"),         # department with zero July headcount: omitted
    employee("EMP1008", "Engineering", "2026-07-10"),   # 16 working days, one half day -> 3.125% -> 3.13
]
LOGS = [
    record("EMP1001", "2026-06-30", late=50, wh=8.0),                      # outside July
    record("EMP1001", "2026-07-01", wh=9.12),
    record("EMP1001", "2026-07-02", late=35, wh=8.13, ot=40),
    record("EMP1001", "2026-07-03", status="WFH", wh=3.5, half=True),
    record("EMP1001", "2026-07-04", late=15, ot=60, wh=4.0, half=True),   # Saturday: totals yes, present_days no
    record("EMP1001", "2026-07-06", status="LEAVE", punch_in=None),
    record("EMP1001", "2026-07-07", status="ABSENT", punch_in=None),
    record("EMP1001", "2026-07-08", status="ON_DUTY", late=12, wh=8.0),
    record("EMP1001", "2026-07-09"),                                       # open record: no work_hours
    record("EMP1001", "2026-07-10", wh=9.0, legacy=True),                  # no half_day / history fields
    record("EMP1001", "2026-08-01", late=50, wh=8.0),                      # outside July
    record("EMP1002", "2026-07-15", wh=8.12),
    record("EMP1002", "2026-07-16", late=35, wh=8.0),
    record("EMP1002", "2026-07-20", wh=4.0, half=True),
    record("EMP1005", "2026-07-01", late=35, wh=8.0),
    record("EMP1005", "2026-07-02", status="LEAVE", punch_in=None),
    record("EMP1005", "2026-07-03", status="ON_DUTY", wh=7.5),
    record("EMP1006", "2026-07-06", wh=8.75, ot=40),
    record("EMP1006", "2026-07-11", late=20, wh=8.0),                      # Saturday
    record("EMP1008", "2026-07-13", wh=4.0, half=True),
    record("EMP9999", "2026-07-02", late=500, wh=8.0),                     # orphan: employee does not exist
]


@pytest.fixture
def analytics_data(mdb):
    mdb.employees.insert_many([dict(e) for e in EMPLOYEES])
    mdb.attendance_logs.insert_many([dict(r) for r in LOGS])


def test_employee_monthly_hand_computed(api, analytics_data):
    r = api.get("/analytics/employees/EMP1001/monthly", params={"month": "2026-07"})
    body = check_response(r, "/analytics/employees/{emp_code}/monthly", "get")
    # present_days (Mon-Fri only): 07-01 1 + 07-02 1 + 07-03 0.5 (half) + 07-08 1 + 07-09 1 + 07-10 1 = 5.5
    # late: 07-02 35, 07-04 15 (Saturday still counts), 07-08 12 -> count 3, total 62. Overtime 40 + 60 = 100.
    assert body == {"emp_code": "EMP1001", "month": "2026-07", "working_days": 23, "present_days": 5.5,
                    "leave_days": 1, "late_count": 3, "total_late_minutes": 62, "total_overtime_minutes": 100,
                    "attendance_pct": 23.91}  # 5.5 / 23 * 100 = 23.913...


def test_employee_monthly_joiners_and_rounding(api, analytics_data):
    mid = api.get("/analytics/employees/EMP1002/monthly", params={"month": "2026-07"}).json()
    assert (mid["working_days"], mid["present_days"], mid["attendance_pct"]) == (13, 2.5, 19.23)  # 2.5/13
    half_up_case = api.get("/analytics/employees/EMP1008/monthly", params={"month": "2026-07"}).json()
    assert (half_up_case["working_days"], half_up_case["attendance_pct"]) == (16, 3.13)  # 3.125 half-up
    future = api.get("/analytics/employees/EMP1003/monthly", params={"month": "2026-07"}).json()
    assert (future["working_days"], future["attendance_pct"]) == (0, None)
    empty = api.get("/analytics/employees/EMP1004/monthly", params={"month": "2026-07"}).json()
    assert (empty["present_days"], empty["attendance_pct"], empty["late_count"]) == (0, 0.0, 0)
    assert api.get("/analytics/employees/EMP1001/monthly", params={"month": "2026-02"}).json()["working_days"] == 20


def test_employee_monthly_matches_reference_for_everyone(api, analytics_data):
    for emp in EMPLOYEES:
        got = api.get(f"/analytics/employees/{emp['emp_code']}/monthly", params={"month": "2026-07"}).json()
        assert got == reference_monthly(emp, "2026-07"), emp["emp_code"]


def test_employee_monthly_unknown_404(api, analytics_data):
    r = api.get("/analytics/employees/EMP9999/monthly", params={"month": "2026-07"})
    assert r.status_code == 404 and check_response(r, "/analytics/employees/{emp_code}/monthly", "get")


def test_department_summary_hand_computed(api, analytics_data):
    body = check_response(api.get("/analytics/departments/summary", params={"month": "2026-07"}),
                          "/analytics/departments/summary", "get")
    by_dept = {i["department"]: i for i in body["items"]}
    assert [i["department"] for i in body["items"]] == ["Engineering", "Sales", "Support"]  # Legal omitted
    sales = by_dept["Sales"]
    # EMP1004 (no logs) + EMP1005. avg over 2 qualifying records: (8.0 + 7.5) / 2 = 7.75
    assert sales == {"department": "Sales", "headcount": 2, "present_days": 2.0, "avg_work_hours": 7.75,
                     "late_count": 1, "total_late_minutes": 35, "leave_count": 1, "on_duty_count": 1}
    eng = by_dept["Engineering"]
    assert eng["headcount"] == 3  # EMP1001, EMP1002, EMP1008; EMP1003 joins in August
    assert eng["total_late_minutes"] == 35 + 15 + 12 + 35  # orphan EMP9999's 500 is ignored
    assert body["items"] == reference_summary("2026-07")


def test_department_summary_filter_and_average_over_records(api, mdb):
    mdb.employees.insert_many([employee("EMP2001", "Avg"), employee("EMP2002", "Avg"), employee("EMP2003", "Other")])
    mdb.attendance_logs.insert_many([
        record("EMP2001", "2026-07-01", wh=8.12), record("EMP2001", "2026-07-02", wh=8.13),
        record("EMP2001", "2026-07-03", wh=8.12), record("EMP2002", "2026-07-01", wh=8.13),
        record("EMP2002", "2026-07-02", status="LEAVE", punch_in=None, wh=None),
    ])
    body = api.get("/analytics/departments/summary", params={"month": "2026-07", "department": "Avg"}).json()
    # records: 8.12, 8.13, 8.12, 8.13 -> 8.125 -> 8.13 half-up (per-employee average of averages would differ)
    assert [i["department"] for i in body["items"]] == ["Avg"]
    assert body["items"][0]["avg_work_hours"] == 8.13
    none = api.get("/analytics/departments/summary", params={"month": "2026-07", "department": "Other"}).json()
    assert none["items"][0]["avg_work_hours"] is None and none["items"][0]["headcount"] == 1
    assert api.get("/analytics/departments/summary", params={"month": "2026-07", "department": "Nope"}).json() == {
        "month": "2026-07", "items": []}


def test_department_summary_average_is_not_average_of_averages(api, mdb):
    mdb.employees.insert_many([employee("EMP2001", "A"), employee("EMP2002", "A")])
    mdb.attendance_logs.insert_many([record("EMP2001", f"2026-07-0{d}", wh=8.0) for d in (1, 2, 3)]
                                    + [record("EMP2002", "2026-07-01", wh=4.0)])
    item = api.get("/analytics/departments/summary", params={"month": "2026-07"}).json()["items"][0]
    assert item["avg_work_hours"] == 7.0  # (8+8+8+4)/4, not (8+4)/2 = 6.0


# =========================================================================== leaderboard
@pytest.fixture
def leaderboard_data(mdb):
    people = [("EMP3001", "A"), ("EMP3002", "B"), ("EMP3003", "A"), ("EMP3004", "B"), ("EMP3005", "A"),
              ("EMP3006", "B")]
    mdb.employees.insert_many([employee(code, dept, name=f"N{code}") for code, dept in people])
    totals = {"EMP3001": [60, 40], "EMP3003": [80], "EMP3002": [50, 30], "EMP3005": [50], "EMP3004": [25, 25],
              "EMP3006": []}
    docs = []
    for code, lates in totals.items():
        for i, late in enumerate(lates):
            docs.append(record(code, f"2026-07-{i + 6:02d}", late=late))
        docs.append(record(code, "2026-07-20", late=0))  # on time: not counted
    docs += [record("EMP3006", "2026-06-30", late=300), record("EMP9999", "2026-07-06", late=999)]
    mdb.attendance_logs.insert_many(docs)
    # totals: EMP3001 100, EMP3002 80, EMP3003 80, EMP3004 50, EMP3005 50, EMP3006 0 (June does not count)


def _board(api, **params):
    r = api.get("/analytics/leaderboard/late", params={"month": "2026-07", **params})
    return check_response(r, "/analytics/leaderboard/late", "get")["items"]


def test_leaderboard_competition_ranking(api, leaderboard_data):
    rows = _board(api)
    assert [(r["rank"], r["emp_code"], r["total_late_minutes"], r["late_count"]) for r in rows] == [
        (1, "EMP3001", 100, 2), (2, "EMP3002", 80, 2), (2, "EMP3003", 80, 1), (4, "EMP3004", 50, 2),
        (4, "EMP3005", 50, 1)]
    assert rows[0]["name"] == "NEMP3001" and rows[0]["department"] == "A"


@pytest.mark.parametrize("limit, codes", [
    (1, ["EMP3001"]),
    (2, ["EMP3001", "EMP3002", "EMP3003"]),             # tie at the cutoff: both kept (3 rows for limit 2)
    (3, ["EMP3001", "EMP3002", "EMP3003"]),             # rank 4 is beyond 3
    (4, ["EMP3001", "EMP3002", "EMP3003", "EMP3004", "EMP3005"]),
])
def test_leaderboard_limit_after_ranking(api, leaderboard_data, limit, codes):
    assert [r["emp_code"] for r in _board(api, limit=limit)] == codes


def test_leaderboard_department_ranks_inside_department(api, leaderboard_data):
    rows = _board(api, department="B")
    assert [(r["rank"], r["emp_code"]) for r in rows] == [(1, "EMP3002"), (2, "EMP3004")]
    rows = _board(api, department="A", limit=2)
    assert [(r["rank"], r["emp_code"]) for r in rows] == [(1, "EMP3001"), (2, "EMP3003")]
    assert _board(api, department="Nope") == []


# =========================================================================== trend
@pytest.fixture
def trend_data(mdb):
    mdb.employees.insert_many([
        employee("EMP4001", "Ops", "2026-06-01"),
        employee("EMP4002", "Ops", "2026-07-03"),   # joins on the Friday
        employee("EMP4003", "Ops", "2026-07-20"),   # joins after the range
        employee("EMP4004", "Elsewhere", "2026-01-01"),
    ])
    mdb.attendance_logs.insert_many([
        record("EMP4001", "2026-06-30"),                                         # before the range
        record("EMP4001", "2026-07-01"),
        record("EMP4001", "2026-07-03", half=True, wh=4.0),
        record("EMP4002", "2026-07-03", late=15),
        record("EMP4001", "2026-07-04"),                                         # Saturday
        record("EMP4001", "2026-07-06", status="LEAVE", punch_in=None),
        record("EMP4002", "2026-07-06", status="WFH"),
        record("EMP4001", "2026-07-07", status="ABSENT", punch_in=None),
        record("EMP4002", "2026-07-07", status="ON_DUTY", late=20),
        record("EMP4001", "2026-07-09"), record("EMP4002", "2026-07-09", late=11),
        record("EMP4001", "2026-07-10"),
        record("EMP4004", "2026-07-02"), record("EMP4004", "2026-07-08"),        # other department
    ])


def test_trend_hand_computed(api, trend_data):
    r = api.get("/analytics/departments/Ops/trend", params={"from": "2026-07-01", "to": "2026-07-12"})
    body = check_response(r, "/analytics/departments/{department}/trend", "get")
    rows = {i["date"]: i for i in body["items"]}
    assert list(rows) == [f"2026-07-{d:02d}" for d in range(1, 13)]  # one row per day, weekends included
    assert rows["2026-07-01"] == {"date": "2026-07-01", "is_working_day": True, "headcount": 1, "present_count": 1,
                                  "late_count": 0, "attendance_rate": 1.0, "moving_avg_7d": 1.0}
    assert (rows["2026-07-02"]["attendance_rate"], rows["2026-07-02"]["moving_avg_7d"]) == (0.0, 0.5)  # no records
    assert (rows["2026-07-03"]["headcount"], rows["2026-07-03"]["present_count"]) == (2, 1.5)
    assert rows["2026-07-03"]["attendance_rate"] == 0.75 and rows["2026-07-03"]["late_count"] == 1
    assert rows["2026-07-04"]["is_working_day"] is False and rows["2026-07-04"]["attendance_rate"] is None
    assert rows["2026-07-04"]["present_count"] == 1  # weekend presence still counted
    assert rows["2026-07-04"]["moving_avg_7d"] == 0.5833  # (1 + 0 + 0.75) / 3, nulls ignored
    # 07-08: window 07-02..07-08 -> rates 0, 0.75, (null, null), 0.5, 0.5, 0 -> 1.75 / 5 = 0.35
    assert rows["2026-07-08"]["moving_avg_7d"] == 0.35
    assert body["items"] == reference_trend("Ops", date(2026, 7, 1), date(2026, 7, 12))


def test_trend_window_does_not_reach_before_from(api, trend_data):
    rows = api.get("/analytics/departments/Ops/trend", params={"from": "2026-07-04", "to": "2026-07-06"}).json()["items"]
    assert [(r["date"], r["attendance_rate"], r["moving_avg_7d"]) for r in rows] == [
        ("2026-07-04", None, None), ("2026-07-05", None, None), ("2026-07-06", 0.5, 0.5)]
    assert rows[0]["headcount"] == 2


def test_trend_half_up_four_decimals(api, mdb):
    mdb.employees.insert_many([employee(f"EMP5{n:03d}", "Big", "2026-01-01") for n in range(32)])
    mdb.attendance_logs.insert_one(record("EMP5000", "2026-07-01"))
    rows = api.get("/analytics/departments/Big/trend", params={"from": "2026-07-01", "to": "2026-07-02"}).json()["items"]
    assert rows[0]["attendance_rate"] == 0.0313  # 1/32 = 0.03125 -> half-up (half-even would give 0.0312)
    assert rows[1]["moving_avg_7d"] == 0.0157    # (0.0313 + 0) / 2 = 0.01565 -> 0.0157


def test_trend_department_before_anyone_joined(api, mdb):
    mdb.employees.insert_one(employee("EMP6001", "Late", "2026-07-10"))
    rows = api.get("/analytics/departments/Late/trend", params={"from": "2026-07-01", "to": "2026-07-03"}).json()["items"]
    assert [(r["date"], r["headcount"], r["attendance_rate"]) for r in rows] == [
        ("2026-07-01", 0, None), ("2026-07-02", 0, None), ("2026-07-03", 0, None)]


def test_trend_full_quarter_and_errors(api, trend_data):
    rows = api.get("/analytics/departments/Ops/trend", params={"from": "2026-07-01", "to": "2026-09-30"}).json()["items"]
    assert len(rows) == 92 and rows[-1]["headcount"] == 3  # EMP4003 joined 07-20
    r = api.get("/analytics/departments/Nope/trend", params={"from": "2026-07-01", "to": "2026-07-31"})
    assert r.status_code == 404 and check_response(r, "/analytics/departments/{department}/trend", "get")
    r = api.get("/analytics/departments/Ops/trend", params={"from": "2026-07-01", "to": "2026-10-01"})
    assert r.status_code == 422
    single = api.get("/analytics/departments/Ops/trend", params={"from": "2026-07-01", "to": "2026-07-01"}).json()
    assert len(single["items"]) == 1


# =========================================================================== extras
def test_history_endpoint(api):
    url = _setup_closed_day(api)
    empty = api.get(url + "/history")
    assert empty.status_code == 200 and empty.json() == {"emp_code": "EMP0001", "date": "2026-07-07", "history": []}
    api.patch(url, json={"status": "WFH", "reason": "worked from home", "regularized_by": "a"})
    api.patch(url, json={"punch_out": ms(2026, 7, 7, 19, 30), "reason": "forgot to punch", "regularized_by": "b"})
    body = api.get(url + "/history").json()
    assert [h["by"] for h in body["history"]] == ["a", "b"]
    assert body["history"][1]["changes"]["punch_out"]["to"] == ms(2026, 7, 7, 19, 30)  # epoch ms, like the record
    assert api.get("/attendance/EMP9999/2026-07-07/history").status_code == 404
    assert api.get("/attendance/EMP0001/2026-07-08/history").status_code == 404


@pytest.fixture
def streak_data(mdb):
    mdb.employees.insert_many([employee("EMP7001"), employee("EMP7002"), employee("EMP7003", joined="2026-07-08")])
    mdb.attendance_logs.insert_many([
        # EMP7001: Wed 1, Thu 2, Fri 3, (weekend), Mon 6 on time = 4-day run across the weekend;
        # Tue 7 late breaks it; Wed 8, Thu 9, Fri 10 on time = 3; Mon 13 missing; Tue 14 LEAVE; Wed 15 on time = 1
        record("EMP7001", "2026-07-01"), record("EMP7001", "2026-07-02"), record("EMP7001", "2026-07-03"),
        record("EMP7001", "2026-07-04"),  # Saturday record: ignored, neither extends nor breaks
        record("EMP7001", "2026-07-06"), record("EMP7001", "2026-07-07", late=15),
        record("EMP7001", "2026-07-08", status="WFH"), record("EMP7001", "2026-07-09", status="ON_DUTY"),
        record("EMP7001", "2026-07-10"), record("EMP7001", "2026-07-14", status="LEAVE", punch_in=None),
        record("EMP7001", "2026-07-15"),
        # EMP7002: two 2-day runs -> the earlier one wins the tie
        record("EMP7002", "2026-07-01"), record("EMP7002", "2026-07-02"), record("EMP7002", "2026-07-06"),
        record("EMP7002", "2026-07-07"),
        # EMP7003: joined Wed 8; logs before joining are outside its working days
        record("EMP7003", "2026-07-07"), record("EMP7003", "2026-07-08"), record("EMP7003", "2026-07-09"),
    ])


def _streaks(api, code):
    r = api.get(f"/analytics/employees/{code}/streaks", params={"month": "2026-07"})
    assert r.status_code == 200, r.text
    return r.json()


def test_streaks_across_weekends_and_breaks(api, streak_data):
    body = _streaks(api, "EMP7001")
    assert body["longest_on_time_streak"] == {"days": 4, "start": "2026-07-01", "end": "2026-07-06"}
    assert (body["on_time_days"], body["on_time_runs"]) == (8, 3)


def test_streaks_tie_and_joiner(api, streak_data):
    assert _streaks(api, "EMP7002")["longest_on_time_streak"] == {"days": 2, "start": "2026-07-01", "end": "2026-07-02"}
    joiner = _streaks(api, "EMP7003")
    assert joiner["longest_on_time_streak"] == {"days": 2, "start": "2026-07-08", "end": "2026-07-09"}
    assert joiner["on_time_days"] == 2


def test_streaks_no_logs_and_unknown(api, mdb):
    mdb.employees.insert_one(employee("EMP7009"))
    body = _streaks(api, "EMP7009")
    assert body["longest_on_time_streak"] == {"days": 0, "start": None, "end": None} and body["on_time_days"] == 0
    assert api.get("/analytics/employees/EMP9999/streaks", params={"month": "2026-07"}).status_code == 404


def test_dashboard_and_request_id_through_uvicorn(api):
    r = api.get("/dashboard", headers={"X-Request-ID": "trace-42"})
    assert r.status_code == 200 and "Attendance dashboard" in r.text
    assert r.headers["X-Request-ID"] == "trace-42" and float(r.headers["X-Response-Time-ms"]) >= 0
    assert "X-Request-ID" in api.get("/health").headers


# =========================================================================== explain
EXPLAIN_CASES = [
    ("attendance_list", {}),
    ("attendance_list", {"emp_code": "EMP1001"}),
    ("attendance_list", {"date_from": "2026-07-01", "date_to": "2026-07-31", "page": 2}),
    ("attendance_list", {"status": "LEAVE"}),
    ("employee_monthly", {"emp_code": "EMP1001", "month": "2026-07"}),
    ("department_summary", {"month": "2026-07"}),
    ("department_summary", {"month": "2026-07", "department": "Sales"}),
    ("late_leaderboard", {"month": "2026-07", "limit": 3}),
    ("late_leaderboard", {"month": "2026-07", "department": "Engineering"}),
    ("department_trend", {"department": "Engineering", "from": "2026-07-01", "to": "2026-07-31"}),
]


@pytest.mark.parametrize("endpoint, params", EXPLAIN_CASES)
def test_explain_uses_indexes(api, analytics_data, endpoint, params):
    r = api.get(f"/admin/explain/{endpoint}", params=params)
    body = check_response(r, "/admin/explain/{endpoint}", "get")
    assert body["endpoint"] == endpoint and body["collection"] in ("employees", "attendance_logs")
    text = plan_text(body)
    assert "IXSCAN" in text and "COLLSCAN" not in text, text[:3000]


# =========================================================================== independent reference implementations
def _weekday(day: str) -> bool:
    return date.fromisoformat(day).weekday() < 5


def _credit(log) -> float:
    if log["status"] not in ("PRESENT", "WFH", "ON_DUTY"):
        return 0
    return 0.5 if log.get("half_day") else 1


def _month_logs(code, month):
    return [r for r in LOGS if r["emp_code"] == code and r["date"].startswith(month)]


def reference_monthly(emp, month):
    y, m = map(int, month.split("-"))
    days = [date(y, m, d) for d in range(1, 32) if d <= (date(y + (m == 12), m % 12 + 1, 1) - date(y, m, 1)).days]
    working = sum(1 for d in days if d.weekday() < 5 and d.isoformat() >= emp["joined_on"])
    mine = _month_logs(emp["emp_code"], month)
    present = sum(_credit(r) for r in mine if _weekday(r["date"]))
    return {
        "emp_code": emp["emp_code"], "month": month, "working_days": working, "present_days": present,
        "leave_days": sum(r["status"] == "LEAVE" for r in mine),
        "late_count": sum((r.get("late_minutes") or 0) > 0 for r in mine),
        "total_late_minutes": sum(r.get("late_minutes") or 0 for r in mine),
        "total_overtime_minutes": sum(r.get("overtime_minutes") or 0 for r in mine),
        "attendance_pct": None if working == 0 else half_up(Decimal(str(present)) * 100 / working, 2),
    }


def reference_summary(month):
    y, m = map(int, month.split("-"))
    end = (date(y + (m == 12), m % 12 + 1, 1) - timedelta(days=1)).isoformat()
    out = {}
    for emp in EMPLOYEES:
        if emp["joined_on"] > end:
            continue
        d = out.setdefault(emp["department"], {"department": emp["department"], "headcount": 0, "present_days": 0,
                                               "hours": [], "late_count": 0, "total_late_minutes": 0,
                                               "leave_count": 0, "on_duty_count": 0})
        d["headcount"] += 1
        for r in _month_logs(emp["emp_code"], month):
            d["present_days"] += _credit(r) if _weekday(r["date"]) else 0
            if r["status"] in ("PRESENT", "WFH", "ON_DUTY") and r.get("work_hours") is not None:
                d["hours"].append(Decimal(str(r["work_hours"])))
            d["late_count"] += (r.get("late_minutes") or 0) > 0
            d["total_late_minutes"] += r.get("late_minutes") or 0
            d["leave_count"] += r["status"] == "LEAVE"
            d["on_duty_count"] += r["status"] == "ON_DUTY"
    items = []
    for name in sorted(out):
        d = out[name]
        hours = d.pop("hours")
        d["avg_work_hours"] = half_up(sum(hours) / len(hours), 2) if hours else None
        items.append(d)
    return items


def reference_trend(dept, start, end):
    # the trend fixture, re-read from MongoDB-independent constants is overkill; rebuild from the inserted docs
    client = MongoClient(TEST_MONGO_URI)
    database = client[TEST_DB]
    emps = list(database.employees.find({"department": dept}))
    codes = {e["emp_code"] for e in emps}
    logs = [r for r in database.attendance_logs.find() if r["emp_code"] in codes]
    client.close()
    rows, rates = [], []
    day = start
    while day <= end:
        s = day.isoformat()
        today = [r for r in logs if r["date"] == s]
        headcount = sum(e["joined_on"] <= s for e in emps)
        present = sum(_credit(r) for r in today)
        working = day.weekday() < 5
        rate = half_up(Decimal(str(present)) / headcount, 4) if working and headcount else None
        rates.append(rate)
        window = [x for x in rates[-7:] if x is not None]
        moving = half_up(sum(Decimal(str(x)) for x in window) / len(window), 4) if window else None
        rows.append({"date": s, "is_working_day": working, "headcount": headcount, "present_count": present,
                     "late_count": sum((r.get("late_minutes") or 0) > 0 for r in today),
                     "attendance_rate": rate, "moving_avg_7d": moving})
        day += timedelta(days=1)
    return rows
