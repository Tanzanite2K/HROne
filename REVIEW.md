# REVIEW.md

Defects found in the starter `app/main.py` (line numbers refer to the **original starter file**). Every fix lives in
the new `app/main.py`; the test that exercises it is named in the last column (`tests/test_rules.py` = R1-R5 unit
tests, `tests/test_validation.py` = request validation, `tests/test_integration.py` = real MongoDB + uvicorn).

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
|---|---|---|---|---|
| 1 | module, L22 `MongoClient(...)` | No `tz_aware=True`: PyMongo hands back **naive** datetimes, so instants lose their UTC meaning and later mix with aware IST datetimes. | `compute_overtime` subtracts a naive `punch_out` from an aware shift end -> `TypeError` -> 500. | `MongoClient(..., tz_aware=True, tzinfo=UTC)`; all instants are aware UTC internally (`test_instant_round_trip_and_naive_is_utc`). |
| 2 | module (whole file) | **No indexes are created.** The seeded DB only has `_id`, so there is no uniqueness for `emp_code` or (`emp_code`,`date`) and every list/analytics query is a COLLSCAN. | Two parallel punch-ins both get 201; `/admin/explain` shows COLLSCAN on 100k docs. | `ensure_indexes()` runs at startup (lifespan, idempotent, named indexes) and is retried by `/health` if Mongo was down (`test_indexes_created_at_startup`, `test_explain_uses_indexes`). |
| 3 | `compute_late_minutes`, L34-37 | Compares whole minutes `minutes > 10` instead of "strictly more than 10:00": 09:40:30 gives `int(10.5)=10`, not `> 10`, so 0 instead of 10. | Shift 09:30, punch 09:40:01 -> starter 0, contract 10. | Compare the `timedelta` with `> 10 min`, then floor minutes since shift start (`test_r2_late_day_shift`). |
| 4 | `compute_late_minutes`, L35 | `punch_in.replace(hour=h, minute=m)` builds the shift start in the **punch's own time zone** (UTC/naive), not IST: off by 5h30. | Punch 09:45 IST = 04:15 UTC; shift start becomes 09:30 UTC -> negative -> 0 late. | Shift start = `datetime.combine(attendance_date, shift_start, tzinfo=IST)`. |
| 5 | `compute_late_minutes`, L35 | Overnight shifts: a punch at 01:00 for a 22:00 shift is compared with 22:00 **of the same calendar day**. | 22:00-06:00 shift, punch 01:00 -> 0 instead of 180. | Uses the R1 attendance date (previous day) for the shift start (`test_r2_late_overnight_after_midnight`). |
| 6 | `compute_work_hours`, L41 | `round()` on a float is not half-up (binary floats + banker's rounding). Also no truncation to whole seconds. | 29250 s = 8.125 h -> `round` gives 8.12, contract 8.13. | `Decimal(seconds)/3600` quantized with `ROUND_HALF_UP` on whole seconds (`test_r4_work_hours_half_up`, `test_r4_python_round_would_be_wrong`). |
| 7 | `compute_overtime`, L47 | No 30-minute minimum: any positive difference is overtime. | Sample doc 1 (out 18:35) -> 5, contract 0. | `minutes if minutes >= 30 else 0` (`test_r3_overtime`). |
| 8 | `compute_overtime`, L46 | Shift end is always on `date_str`; for overnight shifts it must be the **next** day. | 22:00-06:00, out 06:40 next day -> 1480 min instead of 40. | `shift_end_at()` adds a day when `shift_end <= shift_start`. |
| 9 | `EmployeeIn`, L53-60 | No validation at all: any `emp_code`, email, empty name, `25:99` shifts, `shift_start == shift_end`, invalid `joined_on`. | `POST /employees {"emp_code":"x","joined_on":"hello"}` -> 201. | Pydantic constraints from `EmployeeCreate` in the contract + `shift_start != shift_end` + real-date check (`test_create_employee_rejects_invalid_input`). |
| 10 | `create_employee`, L79-83 | Check-then-insert: two concurrent creates both pass `find_one` and both insert. | 12 parallel identical POSTs -> several 201s, duplicate employees. | Insert directly and map `DuplicateKeyError` (unique index) to 409 (`test_concurrent_duplicate_creates_yield_one_201`). |
| 11 | `create_employee`, L82/L85 | `created_at = datetime.now()`: naive **server-local** time (stored shifted by the server's offset) and returned as an ISO string instead of epoch ms. | Response `"created_at":"2026-...T..."`; on an IST server the stored time is 5h30 off. | `datetime.now(UTC)` at ms precision; serialised to epoch ms (`test_create_employee_contract_and_storage`). |
| 12 | `list_employees`, L93 | `skip = page * page_size`: page 1 skips the first page. | `GET /employees?page=1` misses the first 20 employees. | `skip = (page - 1) * page_size` (`test_list_employees_filter_sort_pagination`). |
| 13 | `list_employees`, L94 | `total` counts the whole collection, ignoring `department`. | `?department=Sales` returns 2 items but `total: 7`. | `count_documents(query)` with the same filter (R10). |
| 14 | `list_employees`, L95 | No sort, so order is undefined (contract: `emp_code` ascending) and pages can overlap. | Insert codes out of order -> list comes back in insertion order. | `.sort("emp_code", 1)` backed by an index. |
| 15 | `list_employees` / `list_attendance`, L89/L130 | No bounds on `page`/`page_size`: `page=0` gives a negative skip (500 from MongoDB), `page_size=100000` dumps the collection. | `GET /employees?page=0` -> 500. | `page >= 1`, `1 <= page_size <= 100` -> 422 (`test_list_employees_pagination_bounds`). |
| 16 | `PunchInIn`, L64-66 | `punched_at: Optional[int]` is lax: accepts seconds (`1783310280`), negative numbers, `"1783310280000"` and `1.78e12`; `status: str` accepts `ABSENT`, `LEAVE` or garbage. | Punch-in with `punched_at` in seconds creates a record in 1970. | Strict `EpochMillis` (int, 1e11..4102444800000) and `PresenceStatus` literal (`test_punch_in_rejects_bad_timestamps`, `test_punch_in_accepts_only_presence_statuses`). |
| 17 | `punch_in`, L101/L113 | Unknown employee: `emp` is `None`, `emp["shift_start"]` raises `TypeError` -> 500. | `POST /attendance/punch-in {"emp_code":"EMP9999"}` -> 500. | 404 via `get_employee_or_404` (`test_punch_in_unknown_employee_404`). |
| 18 | `punch_in`, L102-103 | `datetime.fromtimestamp(ms/1000)` uses the **server's local zone** and returns a naive datetime; `date` is the server-local date, not the IST date; overnight rule (R1) ignored; milliseconds not truncated. | Punch at 00:30 IST on a UTC server is dated the previous day; a 01:00 punch on a night shift gets its own date. | `ms_to_instant` (UTC, truncated to seconds) + `attendance_date()` implementing R1 (`test_r1_attendance_date`, `test_punch_in_uses_ist_date_not_utc`, `test_punch_in_overnight_shift`). |
| 19 | `punch_in`, L102 | `if body.punched_at` treats the value as a truthiness flag. | Harmless today (0 is now rejected), but wrong intent. | `is not None`. |
| 20 | `punch_in`, L104-118 | Check-then-insert race: simultaneous identical punch-ins all pass the `find_one`. | 16 parallel punch-ins -> several 201s and duplicate (emp_code, date) docs. | Unique (`emp_code`,`date`) index; insert and map `DuplicateKeyError` to 409 (`test_concurrent_identical_punch_ins_yield_one_201`). |
| 21 | `punch_in` / `list_attendance`, L119/L149 | Adds an `id` field (stringified ObjectId). The contract says records have no id; `_id` must never leak. | Response has an extra `id` key; contract validator fails. | Projections exclude `_id`; serialiser emits exactly the `AttendanceRecord` fields (contract checker rejects extra keys). |
| 22 | `punch_in` / `list_attendance` | Datetimes are returned as ISO strings (FastAPI's default), not epoch ms; history values are not converted either. | `"punch_in": "2026-07-06T04:35:00"`. | `serialize_record` / `serialize_history` convert every instant, including `history[].changes.*.from/to` (`test_sample_data_round_trip`). |
| 23 | `list_attendance`, L144-147 | Loads **the whole collection** into Python, sorts and slices it there. Unusable at 100k docs; also sorts by `date` only (contract: `date` desc then `emp_code` asc). | Latency/memory grows with the collection; tie order between employees on the same date is random. | `find().sort(date -1, emp_code 1).skip().limit()` on the `date_desc_emp_code` index + `count_documents` (`test_list_attendance_sort_filter_pagination`). |
| 24 | `list_attendance`, L127-142 | No validation: `date_from > date_to` silently returns nothing (contract: 422); `status`/date formats are unchecked. | `?date_from=2026-07-10&date_to=2026-07-01` -> 200 with an empty page. | Date pattern + real-date check, `Status` enum, `date_from > date_to` -> 422 (`test_list_attendance_rejects`). |
| 25 | `list_attendance` | Legacy records without `half_day`/`history` are returned with those keys missing. | Sample doc 9 has no `half_day` key in the response. | Serialiser fills documented defaults (`false`, `[]`, 0). |
| 26 | `health`, L73-74 | Always 200, even when MongoDB is down (contract: 503 unless a ping succeeds). | Stop MongoDB -> `/health` still 200. | `ping`; any `PyMongoError` -> 503 (`test_health_is_503_when_mongodb_is_down`). |
| 27 | whole file | No handling of a lost DB connection: every endpoint returns 500. | Kill MongoDB mid-run -> 500s. | `ConnectionFailure` handler -> 503 (`test_db_errors_become_503`). |
| 28 | L154-161 | Five endpoints and the explain endpoint are missing. | 404 on `/attendance/punch-out` etc. | Implemented per `openapi.yaml`. |

## Looked at and decided were **not** defects
- `load_dotenv()` (L20): its default is `override=False`, so real environment variables already win over `.env` as
  the contract requires. Kept, with `override=False` written out to make the intent visible.
- `IST = timezone(timedelta(hours=5, minutes=30))` (L25): a fixed offset is exactly right; India has no daylight
  saving time, so there is no need for `zoneinfo`.
- Synchronous PyMongo inside plain `def` endpoints: FastAPI runs them in a thread pool and `MongoClient` is
  thread-safe, so concurrency is real. Switching to Motor would not change correctness.
- `status_code=201` on both creates, and `datetime.fromisoformat(date_str).replace(tzinfo=IST)` in
  `compute_overtime`: attaching IST to the shift end was the correct idea; only the next-day case was missing.
- `uvicorn app.main:app` / module-level `app`: fine. The `MongoClient` connects lazily, so importing the module does
  not block startup.
- Kit, not `main.py`: `README.md` says `cp .env.example .env` but the kit has no `.env.example`. I added one with
  local placeholders only.

## Test status
- Unit and validation tests (`tests/test_rules.py`, `tests/test_validation.py`): **run and passing**.
- Integration tests (`tests/test_integration.py`, real MongoDB 8.0.6 + uvicorn): **run and passing** (71 tests).
  Every test named in the table above has been run; see `README.md` for the full run.
