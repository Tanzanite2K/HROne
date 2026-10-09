# Employee Attendance & Analytics API (HROne assignment)

FastAPI + MongoDB. All application code is in `app/main.py`; it implements every operation in `openapi.yaml`
(rules R1-R10) exactly, plus a few clearly separated extras (below). `REVIEW.md` lists the starter's defects;
`DECISIONS.md` answers the five questions.

<!-- After creating the GitHub repo, replace OWNER/REPO to show the CI badge: -->
<!-- ![tests](https://github.com/OWNER/REPO/actions/workflows/tests.yml/badge.svg) -->

## Run
Requires Python 3.11+ and MongoDB **6.0+** (uses `$densify`, `$setWindowFields`, `$dateAdd`, and `$documents`
inside `$unionWith`; targets the grader's 7.0, tested so far on 8.0, see Known limitations).
```bash
python -m venv .venv && source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                                     # optional; real env vars win over .env (never commit .env)
python sample_seed.py --db hrone_dev                     # optional; see "Seeding" below
uvicorn app.main:app --port 8000
```
### Seeding (destructive, so guarded)
`sample_seed.py` deletes everything in `employees` and `attendance_logs` of the target database, then loads the nine
samples. It always asks you to type the database name (or `--yes`), and it refuses a remote cluster such as Atlas
unless you pass both `--allow-remote` and `--db <disposable name>`; it never seeds a remote `attendance_db`.
```bash
python sample_seed.py --db hrone_dev                      # local MongoDB
python sample_seed.py --db hrone_dev --allow-remote --yes # remote cluster, disposable database only
```
Then run the app with `MONGO_DB=hrone_dev` to see the samples.

### Configuration
| Variable | Default | Meaning |
|---|---|---|
| `MONGO_URI` | `mongodb://localhost:27017` | MongoDB connection string |
| `MONGO_DB` | `attendance_db` | Database holding `employees` and `attendance_logs` |

All indexes are created at startup (idempotent). If MongoDB is down at startup, the app still starts, `/health`
returns 503, and indexes are created on the first successful `/health`.

Swagger UI: http://localhost:8000/docs. Example:
```bash
curl -X POST localhost:8000/employees -H 'content-type: application/json' \
  -d '{"emp_code":"EMP0101","name":"Asha","email":"asha@example.com","department":"Engineering","joined_on":"2026-01-05"}'
curl -X POST localhost:8000/attendance/punch-in -H 'content-type: application/json' \
  -d '{"emp_code":"EMP0101","punched_at":1783312500000}'
curl 'localhost:8000/analytics/leaderboard/late?month=2026-07&limit=5'
curl 'localhost:8000/admin/explain/department_trend?department=Engineering&from=2026-07-01&to=2026-07-31'
```

## Tests
```bash
pip install -r requirements-dev.txt
pytest tests/test_rules.py tests/test_validation.py           # no database needed
TEST_MONGO_URI=mongodb://localhost:27017 pytest tests/test_integration.py -v   # real MongoDB + uvicorn
```
Integration tests start `uvicorn app.main:app` themselves and use a fresh database named `hrone_test_<random>`,
which they drop afterwards. They never read `MONGO_URI`/`.env`, so your real data is never touched. If no MongoDB
answers at `TEST_MONGO_URI`, they are reported as skipped.

### Latest verified run
On Windows 11, Python 3.11, **MongoDB 8.0.6** (local service), 2026-10-09: `pytest tests` -> **233 passed**
(44 rule unit tests, 104 validation/contract tests, 14 seed-safety tests, 71 integration tests against the real
database, including concurrent punch-in/punch-out/create/PATCH races and explain checks). The grader runs MongoDB 7.0; every stage used
exists since 6.0, but 7.0 itself was not run here. The 219 tests that existed then also passed against MongoDB Atlas (8.0.34,
free tier) using a throwaway `hrone_test_<random>` database.

`python -m tests.perf_check` (100,000 records, 400 employees, throwaway DB) -> every explain plan IXSCAN with no
COLLSCAN; median latency: list 6-58 ms, monthly 3 ms, leaderboard 95 ms, 92-day trend 64 ms, department summary
243 ms.

## Extras beyond the contract
Additive only: no contract path, field or status code changes, and `tests/test_validation.py` checks that the app's
contract operations still match `openapi.yaml` one for one.

| Extra | What it does |
|---|---|
| CI (`.github/workflows/tests.yml`) | On every push, runs the full suite against MongoDB **7.0** and **8.0** containers; integration tests fail (not skip) if the database is missing |
| Request id + timing | Every response has `X-Request-ID` (your own id is echoed if it is safe, else a new one) and `X-Response-Time-ms`; one log line per request (`hrone.access` logger) |
| `GET /attendance/{emp_code}/{date}/history` | Just the audit trail of one record, epoch-ms instants; same 404/422 rules as the PATCH |
| `GET /analytics/employees/{emp_code}/streaks?month=YYYY-MM` | Longest run of consecutive on-time working days (present, `late_minutes` 0; weekends skipped; a missing, late, ABSENT or LEAVE weekday breaks it), via a gaps-and-islands `$setWindowFields` pipeline |
| `GET /dashboard` | Read-only HTML page (summary table, leaderboard, trend chart) over the analytics endpoints; no external scripts, hidden from the OpenAPI schema |

## Known limitations
- **MongoDB 7.0 compatibility has NOT been tested yet.** The CI workflow runs the suite on 7.0, but only after the
  first push to GitHub; until that run is green this stays unverified. The grader uses 7.0; all runs so far used MongoDB 8.0
  (local 8.0.6 and Atlas 8.0.34). Every stage the code uses (`$lookup` with `localField` + `pipeline`,
  `$setWindowFields`/`$rank`, `$densify`, `$dateAdd`, `$documents` in `$unionWith`) is documented for 6.0+, so no
  difference is expected, but it is unverified. To check: `docker run -p 27017:27017 mongo:7`, then
  `TEST_MONGO_URI=mongodb://localhost:27017 pytest tests`.
- The 100,000-record performance check ran only on a local MongoDB 8.0, not on Atlas.

## Interpretations of ambiguous points
- **Trend range "exceeds 92 days"**: counted inclusively (`from`..`to` may hold at most 92 rows, i.e. a full quarter).
- **`moving_avg_7d`** is the mean of the reported (already 4-decimal) `attendance_rate` values in the window.
- **No-op PATCH**: 422 when no editable field is sent, or when the sent `status`/`punch_in`/`punch_out` leave the
  record unchanged. Derived fields sent by a client are ignored (the global convention), and an explicit `null` for
  `status`/`punch_in`/`punch_out` is 422 (those schemas are not nullable).
- **PATCH `punch_in` must stay on the record's date** is checked when `punch_in` is supplied; the final record is
  always re-validated (e.g. a new `punch_in` after the stored `punch_out` is 422).
- **Punch-in on a date that already has a record** (including a seeded `ABSENT`/`LEAVE` one) is 409.
- Rounding: R4/R8 half-up is done with exact decimals: Python `Decimal` for work hours, and `Decimal128` inside the
  pipelines (MongoDB's `$round` rounds half to even).
