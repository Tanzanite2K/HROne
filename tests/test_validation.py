"""Request validation and contract-shape tests that never reach MongoDB.

Every request here is rejected before the handler touches the database, so these run anywhere. The TestClient is
used WITHOUT a `with` block, so the startup hook (index creation) does not run either.
"""
import re

import pytest
from fastapi.testclient import TestClient
from pymongo import MongoClient

import app.main as main
from tests.contract import SPEC, check_response

client = TestClient(main.app)
GOOD_EMPLOYEE = {"emp_code": "EMP0001", "name": "Asha", "email": "asha@example.com", "department": "Eng",
                 "joined_on": "2026-01-05"}
VALID_MS = 1783310280000


def assert_422(response, path, method):
    assert response.status_code == 422, response.text
    body = check_response(response, path, method)
    assert isinstance(body["detail"], list) and body["detail"]


# ---------------------------------------------------------------- employees
@pytest.mark.parametrize("patch", [
    {"emp_code": "E0001"}, {"emp_code": "EMP123"}, {"emp_code": "EMP1234567"}, {"emp_code": "emp0001"},
    {"name": ""}, {"name": "x" * 101}, {"email": "not-an-email"}, {"email": "a@b"}, {"department": ""},
    {"department": "d" * 51}, {"shift_start": "24:00"}, {"shift_end": "9:30"}, {"shift_start": "18:30"},
    {"joined_on": "2026-02-30"}, {"joined_on": "05-01-2026"}, {"joined_on": "20260105"}, {"name": 123},
])
def test_create_employee_rejects_invalid_input(patch):
    body = {**GOOD_EMPLOYEE, **patch}
    if patch == {"shift_start": "18:30"}:  # equal to default shift_end
        body["shift_end"] = "18:30"
    assert_422(client.post("/employees", json=body), "/employees", "post")


@pytest.mark.parametrize("field", ["emp_code", "name", "email", "department", "joined_on"])
def test_create_employee_missing_required(field):
    body = {k: v for k, v in GOOD_EMPLOYEE.items() if k != field}
    assert_422(client.post("/employees", json=body), "/employees", "post")


@pytest.mark.parametrize("params", [{"page": 0}, {"page": -1}, {"page_size": 0}, {"page_size": 101},
                                    {"page": "x"}, {"page_size": 2.5}])
def test_list_employees_pagination_bounds(params):
    assert_422(client.get("/employees", params=params), "/employees", "get")


# ---------------------------------------------------------------- punch-in / punch-out timestamps
BAD_TIMESTAMPS = [1783310280, 99_999_999_999, 4_102_444_800_001, 1783310280000.0, 1.78331028e12, "1783310280000",
                  True, -5]


@pytest.mark.parametrize("value", BAD_TIMESTAMPS)
def test_punch_in_rejects_bad_timestamps(value):
    r = client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": value})
    assert_422(r, "/attendance/punch-in", "post")


@pytest.mark.parametrize("value", BAD_TIMESTAMPS)
def test_punch_out_rejects_bad_timestamps(value):
    r = client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": value})
    assert_422(r, "/attendance/punch-out", "post")


@pytest.mark.parametrize("status", ["ABSENT", "LEAVE", "present", "HOLIDAY", 1])
def test_punch_in_accepts_only_presence_statuses(status):
    r = client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "status": status})
    assert_422(r, "/attendance/punch-in", "post")


def test_punch_in_requires_emp_code():
    assert_422(client.post("/attendance/punch-in", json={}), "/attendance/punch-in", "post")
    assert_422(client.post("/attendance/punch-in", content=b"not json",
                           headers={"content-type": "application/json"}), "/attendance/punch-in", "post")


# ---------------------------------------------------------------- GET /attendance
@pytest.mark.parametrize("params", [
    {"date_from": "2026-07-10", "date_to": "2026-07-01"},
    {"date_from": "2026-7-1"}, {"date_to": "2026-02-30"}, {"status": "present"}, {"status": "HOLIDAY"},
    {"page": 0}, {"page_size": 101},
])
def test_list_attendance_rejects(params):
    assert_422(client.get("/attendance", params=params), "/attendance", "get")


# ---------------------------------------------------------------- PATCH
PATCH_PATH = "/attendance/{emp_code}/{date}"
GOOD_PATCH = {"reason": "biometric glitch", "regularized_by": "hr.admin"}


@pytest.mark.parametrize("body", [
    {"reason": "bio"},                                         # reason too short (< 5)
    {"regularized_by": ""},
    {"reason": "x" * 201},
    {"status": None}, {"punch_in": None}, {"punch_out": None},  # omitted is fine, explicit null is not
    {"status": "HOLIDAY"},
    {"punch_in": 1783310280}, {"punch_out": 1783310280000.5}, {"punch_in": "1783310280000"},
])
def test_regularize_rejects_invalid_bodies(body):
    r = client.patch("/attendance/EMP0001/2026-07-06", json={**GOOD_PATCH, **body})
    assert_422(r, PATCH_PATH, "patch")


@pytest.mark.parametrize("missing", ["reason", "regularized_by"])
def test_regularize_requires_reason_and_actor(missing):
    body = {k: v for k, v in GOOD_PATCH.items() if k != missing}
    assert_422(client.patch("/attendance/EMP0001/2026-07-06", json={**body, "status": "LEAVE"}), PATCH_PATH, "patch")


@pytest.mark.parametrize("bad_date", ["2026-7-6", "2026-02-30", "06-07-2026", "today"])
def test_regularize_rejects_bad_path_date(bad_date):
    r = client.patch(f"/attendance/EMP0001/{bad_date}", json={**GOOD_PATCH, "status": "LEAVE"})
    assert_422(r, PATCH_PATH, "patch")


# ---------------------------------------------------------------- analytics
@pytest.mark.parametrize("month", ["2026-13", "2026-00", "2026-7", "202607", "July", "0000-01"])
def test_month_validation(month):
    assert_422(client.get("/analytics/employees/EMP0001/monthly", params={"month": month}),
               "/analytics/employees/{emp_code}/monthly", "get")
    assert_422(client.get("/analytics/departments/summary", params={"month": month}),
               "/analytics/departments/summary", "get")
    assert_422(client.get("/analytics/leaderboard/late", params={"month": month}),
               "/analytics/leaderboard/late", "get")


def test_month_is_required():
    assert_422(client.get("/analytics/departments/summary"), "/analytics/departments/summary", "get")
    assert_422(client.get("/analytics/leaderboard/late"), "/analytics/leaderboard/late", "get")


@pytest.mark.parametrize("limit", [0, 51, -1, "ten"])
def test_leaderboard_limit_bounds(limit):
    assert_422(client.get("/analytics/leaderboard/late", params={"month": "2026-07", "limit": limit}),
               "/analytics/leaderboard/late", "get")


TREND = "/analytics/departments/{department}/trend"


@pytest.mark.parametrize("params", [
    {"from": "2026-07-10", "to": "2026-07-09"},       # to < from
    {"from": "2026-07-01", "to": "2026-10-01"},       # 93 days inclusive
    {"from": "2026-01-01", "to": "2026-12-31"},
    {"from": "2026-07-01"}, {"to": "2026-07-01"},     # both required
    {"from": "2026-02-30", "to": "2026-03-01"},
    {"from": "2026/07/01", "to": "2026-07-02"},
])
def test_trend_range_validation(params):
    assert_422(client.get("/analytics/departments/Engineering/trend", params=params), TREND, "get")


# ---------------------------------------------------------------- admin/explain
def test_explain_rejects_unknown_endpoint():
    for name in ["employees", "drop", "attendance_logs", "../health"]:
        r = client.get(f"/admin/explain/{name}")
        assert r.status_code in (404, 422)  # '../health' is normalised by the router
    assert_422(client.get("/admin/explain/whatever"), "/admin/explain/{endpoint}", "get")


@pytest.mark.parametrize("endpoint, params", [
    ("employee_monthly", {"month": "2026-07"}),
    ("employee_monthly", {"emp_code": "EMP0001"}),
    ("department_summary", {}),
    ("late_leaderboard", {}),
    ("department_trend", {"from": "2026-07-01", "to": "2026-07-31"}),
    ("department_trend", {"department": "Eng", "to": "2026-07-31"}),
    ("department_trend", {"department": "Eng", "from": "2026-07-31", "to": "2026-07-01"}),
    ("attendance_list", {"date_from": "2026-07-31", "date_to": "2026-07-01"}),
    ("late_leaderboard", {"month": "2026-07", "limit": 99}),
])
def test_explain_missing_or_invalid_params(endpoint, params):
    assert_422(client.get(f"/admin/explain/{endpoint}", params=params), "/admin/explain/{endpoint}", "get")


# ---------------------------------------------------------------- database down -> 503, never 500
def test_health_is_503_when_mongodb_is_down(monkeypatch):
    dead = MongoClient("mongodb://127.0.0.1:1", serverSelectionTimeoutMS=200)
    monkeypatch.setattr(main, "client", dead)
    r = client.get("/health")
    assert r.status_code == 503
    check_response(r, "/health", "get")


def test_db_errors_become_503(monkeypatch):
    dead = MongoClient("mongodb://127.0.0.1:1", serverSelectionTimeoutMS=200)
    monkeypatch.setattr(main, "employees", dead["x"]["employees"])
    assert TestClient(main.app, raise_server_exceptions=False).get("/employees").status_code == 503


# ---------------------------------------------------------------- the generated API matches openapi.yaml
def _app_operations():
    """Contract operations only: extras are tagged "extras" and checked separately below."""
    spec = main.app.openapi()
    return {(path, method): op for path, item in spec["paths"].items() for method, op in item.items()
            if "extras" not in op.get("tags", [])}


def test_every_contract_operation_exists_with_the_same_parameters():
    ours = _app_operations()
    for path, item in SPEC["paths"].items():
        for method, op in item.items():
            assert (path, method) in ours, f"missing {method.upper()} {path}"
            expected = {}
            for p in op.get("parameters", []):
                if "$ref" in p:
                    p = SPEC["components"]["parameters"][p["$ref"].split("/")[-1]]
                expected[(p["name"], p["in"])] = p.get("required", False)
            actual = {(p["name"], p["in"]): p.get("required", False) for p in ours[(path, method)].get("parameters", [])}
            assert actual == expected, f"{method.upper()} {path}: {actual} != {expected}"
    assert len(ours) == sum(len(item) for item in SPEC["paths"].values()), "app exposes undocumented operations"


def test_extras_are_only_new_paths():
    spec = main.app.openapi()
    extras = {(p, m) for p, item in spec["paths"].items() for m, op in item.items() if "extras" in op.get("tags", [])}
    assert extras == {("/attendance/{emp_code}/{date}/history", "get"), ("/analytics/employees/{emp_code}/streaks", "get")}
    assert not any(path in SPEC["paths"] for path, _ in extras)  # never shadows or changes a contract path
    assert "/dashboard" not in spec["paths"]


# ---------------------------------------------------------------- extras (offline parts)
def test_request_id_and_timing_headers():
    r = client.get("/attendance", params={"page": 0})  # rejected before the DB, still passes through the middleware
    assert r.status_code == 422
    assert re.fullmatch(r"[0-9a-f]{32}", r.headers["X-Request-ID"])
    assert float(r.headers["X-Response-Time-ms"]) >= 0


def test_request_id_is_echoed_only_when_safe():
    assert client.get("/attendance", params={"page": 0}, headers={"X-Request-ID": "abc-123"}).headers["X-Request-ID"] == "abc-123"
    forged = client.get("/attendance", params={"page": 0}, headers={"X-Request-ID": "x" * 65})
    assert forged.headers["X-Request-ID"] != "x" * 65


def test_dashboard_is_served_and_self_contained():
    r = client.get("/dashboard")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    assert "Attendance dashboard" in r.text
    assert "innerHTML" not in r.text and "http://" not in r.text.replace("http://www.w3.org/2000/svg", "")
    assert "https://" not in r.text  # no external scripts, fonts or styles


@pytest.mark.parametrize("bad", ["2026-7-6", "2026-02-30", "today"])
def test_history_rejects_bad_date(bad):
    assert client.get(f"/attendance/EMP0001/{bad}/history").status_code == 422


@pytest.mark.parametrize("month", ["2026-13", "July", "0000-01"])
def test_streaks_rejects_bad_month(month):
    assert client.get("/analytics/employees/EMP0001/streaks", params={"month": month}).status_code == 422
