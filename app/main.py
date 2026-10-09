"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB. A local .env is loaded for convenience, but real environment variables always win.

Layout of this file (everything lives here, as the assignment requires):
    1. Configuration, MongoDB client, indexes
    2. Time helpers and the business rules R1-R5
    3. Serialisation (MongoDB document -> API JSON)
    4. Request / response models
    5. Endpoints: system, employees, attendance
    6. Aggregation pipeline builders (shared by the analytics endpoints and /admin/explain)
    7. Endpoints: analytics, admin
    8. Extras beyond the contract (tagged "extras"; nothing in sections 1-7 depends on them)
"""
from __future__ import annotations

import json
import logging
import os
import re
import uuid
from contextlib import asynccontextmanager
from datetime import date as Date, datetime, time, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from enum import Enum
from time import perf_counter
from typing import Annotated, Any, Literal, Optional

from bson import Decimal128, json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field, field_validator, model_validator
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import ConnectionFailure, DuplicateKeyError, PyMongoError

log = logging.getLogger("uvicorn.error")

# =========================================================================== #
# 1. Configuration, MongoDB client, indexes
# =========================================================================== #
load_dotenv(override=False)  # override=False: a real environment variable beats the .env file

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

# tz_aware=True: PyMongo returns timezone-aware UTC datetimes instead of naive ones, so an instant can never be
# silently re-interpreted in the server's local time zone.
client = MongoClient(MONGO_URI, tz_aware=True, tzinfo=timezone.utc, serverSelectionTimeoutMS=5000)
db = client[MONGO_DB]
employees = db["employees"]
logs = db["attendance_logs"]

# (collection, keys, options). Every index is named so create_index is idempotent across restarts.
INDEXES = [
    # Guarantees one employee per emp_code; concurrent duplicate creates -> exactly one 201, the rest 409.
    (employees, [("emp_code", 1)], {"name": "uniq_emp_code", "unique": True}),
    # GET /employees?department=X sorted by emp_code.
    (employees, [("department", 1), ("emp_code", 1)], {"name": "department_emp_code"}),
    # Department summary (joined_on <= month end) and trend (department + joined_on <= to).
    (employees, [("department", 1), ("joined_on", 1)], {"name": "department_joined_on"}),
    # Department summary without a department filter: {joined_on: {$lte: month_end}} needs joined_on as the
    # leading key, otherwise the plan is a COLLSCAN of employees.
    (employees, [("joined_on", 1)], {"name": "joined_on"}),
    # The natural key: at most one record per (emp_code, date). Makes punch-in race-safe; also serves every
    # "one employee, date range" lookup (monthly summary, $lookup from employees).
    (logs, [("emp_code", 1), ("date", 1)], {"name": "uniq_emp_code_date", "unique": True}),
    # GET /attendance default sort (date desc, emp_code asc) and every month/date-range scan in analytics.
    (logs, [("date", -1), ("emp_code", 1)], {"name": "date_desc_emp_code"}),
    # Punch-out: "the employee's most recent record whose punch_in <= punched_at".
    (logs, [("emp_code", 1), ("punch_in", -1)], {"name": "emp_code_punch_in_desc"}),
    # GET /attendance?status=X (filter + sort + count) without walking the whole date index.
    (logs, [("status", 1), ("date", -1), ("emp_code", 1)], {"name": "status_date_emp_code"}),
]
_indexes_ready = False


def ensure_indexes() -> None:
    """Create all indexes. Safe to call repeatedly: creating an existing, identical index is a no-op."""
    global _indexes_ready
    for collection, keys, options in INDEXES:
        try:
            collection.create_index(keys, **options)
        except ConnectionFailure:
            raise
        except PyMongoError as exc:  # e.g. existing duplicate data blocks a unique index: report, keep serving
            log.error("could not create index %s on %s: %s", options["name"], collection.name, exc)
    _indexes_ready = True


@asynccontextmanager
async def lifespan(_: FastAPI):
    try:
        ensure_indexes()
    except ConnectionFailure as exc:  # do not crash the process; /health retries once MongoDB is reachable
        log.error("MongoDB not reachable at startup, indexes will be created on first /health: %s", exc)
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


@app.exception_handler(ConnectionFailure)
async def _db_unavailable(_: Request, exc: ConnectionFailure) -> JSONResponse:
    # A lost database connection is "service unavailable", never an unexplained 500.
    return JSONResponse(status_code=503, content={"detail": "Database unavailable"})


# Extra (not in the contract): every response carries a request id and its server-side duration, and one log line
# per request ties them together. Bodies and status codes are untouched, so the contract is unaffected.
access_log = logging.getLogger("hrone.access")
REQUEST_ID_PATTERN = re.compile(r"[A-Za-z0-9._-]{1,64}")


@app.middleware("http")
async def request_id_and_timing(request: Request, call_next):
    incoming = request.headers.get("x-request-id", "")
    # Reuse the caller's id only if it is short and safe (no newlines etc. that could forge log lines).
    request_id = incoming if REQUEST_ID_PATTERN.fullmatch(incoming) else uuid.uuid4().hex
    started = perf_counter()
    response = await call_next(request)
    elapsed_ms = (perf_counter() - started) * 1000
    response.headers["X-Request-ID"] = request_id
    response.headers["X-Response-Time-ms"] = f"{elapsed_ms:.1f}"
    access_log.info("%s %s %s %.1fms id=%s", request.method, request.url.path, response.status_code, elapsed_ms,
                    request_id)
    return response


# =========================================================================== #
# 2. Time helpers and the business rules R1-R5
# =========================================================================== #
IST = timezone(timedelta(hours=5, minutes=30))  # India has no daylight saving, so a fixed offset is exact
UTC = timezone.utc
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

MIN_EPOCH_MS = 100_000_000_000  # anything smaller looks like seconds -> 422
MAX_EPOCH_MS = 4_102_444_800_000  # 2100-01-01T00:00:00Z

PRESENCE = ("PRESENT", "WFH", "ON_DUTY")  # R6
NON_PRESENCE = ("ABSENT", "LEAVE")
GRACE = timedelta(minutes=10)  # R2
MIN_OVERTIME_MINUTES = 30  # R3
HALF_DAY_BELOW_HOURS = 4.5  # R5
MAX_SHIFT_LENGTH = timedelta(hours=24)
TREND_MAX_DAYS = 92

DATE_PATTERN = r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$"
MONTH_PATTERN = r"^[0-9]{4}-(0[1-9]|1[0-2])$"
HHMM_PATTERN = r"^([01][0-9]|2[0-3]):[0-5][0-9]$"


def ms_to_instant(ms: int) -> datetime:
    """Epoch milliseconds -> aware UTC datetime, truncated to whole seconds (R1: 09:40:00.900 counts as 09:40:00)."""
    return datetime.fromtimestamp(ms // 1000, tz=UTC)


def now_instant() -> datetime:
    """The current instant, truncated to whole seconds like a client-supplied punch."""
    return datetime.now(UTC).replace(microsecond=0)


def now_millis_precision() -> datetime:
    """The current instant at millisecond precision (what BSON can store), for created_at and history[].at."""
    n = datetime.now(UTC)
    return n.replace(microsecond=n.microsecond // 1000 * 1000)


def instant_to_ms(value: Optional[datetime]) -> Optional[int]:
    """Aware (or naive-but-UTC, as stored by BSON) datetime -> integer epoch milliseconds. Exact integer maths."""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return (value - EPOCH) // timedelta(milliseconds=1)


def _hhmm(value: str) -> time:
    hours, minutes = value.split(":")
    return time(int(hours), int(minutes))


def is_overnight(shift_start: str, shift_end: str) -> bool:
    return _hhmm(shift_end) <= _hhmm(shift_start)


def attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> Date:
    """R1. The IST calendar date of the punch-in, except that for an overnight shift a punch-in whose IST time is
    earlier than shift_end belongs to the shift that started the previous day."""
    local = punch_in.astimezone(IST)
    day = local.date()
    if is_overnight(shift_start, shift_end) and local.time() < _hhmm(shift_end):
        day -= timedelta(days=1)
    return day


def shift_start_at(day: Date, shift_start: str) -> datetime:
    return datetime.combine(day, _hhmm(shift_start), tzinfo=IST)


def shift_end_at(day: Date, shift_start: str, shift_end: str) -> datetime:
    """The shift end of an overnight shift falls on the next calendar day (R1)."""
    end_day = day + timedelta(days=1) if is_overnight(shift_start, shift_end) else day
    return datetime.combine(end_day, _hhmm(shift_end), tzinfo=IST)


def compute_late_minutes(punch_in: datetime, day: Date, shift_start: str) -> int:
    """R2. Late only if strictly more than 10:00 after shift_start; then whole minutes since shift_start."""
    elapsed = punch_in - shift_start_at(day, shift_start)
    if elapsed <= GRACE:
        return 0
    return elapsed // timedelta(minutes=1)  # timedelta // timedelta is an int and floors


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4. Seconds / 3600 rounded to 2 decimals HALF-UP using exact decimal arithmetic (9045 s -> 2.52)."""
    seconds = (punch_out - punch_in) // timedelta(seconds=1)
    hours = (Decimal(seconds) / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return float(hours)


def compute_overtime(punch_out: datetime, day: Date, shift_start: str, shift_end: str) -> int:
    """R3. Whole minutes from shift end to punch_out, counted only when >= 30."""
    minutes = (punch_out - shift_end_at(day, shift_start, shift_end)) // timedelta(minutes=1)
    return minutes if minutes >= MIN_OVERTIME_MINUTES else 0


def punch_out_fields(employee: dict, day: Date, punch_in: datetime, punch_out: Optional[datetime]) -> dict:
    """The fields computed at punch-out (R3-R5). Before punch-out they have their documented defaults."""
    if punch_out is None:
        return {"work_hours": None, "overtime_minutes": 0, "half_day": False}
    work_hours = compute_work_hours(punch_in, punch_out)
    return {
        "work_hours": work_hours,
        "overtime_minutes": compute_overtime(punch_out, day, employee["shift_start"], employee["shift_end"]),
        "half_day": work_hours < HALF_DAY_BELOW_HOURS,  # R5 compares the ROUNDED value
    }


def derived_fields(employee: dict, day: Date, status: str, punch_in, punch_out) -> dict:
    """All four derived fields for a final record state (used after a correction)."""
    if status in NON_PRESENCE:
        return {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    return {
        "late_minutes": compute_late_minutes(punch_in, day, employee["shift_start"]),
        **punch_out_fields(employee, day, punch_in, punch_out),
    }


def unprocessable(message: str, *loc: str) -> HTTPException:
    """A 422 whose body has the same shape as FastAPI's own validation errors ({"detail": [ {...} ]})."""
    return HTTPException(status_code=422, detail=[{"type": "value_error", "loc": list(loc), "msg": message}])


def parse_calendar_date(value: str, *loc: str) -> Date:
    """The regex already guarantees YYYY-MM-DD; this rejects impossible dates like 2026-02-30."""
    try:
        return Date.fromisoformat(value)
    except ValueError:
        raise unprocessable(f"{value!r} is not a valid calendar date", *loc)


def month_bounds(month: str, *loc: str) -> tuple[Date, Date]:
    """'2026-07' -> (2026-07-01, 2026-08-01): first day of the month and first day of the next month."""
    try:
        year, mon = int(month[:4]), int(month[5:7])
        first = Date(year, mon, 1)
        next_first = Date(year + 1, 1, 1) if mon == 12 else Date(year, mon + 1, 1)
    except ValueError:
        raise unprocessable(f"{month!r} is not a valid month", *loc)
    return first, next_first


# =========================================================================== #
# 3. Serialisation: MongoDB document -> API JSON
# =========================================================================== #
TRACKED_FIELDS = ("status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day")
INSTANT_FIELDS = ("punch_in", "punch_out")


def _api_value(value: Any) -> Any:
    return instant_to_ms(value) if isinstance(value, datetime) else value


def serialize_employee(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": instant_to_ms(doc.get("created_at")),
    }


def serialize_history(entry: dict) -> dict:
    return {
        "at": instant_to_ms(entry.get("at")),
        "by": entry.get("by"),
        "reason": entry.get("reason"),
        # Dates hide inside the nested changes object too; convert them to epoch ms as well.
        "changes": {
            field: {"from": _api_value(change.get("from")), "to": _api_value(change.get("to"))}
            for field, change in (entry.get("changes") or {}).items()
        },
    }


def serialize_record(doc: dict) -> dict:
    """Stored record -> AttendanceRecord. Missing fields on legacy records take their documented defaults."""
    work_hours = doc.get("work_hours")
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": instant_to_ms(doc.get("punch_in")),
        "punch_out": instant_to_ms(doc.get("punch_out")),
        "work_hours": None if work_hours is None else float(work_hours),
        "late_minutes": int(doc.get("late_minutes") or 0),
        "overtime_minutes": int(doc.get("overtime_minutes") or 0),
        "half_day": bool(doc.get("half_day", False)),
        "history": [serialize_history(h) for h in (doc.get("history") or [])],
    }


def to_float(value: Any) -> Optional[float]:
    """Aggregation results may be Decimal128 (exact rounding happens in the pipeline)."""
    if value is None:
        return None
    if isinstance(value, Decimal128):
        return float(value.to_decimal())
    return float(value)


# =========================================================================== #
# 4. Request / response models
# =========================================================================== #
# strict=True: rejects floats (even 1.7e12), booleans and numeric strings; bounds reject seconds-looking values.
EpochMillis = Annotated[int, Field(strict=True, ge=MIN_EPOCH_MS, le=MAX_EPOCH_MS)]
Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]


class EmployeeCreate(BaseModel):
    emp_code: Annotated[str, Field(pattern=r"^EMP[0-9]{4,6}$", examples=["EMP0001"])]
    name: Annotated[str, Field(min_length=1, max_length=100)]
    email: Annotated[str, Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")]
    department: Annotated[str, Field(min_length=1, max_length=50)]
    shift_start: Annotated[str, Field(pattern=HHMM_PATTERN)] = "09:30"
    shift_end: Annotated[str, Field(pattern=HHMM_PATTERN)] = "18:30"
    joined_on: Annotated[str, Field(pattern=DATE_PATTERN, examples=["2026-01-05"])]

    @field_validator("joined_on")
    @classmethod
    def _real_date(cls, value: str) -> str:
        Date.fromisoformat(value)  # ValueError -> 422
        return value

    @model_validator(mode="after")
    def _shift_not_empty(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class Employee(BaseModel):
    emp_code: str
    name: str
    email: str
    department: str
    shift_start: str
    shift_end: str
    joined_on: str
    created_at: int


class EmployeePage(BaseModel):
    items: list[Employee]
    total: int
    page: int
    page_size: int


class PunchInRequest(BaseModel):
    emp_code: Annotated[str, Field(min_length=1)]
    punched_at: Optional[EpochMillis] = None
    status: PresenceStatus = "PRESENT"


class PunchOutRequest(BaseModel):
    emp_code: Annotated[str, Field(min_length=1)]
    punched_at: Optional[EpochMillis] = None


class RegularizeRequest(BaseModel):
    """Only these fields can be changed. Derived fields sent by a client are ignored (pydantic drops extras)."""

    status: Optional[Status] = None
    punch_in: Optional[EpochMillis] = None
    punch_out: Optional[EpochMillis] = None
    reason: Annotated[str, Field(min_length=5, max_length=200)]
    regularized_by: Annotated[str, Field(min_length=1, max_length=50)]

    @model_validator(mode="after")
    def _no_explicit_nulls(self):
        # Omitted means "unchanged". An explicit null is not a valid Status / EpochMillis in the contract.
        for field in ("status", "punch_in", "punch_out"):
            if field in self.model_fields_set and getattr(self, field) is None:
                raise ValueError(f"{field} may be omitted but not null")
        return self


class HistoryEntry(BaseModel):
    at: int
    by: str
    reason: str
    changes: dict[str, dict[str, Any]]


class AttendanceRecord(BaseModel):
    emp_code: str
    date: str
    status: Status
    punch_in: Optional[int]
    punch_out: Optional[int]
    work_hours: Optional[float]
    late_minutes: int
    overtime_minutes: int
    half_day: bool
    history: list[HistoryEntry]


class AttendancePage(BaseModel):
    items: list[AttendanceRecord]
    total: int
    page: int
    page_size: int


class EmployeeMonthly(BaseModel):
    emp_code: str
    month: str
    working_days: int
    present_days: float
    leave_days: int
    late_count: int
    total_late_minutes: int
    total_overtime_minutes: int
    attendance_pct: Optional[float]


class DepartmentSummaryItem(BaseModel):
    department: str
    headcount: int
    present_days: float
    avg_work_hours: Optional[float]
    late_count: int
    total_late_minutes: int
    leave_count: int
    on_duty_count: int


class DepartmentSummary(BaseModel):
    month: str
    items: list[DepartmentSummaryItem]


class LeaderboardItem(BaseModel):
    rank: int
    emp_code: str
    name: str
    department: str
    total_late_minutes: int
    late_count: int


class Leaderboard(BaseModel):
    month: str
    items: list[LeaderboardItem]


class TrendItem(BaseModel):
    date: str
    is_working_day: bool
    headcount: int
    present_count: float
    late_count: int
    attendance_rate: Optional[float]
    moving_avg_7d: Optional[float]


class Trend(BaseModel):
    department: str
    items: list[TrendItem]


class ExplainOutput(BaseModel):
    endpoint: str
    collection: str
    explain: dict[str, Any]


# Reusable query-parameter types (FastAPI turns violations into 422 automatically).
PageQ = Annotated[int, Query(ge=1)]
PageSizeQ = Annotated[int, Query(ge=1, le=100)]
MonthQ = Annotated[str, Query(pattern=MONTH_PATTERN, description="Calendar month, YYYY-MM.")]
OptDateQ = Annotated[Optional[str], Query(pattern=DATE_PATTERN)]
LimitQ = Annotated[int, Query(ge=1, le=50)]


def skip_for(page: int, page_size: int) -> int:
    # R10: page starts at 1. Capped so an absurd page number returns an empty page instead of overflowing int64.
    return min((page - 1) * page_size, 2**62)


# =========================================================================== #
# 5. Endpoints: system, employees, attendance
# =========================================================================== #
@app.get("/health", tags=["system"])
def health():
    try:
        client.admin.command("ping")
        if not _indexes_ready:
            ensure_indexes()
    except PyMongoError:
        raise HTTPException(status_code=503, detail="MongoDB is not reachable")
    return {"status": "ok"}


def get_employee_or_404(emp_code: str) -> dict:
    employee = employees.find_one({"emp_code": emp_code}, {"_id": 0})
    if employee is None:
        raise HTTPException(status_code=404, detail=f"Employee {emp_code} not found")
    return employee


@app.post("/employees", status_code=201, response_model=Employee, tags=["employees"])
def create_employee(body: EmployeeCreate):
    doc = body.model_dump()
    doc["created_at"] = now_millis_precision()
    try:
        # No find-then-insert: the unique index decides atomically, so two concurrent creates cannot both win.
        employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail=f"Employee {body.emp_code} already exists")
    return serialize_employee(doc)


@app.get("/employees", response_model=EmployeePage, tags=["employees"])
def list_employees(department: Optional[str] = None, page: PageQ = 1, page_size: PageSizeQ = 20):
    query = {} if department is None else {"department": department}
    cursor = (
        employees.find(query, {"_id": 0})
        .sort([("emp_code", 1)])
        .skip(skip_for(page, page_size))
        .limit(page_size)
    )
    return {
        "items": [serialize_employee(doc) for doc in cursor],
        "total": employees.count_documents(query),  # same filter as the page (R10)
        "page": page,
        "page_size": page_size,
    }


@app.post("/attendance/punch-in", status_code=201, response_model=AttendanceRecord, tags=["attendance"])
def punch_in(body: PunchInRequest):
    employee = get_employee_or_404(body.emp_code)
    punched_at = ms_to_instant(body.punched_at) if body.punched_at is not None else now_instant()
    day = attendance_date(punched_at, employee["shift_start"], employee["shift_end"])
    doc = {
        "emp_code": body.emp_code,
        "date": day.isoformat(),
        "status": body.status,
        "punch_in": punched_at,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(punched_at, day, employee["shift_start"]),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        # The unique (emp_code, date) index makes this race-safe: of N identical concurrent inserts exactly one
        # succeeds and the others raise DuplicateKeyError.
        logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail=f"{body.emp_code} already has a record for {day.isoformat()}")
    return serialize_record(doc)


@app.post("/attendance/punch-out", response_model=AttendanceRecord, tags=["attendance"])
def punch_out(body: PunchOutRequest):
    employee = get_employee_or_404(body.emp_code)
    punched_at = ms_to_instant(body.punched_at) if body.punched_at is not None else now_instant()

    # Most recent record whose punch_in is at or before punched_at. Searching by punch_in (not by date) is what
    # makes this work across midnight. ABSENT/LEAVE records have punch_in null and never match a $lte on a date.
    record = logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": punched_at}}, sort=[("punch_in", -1)]
    )
    if record is None:
        raise HTTPException(status_code=404, detail=f"No punch-in found for {body.emp_code}")
    if record.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail=f"Record {record['date']} is already punched out")

    started = record["punch_in"]
    if punched_at <= started:
        raise unprocessable("punched_at must be after punch_in", "body", "punched_at")
    if punched_at - started > MAX_SHIFT_LENGTH:
        raise unprocessable("punched_at is more than 24 hours after punch_in", "body", "punched_at")

    day = Date.fromisoformat(record["date"])
    update = {"punch_out": punched_at, **punch_out_fields(employee, day, started, punched_at)}
    # Conditional update: only succeeds while punch_out is still null, so of two simultaneous punch-outs exactly
    # one matches; the loser gets None back and a 409.
    updated = logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
        {"$set": update},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(status_code=409, detail=f"Record {record['date']} is already punched out")
    return serialize_record(updated)


def attendance_filter(emp_code: Optional[str], date_from: Optional[str], date_to: Optional[str],
                      status: Optional[str]) -> dict:
    """Shared by GET /attendance and its explain, so both run the identical query."""
    if date_from is not None:
        parse_calendar_date(date_from, "query", "date_from")
    if date_to is not None:
        parse_calendar_date(date_to, "query", "date_to")
    if date_from is not None and date_to is not None and date_from > date_to:
        raise unprocessable("date_from must not be after date_to", "query", "date_from")
    query: dict = {}
    if emp_code is not None:
        query["emp_code"] = emp_code
    if date_from is not None or date_to is not None:
        # YYYY-MM-DD strings sort chronologically, so plain string comparison is a correct date range.
        query["date"] = {}
        if date_from is not None:
            query["date"]["$gte"] = date_from
        if date_to is not None:
            query["date"]["$lte"] = date_to
    if status is not None:
        query["status"] = status
    return query


ATTENDANCE_SORT = [("date", -1), ("emp_code", 1)]


@app.get("/attendance", response_model=AttendancePage, tags=["attendance"])
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: OptDateQ = None,
    date_to: OptDateQ = None,
    status: Optional[Status] = None,
    page: PageQ = 1,
    page_size: PageSizeQ = 20,
):
    query = attendance_filter(emp_code, date_from, date_to, status)
    # Sorting, skipping and limiting happen in MongoDB on an index; only one page ever reaches Python.
    cursor = logs.find(query, {"_id": 0}).sort(ATTENDANCE_SORT).skip(skip_for(page, page_size)).limit(page_size)
    return {
        "items": [serialize_record(doc) for doc in cursor],
        "total": logs.count_documents(query),
        "page": page,
        "page_size": page_size,
    }


@app.patch("/attendance/{emp_code}/{date}", response_model=AttendanceRecord, tags=["attendance"])
def regularize_attendance(emp_code: str, date: str, body: RegularizeRequest):
    if not re.fullmatch(DATE_PATTERN, date):
        raise unprocessable("date must be YYYY-MM-DD", "path", "date")
    day = parse_calendar_date(date, "path", "date")
    employee = get_employee_or_404(emp_code)
    record = logs.find_one({"emp_code": emp_code, "date": date})
    if record is None:
        raise HTTPException(status_code=404, detail=f"No attendance record for {emp_code} on {date}")

    supplied = body.model_fields_set & {"status", "punch_in", "punch_out"}
    if not supplied:
        raise unprocessable("nothing to change: supply status, punch_in and/or punch_out", "body")

    # Current state, with the documented defaults for fields that legacy records omit.
    current = {
        "status": record["status"],
        "punch_in": record.get("punch_in"),
        "punch_out": record.get("punch_out"),
        "work_hours": record.get("work_hours"),
        "late_minutes": record.get("late_minutes") or 0,
        "overtime_minutes": record.get("overtime_minutes") or 0,
        "half_day": bool(record.get("half_day", False)),
    }

    # 1. Apply the change on top of the current values (omitted fields are unchanged).
    status = body.status if "status" in supplied else current["status"]
    if status in NON_PRESENCE:
        if "punch_in" in supplied or "punch_out" in supplied:
            raise unprocessable(f"status {status} clears both punch times; do not send punch_in/punch_out",
                                "body", "punch_in" if "punch_in" in supplied else "punch_out")
        new_in = new_out = None
    else:
        new_in = ms_to_instant(body.punch_in) if "punch_in" in supplied else current["punch_in"]
        new_out = ms_to_instant(body.punch_out) if "punch_out" in supplied else current["punch_out"]
        if new_in is None:
            raise unprocessable(f"status {status} requires a punch_in", "body", "punch_in")
        if "punch_in" in supplied and attendance_date(new_in, employee["shift_start"], employee["shift_end"]) != day:
            raise unprocessable(f"punch_in must stay on the record's attendance date {date} (R1)", "body", "punch_in")
        # 2. Validate the FINAL record, including a stored punch_out that a new punch_in would invalidate.
        if new_out is not None:
            if new_out <= new_in:
                raise unprocessable("punch_out must be after punch_in", "body", "punch_out")
            if new_out - new_in > MAX_SHIFT_LENGTH:
                raise unprocessable("punch_out must be within 24 hours of punch_in", "body", "punch_out")

    # 3. Recompute the derived fields from the final times.
    final = {"status": status, "punch_in": new_in, "punch_out": new_out,
             **derived_fields(employee, day, status, new_in, new_out)}

    if all(final[f] == current[f] for f in ("status", "punch_in", "punch_out")):
        raise unprocessable("the request changes nothing", "body")

    # 4. Audit entry: only fields whose value actually changed.
    changes = {f: {"from": current[f], "to": final[f]} for f in TRACKED_FIELDS if final[f] != current[f]}
    entry = {"at": now_millis_precision(), "by": body.regularized_by, "reason": body.reason, "changes": changes}

    # 5. Optimistic concurrency: the update only applies if the record is still exactly as we read it (same
    #    punches/status and the same number of history entries). A concurrent correction or punch-out makes the
    #    filter miss, and this request gets 409 instead of silently overwriting the other one.
    guard = {
        "_id": record["_id"],
        "status": record["status"],
        "punch_in": record.get("punch_in"),
        "punch_out": record.get("punch_out"),
        "$expr": {"$eq": [{"$size": {"$ifNull": ["$history", []]}}, len(record.get("history") or [])]},
    }
    updated = logs.find_one_and_update(
        guard,
        {"$set": final, "$push": {"history": entry}},  # $push appends; history is never rewritten
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(status_code=409, detail="The record was modified concurrently; reload and retry")
    return serialize_record(updated)


# =========================================================================== #
# 6. Aggregation pipeline builders
#    Each analytics endpoint and its /admin/explain twin call the same builder, so explain shows the real plan.
# =========================================================================== #
def _half_up(expr: Any, places: int) -> dict:
    """Round a non-negative number HALF-UP inside MongoDB. ($round rounds half to EVEN, and doubles cannot hold
    values like 0.03125*10^4 exactly, so we convert to Decimal128 and use floor(x * 10^p + 0.5) / 10^p.)"""
    factor = Decimal128(str(10**places))
    return {"$divide": [{"$floor": {"$add": [{"$multiply": [{"$toDecimal": expr}, factor]}, Decimal128("0.5")]}},
                        factor]}


def _is_weekday(date_string: str) -> dict:
    """'YYYY-MM-DD' -> true for Mon-Fri. ($isoDayOfWeek: Monday = 1 ... Sunday = 7.)"""
    return {"$lte": [{"$isoDayOfWeek": {"$dateFromString": {"dateString": date_string, "format": "%Y-%m-%d"}}}, 5]}


def _present_credit(log_var: str) -> dict:
    """R5/R6: a presence-status record counts 1, or 0.5 when half_day is true; other statuses count 0."""
    return {"$cond": [{"$in": [f"{log_var}.status", list(PRESENCE)]},
                      {"$cond": [{"$eq": [f"{log_var}.half_day", True]}, 0.5, 1]},
                      0]}


def _count(array: str, cond: dict) -> dict:
    return {"$size": {"$filter": {"input": array, "as": "l", "cond": cond}}}


def _is_late(log_var: str) -> dict:
    return {"$gt": [{"$ifNull": [f"{log_var}.late_minutes", 0]}, 0]}


def _logs_lookup(date_match: dict, fields: list[str]) -> dict:
    """Join each employee to its attendance logs in a date range. localField/foreignField + pipeline uses the
    unique (emp_code, date) index for every employee."""
    return {"$lookup": {
        "from": logs.name,
        "localField": "emp_code",
        "foreignField": "emp_code",
        "pipeline": [{"$match": {"date": date_match}}, {"$project": {"_id": 0, **{f: 1 for f in fields}}}],
        "as": "logs",
    }}


def employee_monthly_pipeline(emp_code: str, month: str) -> list[dict]:
    first, next_first = month_bounds(month, "query", "month")
    month_start = datetime.combine(first, time(0), tzinfo=UTC)
    days_in_month = (next_first - first).days
    return [
        {"$match": {"emp_code": emp_code}},
        _logs_lookup({"$gte": first.isoformat(), "$lt": next_first.isoformat()},
                     ["date", "status", "half_day", "late_minutes", "overtime_minutes"]),
        {"$project": {
            "_id": 0,
            "emp_code": 1,
            # R7: count Mon-Fri days of the month that are on/after joined_on. $range gives day offsets 0..n-1.
            "working_days": {"$size": {"$filter": {
                "input": {"$range": [0, days_in_month]},
                "as": "i",
                "cond": {"$let": {
                    "vars": {"day": {"$dateAdd": {"startDate": month_start, "unit": "day", "amount": "$$i"}}},
                    "in": {"$and": [
                        {"$lte": [{"$isoDayOfWeek": "$$day"}, 5]},
                        {"$gte": [{"$dateToString": {"date": "$$day", "format": "%Y-%m-%d"}}, "$joined_on"]},
                    ]},
                }},
            }}},
            # R7: only Mon-Fri records count towards present_days.
            "present_days": {"$sum": {"$map": {"input": "$logs", "as": "l",
                                               "in": {"$cond": [_is_weekday("$$l.date"), _present_credit("$$l"), 0]}}}},
            "leave_days": _count("$logs", {"$eq": ["$$l.status", "LEAVE"]}),
            "late_count": _count("$logs", _is_late("$$l")),
            # Weekend records still add to the totals. $sum over an array skips missing values (missing = 0).
            "total_late_minutes": {"$sum": "$logs.late_minutes"},
            "total_overtime_minutes": {"$sum": "$logs.overtime_minutes"},
        }},
        {"$set": {"attendance_pct": {"$cond": [
            {"$eq": ["$working_days", 0]},
            None,
            _half_up({"$divide": [{"$multiply": [{"$toDecimal": "$present_days"}, 100]}, "$working_days"]}, 2),
        ]}}},
    ]


def department_summary_pipeline(month: str, department: Optional[str]) -> list[dict]:
    first, next_first = month_bounds(month, "query", "month")
    month_end = (next_first - timedelta(days=1)).isoformat()
    match: dict = {"joined_on": {"$lte": month_end}}  # R9: joined by the end of the month, logs or not
    if department is not None:
        match["department"] = department
    qualifies_for_avg = {"$and": [{"$in": ["$$l.status", list(PRESENCE)]}, {"$isNumber": "$$l.work_hours"}]}
    return [
        # Start from EMPLOYEES, not logs: every employee is one row, so people with zero logs are still counted.
        {"$match": match},
        _logs_lookup({"$gte": first.isoformat(), "$lt": next_first.isoformat()},
                     ["date", "status", "half_day", "work_hours", "late_minutes"]),
        # Per-employee figures (one document per employee).
        {"$project": {
            "_id": 0,
            "department": 1,
            "present_days": {"$sum": {"$map": {"input": "$logs", "as": "l",
                                               "in": {"$cond": [_is_weekday("$$l.date"), _present_credit("$$l"), 0]}}}},
            # Sum of work_hours as Decimal128 (exact), and how many records qualified: the department average is
            # then total / count over RECORDS, not an average of per-employee averages.
            "wh_sum": {"$sum": {"$map": {"input": {"$filter": {"input": "$logs", "as": "l", "cond": qualifies_for_avg}},
                                         "as": "l", "in": {"$toDecimal": "$$l.work_hours"}}}},
            "wh_count": _count("$logs", qualifies_for_avg),
            "late_count": _count("$logs", _is_late("$$l")),
            "total_late_minutes": {"$sum": "$logs.late_minutes"},
            "leave_count": _count("$logs", {"$eq": ["$$l.status", "LEAVE"]}),
            "on_duty_count": _count("$logs", {"$eq": ["$$l.status", "ON_DUTY"]}),
        }},
        {"$group": {
            "_id": "$department",
            "headcount": {"$sum": 1},
            "present_days": {"$sum": "$present_days"},
            "wh_sum": {"$sum": "$wh_sum"},
            "wh_count": {"$sum": "$wh_count"},
            "late_count": {"$sum": "$late_count"},
            "total_late_minutes": {"$sum": "$total_late_minutes"},
            "leave_count": {"$sum": "$leave_count"},
            "on_duty_count": {"$sum": "$on_duty_count"},
        }},
        {"$project": {
            "_id": 0,
            "department": "$_id",
            "headcount": 1,
            "present_days": 1,
            "avg_work_hours": {"$cond": [{"$eq": ["$wh_count", 0]}, None,
                                         _half_up({"$divide": ["$wh_sum", "$wh_count"]}, 2)]},
            "late_count": 1,
            "total_late_minutes": 1,
            "leave_count": 1,
            "on_duty_count": 1,
        }},
        {"$sort": {"department": 1}},
    ]


def late_leaderboard_pipeline(month: str, limit: int, department: Optional[str]) -> list[dict]:
    first, next_first = month_bounds(month, "query", "month")
    pipeline: list[dict] = [
        {"$match": {"date": {"$gte": first.isoformat(), "$lt": next_first.isoformat()}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
        # Name and department come from employees; $unwind drops logs whose employee does not exist.
        {"$lookup": {"from": employees.name, "localField": "_id", "foreignField": "emp_code",
                     "pipeline": [{"$project": {"_id": 0, "name": 1, "department": 1}}], "as": "employee"}},
        {"$unwind": "$employee"},
    ]
    if department is not None:
        pipeline.append({"$match": {"employee.department": department}})  # filter BEFORE ranking
    pipeline += [
        # $rank = standard competition ranking: ties share a rank and the next rank is skipped (1, 2, 2, 4).
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},  # limit applies to the RANK, so every tie at the cutoff is kept
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id", "name": "$employee.name",
                      "department": "$employee.department", "total_late_minutes": 1, "late_count": 1}},
    ]
    return pipeline


def trend_range(from_: str, to: str) -> tuple[Date, Date]:
    start = parse_calendar_date(from_, "query", "from")
    end = parse_calendar_date(to, "query", "to")
    if end < start:
        raise unprocessable("to must not be before from", "query", "to")
    if (end - start).days + 1 > TREND_MAX_DAYS:
        raise unprocessable(f"the range may span at most {TREND_MAX_DAYS} days", "query", "to")
    return start, end


def department_trend_pipeline(department: str, from_: str, to: str) -> list[dict]:
    start, end = trend_range(from_, to)
    from_s, to_s = start.isoformat(), end.isoformat()
    try:
        upper_bound = datetime.combine(end + timedelta(days=1), time(0), tzinfo=UTC)  # $densify bound is exclusive
    except OverflowError:
        raise unprocessable("to is out of range", "query", "to")
    return [
        {"$match": {"department": department, "joined_on": {"$lte": to_s}}},
        _logs_lookup({"$gte": from_s, "$lte": to_s}, ["date", "status", "half_day", "late_minutes"]),
        # Turn each employee into "events" on dates: one join event (+1 headcount from max(joined_on, from)) and
        # one event per log carrying its present credit and late flag.
        {"$project": {"_id": 0, "events": {"$concatArrays": [
            [{"date": {"$max": ["$joined_on", from_s]}, "joined": 1, "present": 0, "late": 0}],
            {"$map": {"input": "$logs", "as": "l", "in": {
                "date": "$$l.date", "joined": 0, "present": _present_credit("$$l"),
                "late": {"$cond": [_is_late("$$l"), 1, 0]},
            }}},
        ]}}},
        {"$unwind": "$events"},
        {"$replaceWith": "$events"},
        # A neutral seed row on `from`, so the series exists even if nobody in the department has joined yet.
        {"$unionWith": {"pipeline": [{"$documents": [{"date": from_s, "joined": 0, "present": 0, "late": 0}]}]}},
        {"$group": {"_id": "$date", "joined": {"$sum": "$joined"}, "present_count": {"$sum": "$present"},
                    "late_count": {"$sum": "$late"}}},
        {"$set": {"day": {"$dateFromString": {"dateString": "$_id", "format": "%Y-%m-%d", "timezone": "UTC"}}}},
        # Gap filling: insert a document for every missing calendar day in [from, to] (upper bound exclusive).
        {"$densify": {"field": "day", "range": {
            "step": 1, "unit": "day", "bounds": [datetime.combine(start, time(0), tzinfo=UTC), upper_bound]}}},
        # R9 per day: running total of join events = employees with joined_on <= that day.
        {"$setWindowFields": {"sortBy": {"day": 1}, "output": {
            "headcount": {"$sum": {"$ifNull": ["$joined", 0]}, "window": {"documents": ["unbounded", "current"]}},
        }}},
        {"$set": {
            "is_working_day": {"$lte": [{"$isoDayOfWeek": "$day"}, 5]},
            "present_count": {"$ifNull": ["$present_count", 0]},
            "late_count": {"$ifNull": ["$late_count", 0]},
        }},
        {"$set": {"attendance_rate": {"$cond": [
            {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
            _half_up({"$divide": [{"$toDecimal": "$present_count"}, "$headcount"]}, 4),
            None,
        ]}}},
        # Current row + up to 6 preceding rows, all inside the requested range. $avg ignores nulls and returns
        # null when the window has no numbers at all.
        {"$setWindowFields": {"sortBy": {"day": 1}, "output": {
            "moving_avg_7d": {"$avg": "$attendance_rate", "window": {"documents": [-6, "current"]}},
        }}},
        {"$project": {
            "_id": 0,
            "date": {"$dateToString": {"date": "$day", "format": "%Y-%m-%d"}},
            "is_working_day": 1,
            "headcount": 1,
            "present_count": 1,
            "late_count": 1,
            "attendance_rate": 1,
            "moving_avg_7d": {"$cond": [{"$eq": [{"$ifNull": ["$moving_avg_7d", None]}, None]}, None,
                                        _half_up("$moving_avg_7d", 4)]},
        }},
        {"$sort": {"date": 1}},
    ]


# =========================================================================== #
# 7. Endpoints: analytics, admin
# =========================================================================== #
@app.get("/analytics/employees/{emp_code}/monthly", response_model=EmployeeMonthly, tags=["analytics"])
def employee_monthly(emp_code: str, month: MonthQ):
    rows = list(employees.aggregate(employee_monthly_pipeline(emp_code, month)))
    if not rows:  # the pipeline starts from employees, so no row means no such employee
        raise HTTPException(status_code=404, detail=f"Employee {emp_code} not found")
    row = rows[0]
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": int(row["working_days"]),
        "present_days": to_float(row["present_days"]),
        "leave_days": int(row["leave_days"]),
        "late_count": int(row["late_count"]),
        "total_late_minutes": int(row["total_late_minutes"]),
        "total_overtime_minutes": int(row["total_overtime_minutes"]),
        "attendance_pct": to_float(row["attendance_pct"]),
    }


@app.get("/analytics/departments/summary", response_model=DepartmentSummary, tags=["analytics"])
def department_summary(month: MonthQ, department: Optional[str] = None):
    items = [
        {
            "department": row["department"],
            "headcount": int(row["headcount"]),
            "present_days": to_float(row["present_days"]),
            "avg_work_hours": to_float(row["avg_work_hours"]),
            "late_count": int(row["late_count"]),
            "total_late_minutes": int(row["total_late_minutes"]),
            "leave_count": int(row["leave_count"]),
            "on_duty_count": int(row["on_duty_count"]),
        }
        for row in employees.aggregate(department_summary_pipeline(month, department))
    ]
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late", response_model=Leaderboard, tags=["analytics"])
def late_leaderboard(month: MonthQ, limit: LimitQ = 10, department: Optional[str] = None):
    items = [
        {**row, "total_late_minutes": int(row["total_late_minutes"]), "late_count": int(row["late_count"]),
         "rank": int(row["rank"])}
        for row in logs.aggregate(late_leaderboard_pipeline(month, limit, department))
    ]
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend", response_model=Trend, tags=["analytics"])
def department_trend(
    department: str,
    from_: Annotated[str, Query(alias="from", pattern=DATE_PATTERN)],
    to: Annotated[str, Query(pattern=DATE_PATTERN)],
):
    pipeline = department_trend_pipeline(department, from_, to)  # validates the range first (422)
    if employees.find_one({"department": department}, {"_id": 1}) is None:
        raise HTTPException(status_code=404, detail=f"Department {department} not found")
    items = [
        {
            "date": row["date"],
            "is_working_day": row["is_working_day"],
            "headcount": int(row["headcount"]),
            "present_count": to_float(row["present_count"]),
            "late_count": int(row["late_count"]),
            "attendance_rate": to_float(row.get("attendance_rate")),
            "moving_avg_7d": to_float(row.get("moving_avg_7d")),
        }
        for row in employees.aggregate(pipeline)
    ]
    return {"department": department, "items": items}


class ExplainTarget(str, Enum):
    attendance_list = "attendance_list"
    employee_monthly = "employee_monthly"
    department_summary = "department_summary"
    late_leaderboard = "late_leaderboard"
    department_trend = "department_trend"


def _require(value: Optional[str], name: str) -> str:
    if value is None:
        raise HTTPException(status_code=422, detail=[{"type": "missing", "loc": ["query", name],
                                                      "msg": "Field required"}])
    return value


@app.get("/admin/explain/{endpoint}", response_model=ExplainOutput, tags=["admin"])
def explain_endpoint(
    endpoint: ExplainTarget,  # an Enum: any other identifier is a 422, so no arbitrary query can be run
    emp_code: Optional[str] = None,
    month: Annotated[Optional[str], Query(pattern=MONTH_PATTERN)] = None,
    department: Optional[str] = None,
    limit: LimitQ = 10,
    date_from: OptDateQ = None,
    date_to: OptDateQ = None,
    status: Optional[Status] = None,
    from_: Annotated[Optional[str], Query(alias="from", pattern=DATE_PATTERN)] = None,
    to: OptDateQ = None,
    page: PageQ = 1,
    page_size: PageSizeQ = 20,
):
    if endpoint is ExplainTarget.attendance_list:
        collection = logs.name
        command: dict = {
            "find": collection,
            "filter": attendance_filter(emp_code, date_from, date_to, status),
            "projection": {"_id": 0},
            "sort": dict(ATTENDANCE_SORT),
            "skip": skip_for(page, page_size),
            "limit": page_size,
        }
    else:
        if endpoint is ExplainTarget.employee_monthly:
            collection, pipeline = employees.name, employee_monthly_pipeline(_require(emp_code, "emp_code"),
                                                                             _require(month, "month"))
        elif endpoint is ExplainTarget.department_summary:
            collection, pipeline = employees.name, department_summary_pipeline(_require(month, "month"), department)
        elif endpoint is ExplainTarget.late_leaderboard:
            collection, pipeline = logs.name, late_leaderboard_pipeline(_require(month, "month"), limit, department)
        else:
            collection, pipeline = employees.name, department_trend_pipeline(
                _require(department, "department"), _require(from_, "from"), _require(to, "to"))
        command = {"aggregate": collection, "pipeline": pipeline, "cursor": {}}

    raw = db.command({"explain": command, "verbosity": "executionStats"})
    # Relaxed Extended JSON turns BSON-only types (Timestamp, ObjectId, Decimal128...) into plain JSON.
    return {"endpoint": endpoint.value, "collection": collection, "explain": json.loads(json_util.dumps(raw))}


# =========================================================================== #
# 8. Extras beyond the contract
#    Additive only: new paths tagged "extras" (plus the request-id middleware in section 1). No contract path,
#    field or status code changes. /dashboard is excluded from the OpenAPI schema.
# =========================================================================== #
class RecordHistory(BaseModel):
    emp_code: str
    date: str
    history: list[HistoryEntry]


@app.get("/attendance/{emp_code}/{date}/history", response_model=RecordHistory, tags=["extras"])
def attendance_history(emp_code: str, date: str):
    """The audit trail of one record (manual corrections only), oldest first. Same 404/422 rules as the PATCH."""
    if not re.fullmatch(DATE_PATTERN, date):
        raise unprocessable("date must be YYYY-MM-DD", "path", "date")
    parse_calendar_date(date, "path", "date")
    get_employee_or_404(emp_code)
    record = logs.find_one({"emp_code": emp_code, "date": date}, {"_id": 0, "history": 1})  # unique-index lookup
    if record is None:
        raise HTTPException(status_code=404, detail=f"No attendance record for {emp_code} on {date}")
    return {"emp_code": emp_code, "date": date,
            "history": [serialize_history(h) for h in (record.get("history") or [])]}


def on_time_streak_pipeline(emp_code: str, month: str) -> list[dict]:
    """Longest run of consecutive WORKING days (Mon-Fri, from joined_on) on which the employee was present and not
    late. Weekends are skipped, not breaks; a working day with no record, a late punch, ABSENT or LEAVE breaks it.

    Gaps-and-islands: number the month's working days 0, 1, 2, ... (idx); keep only on-time days and number them
    again 1, 2, 3, ... (pos, via $setWindowFields). Inside one unbroken run idx and pos rise together, so
    idx - pos is constant per run and works as a group key."""
    first, next_first = month_bounds(month, "query", "month")
    month_start = datetime.combine(first, time(0), tzinfo=UTC)
    on_time = {"$and": [{"$in": ["$$l.status", list(PRESENCE)]}, {"$eq": [{"$ifNull": ["$$l.late_minutes", 0]}, 0]}]}
    return [
        {"$match": {"emp_code": emp_code}},
        _logs_lookup({"$gte": first.isoformat(), "$lt": next_first.isoformat()}, ["date", "status", "late_minutes"]),
        {"$project": {
            "_id": 0,
            "on_time_dates": {"$map": {"input": {"$filter": {"input": "$logs", "as": "l", "cond": on_time}},
                                       "as": "l", "in": "$$l.date"}},
            "working_days": {"$filter": {
                "input": {"$map": {"input": {"$range": [0, (next_first - first).days]}, "as": "i",
                                   "in": {"$dateAdd": {"startDate": month_start, "unit": "day", "amount": "$$i"}}}},
                "as": "d",
                "cond": {"$and": [{"$lte": [{"$isoDayOfWeek": "$$d"}, 5]},
                                  {"$gte": [{"$dateToString": {"date": "$$d", "format": "%Y-%m-%d"}}, "$joined_on"]}]},
            }},
        }},
        {"$unwind": {"path": "$working_days", "includeArrayIndex": "idx"}},
        {"$project": {"idx": 1, "on_time_dates": 1,
                      "date": {"$dateToString": {"date": "$working_days", "format": "%Y-%m-%d"}}}},
        {"$match": {"$expr": {"$in": ["$date", "$on_time_dates"]}}},
        {"$setWindowFields": {"sortBy": {"idx": 1}, "output": {"pos": {"$documentNumber": {}}}}},
        {"$group": {"_id": {"$subtract": ["$idx", "$pos"]}, "days": {"$sum": 1},
                    "start": {"$min": "$date"}, "end": {"$max": "$date"}}},
        {"$sort": {"days": -1, "start": 1}},  # longest first; ties -> earliest run
        {"$group": {"_id": None, "on_time_days": {"$sum": "$days"}, "runs": {"$sum": 1},
                    "best": {"$first": {"days": "$days", "start": "$start", "end": "$end"}}}},
    ]


class Streak(BaseModel):
    days: int
    start: Optional[str]
    end: Optional[str]


class EmployeeStreaks(BaseModel):
    emp_code: str
    month: str
    on_time_days: int
    on_time_runs: int
    longest_on_time_streak: Streak


@app.get("/analytics/employees/{emp_code}/streaks", response_model=EmployeeStreaks, tags=["extras"])
def employee_streaks(emp_code: str, month: MonthQ):
    pipeline = on_time_streak_pipeline(emp_code, month)
    get_employee_or_404(emp_code)
    rows = list(employees.aggregate(pipeline))
    row = rows[0] if rows else {"on_time_days": 0, "runs": 0, "best": {"days": 0, "start": None, "end": None}}
    return {"emp_code": emp_code, "month": month, "on_time_days": int(row["on_time_days"]),
            "on_time_runs": int(row["runs"]), "longest_on_time_streak": row["best"]}


@app.get("/dashboard", response_class=HTMLResponse, include_in_schema=False)
def dashboard():
    """A read-only page over the analytics endpoints. It only calls this API, loads nothing from the internet, and
    writes every database value with textContent (never innerHTML), so stored names cannot inject markup."""
    return DASHBOARD_HTML


DASHBOARD_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Attendance dashboard</title>
<style>
:root{--bg:#f7f7f5;--card:#fff;--ink:#1d1d1b;--muted:#6b6b66;--line:#e3e2dd;--accent:#2f6fde;--accent2:#d9822b}
@media (prefers-color-scheme:dark){:root{--bg:#161615;--card:#1f1f1d;--ink:#ecebe6;--muted:#9c9b95;--line:#33322f;
--accent:#6b9bf0;--accent2:#e9a35a}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.45 system-ui,sans-serif}
header{padding:20px 24px 8px}h1{font-size:20px;margin:0 0 4px}header p{margin:0;color:var(--muted)}
.controls{display:flex;flex-wrap:wrap;gap:12px;padding:12px 24px}
label{display:flex;flex-direction:column;font-size:12px;color:var(--muted);gap:4px}
input,select{font:inherit;padding:6px 8px;border:1px solid var(--line);border-radius:6px;background:var(--card);color:var(--ink)}
main{display:grid;gap:16px;padding:8px 24px 32px;grid-template-columns:repeat(auto-fit,minmax(340px,1fr))}
section{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px;overflow-x:auto}
section.wide{grid-column:1/-1}h2{font-size:15px;margin:0 0 10px}
table{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums}
th,td{text-align:right;padding:6px 8px;border-bottom:1px solid var(--line);white-space:nowrap}
th:first-child,td:first-child,th.l,td.l{text-align:left}th{font-weight:600;color:var(--muted);font-size:12px}
.empty,.err{color:var(--muted);padding:8px 0}.err{color:#c0392b}
svg text{fill:var(--muted);font-size:11px}.legend{display:flex;gap:16px;font-size:12px;color:var(--muted);margin-top:6px}
.sw{display:inline-block;width:12px;height:3px;vertical-align:middle;margin-right:6px}
</style></head><body>
<header><h1>Attendance dashboard</h1><p>Read-only view of the analytics endpoints of this API.</p></header>
<div class="controls">
<label>Month<input id="month" type="month"></label>
<label>Department (trend)<select id="dept"></select></label>
</div>
<main>
<section class="wide"><h2>Department summary</h2><div id="summary"></div></section>
<section><h2>Late-comers leaderboard</h2><div id="board"></div></section>
<section><h2>Daily attendance rate</h2><div id="trend"></div>
<div class="legend"><span><i class="sw" style="background:var(--accent)"></i>attendance rate</span>
<span><i class="sw" style="background:var(--accent2)"></i>7-day moving average</span></div></section>
</main>
<script>
const $ = id => document.getElementById(id);
const el = (tag, text, cls) => { const e = document.createElement(tag); if (text !== undefined) e.textContent = text;
  if (cls) e.className = cls; return e; };
const fmt = v => v === null || v === undefined ? "-" : String(v);
async function api(path) { const r = await fetch(path); if (!r.ok) throw new Error(r.status + " " + path); return r.json(); }
function table(target, cols, rows) {
  target.replaceChildren();
  if (!rows.length) { target.append(el("div", "No data for this month.", "empty")); return; }
  const t = el("table"), head = el("tr");
  cols.forEach(([label, , left]) => head.append(el("th", label, left ? "l" : "")));
  t.append(head);
  rows.forEach(r => { const tr = el("tr"); cols.forEach(([, key, left]) => tr.append(el("td", fmt(r[key]), left ? "l" : "")));
    t.append(tr); });
  target.append(t);
}
function chart(target, items) {
  target.replaceChildren();
  if (!items.length) { target.append(el("div", "No data.", "empty")); return; }
  const W = 560, H = 200, L = 36, R = 8, T = 8, B = 22, ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg"); svg.setAttribute("viewBox", `0 0 ${W} ${H}`); svg.style.width = "100%";
  const x = i => L + (items.length < 2 ? 0 : i * (W - L - R) / (items.length - 1)), y = v => T + (1 - v) * (H - T - B);
  const add = (tag, attrs, text) => { const n = document.createElementNS(ns, tag);
    Object.entries(attrs).forEach(([k, v]) => n.setAttribute(k, v)); if (text) n.textContent = text; svg.append(n); };
  [0, 0.5, 1].forEach(v => { add("line", {x1: L, x2: W - R, y1: y(v), y2: y(v), stroke: "var(--line)"});
    add("text", {x: 4, y: y(v) + 4}, (v * 100) + "%"); });
  add("text", {x: L, y: H - 4}, items[0].date); add("text", {x: W - R, y: H - 4, "text-anchor": "end"}, items.at(-1).date);
  const path = key => { let d = "", pen = false;
    items.forEach((it, i) => { const v = it[key]; if (v === null) { pen = false; return; }
      d += (pen ? "L" : "M") + x(i).toFixed(1) + " " + y(v).toFixed(1); pen = true; }); return d; };
  add("path", {d: path("attendance_rate"), fill: "none", stroke: "var(--accent)", "stroke-width": 2});
  add("path", {d: path("moving_avg_7d"), fill: "none", stroke: "var(--accent2)", "stroke-width": 2});
  target.append(svg);
}
async function load() {
  const month = $("month").value; if (!month) return;
  const [y, m] = month.split("-").map(Number), last = new Date(Date.UTC(y, m, 0)).getUTCDate();
  try {
    const summary = await api(`/analytics/departments/summary?month=${month}`);
    table($("summary"), [["Department", "department", true], ["Headcount", "headcount"], ["Present days", "present_days"],
      ["Avg hours", "avg_work_hours"], ["Late", "late_count"], ["Late min", "total_late_minutes"], ["Leave", "leave_count"],
      ["On duty", "on_duty_count"]], summary.items);
    const sel = $("dept"), keep = sel.value; sel.replaceChildren();
    summary.items.forEach(i => sel.append(el("option", i.department)));
    if ([...sel.options].some(o => o.value === keep)) sel.value = keep;
    const board = await api(`/analytics/leaderboard/late?month=${month}&limit=10`);
    table($("board"), [["Rank", "rank"], ["Employee", "emp_code", true], ["Name", "name", true],
      ["Late min", "total_late_minutes"], ["Late days", "late_count"]], board.items);
    if (sel.value) {
      const t = await api(`/analytics/departments/${encodeURIComponent(sel.value)}/trend?from=${month}-01&to=${month}-${String(last).padStart(2, "0")}`);
      chart($("trend"), t.items);
    } else { chart($("trend"), []); }
  } catch (e) { $("summary").replaceChildren(el("div", "Could not load: " + e.message, "err")); }
}
const now = new Date(); $("month").value = now.toISOString().slice(0, 7);
$("month").addEventListener("change", load); $("dept").addEventListener("change", load); load();
</script></body></html>
"""
