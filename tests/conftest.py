"""Test configuration.

SAFETY
* Tests never use your real database. Before app.main is imported, MONGO_DB is pointed at a fresh, uniquely named
  database `hrone_test_<random>`, and only that exact database is ever dropped (tests/test_integration.py).
* The server comes from TEST_MONGO_URI (default mongodb://localhost:27017), never from MONGO_URI or .env, so running
  the tests can never silently target the database your app is configured for.
* Connection strings are redacted (user:password removed) everywhere the tests print them.
"""
import os
import pathlib
import re
import sys
import uuid

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

TEST_MONGO_URI = os.environ.get("TEST_MONGO_URI", "mongodb://localhost:27017")
TEST_DB = f"hrone_test_{uuid.uuid4().hex[:10]}"
if not re.fullmatch(r"hrone_test_[0-9a-f]{10}", TEST_DB):  # belt and braces: nothing else is ever used or dropped
    raise RuntimeError(f"refusing to run against database {TEST_DB!r}")

# Must happen before `import app.main` anywhere, because the module reads them at import time.
os.environ["MONGO_URI"] = TEST_MONGO_URI
os.environ["MONGO_DB"] = TEST_DB


def redact(text: str) -> str:
    """Hide credentials in any text that may contain a MongoDB connection string."""
    return re.sub(r"(mongodb(?:\+srv)?://)[^@/\s]+@", r"\1***:***@", str(text))


def pytest_report_header(config):
    return [f"MongoDB server: {redact(TEST_MONGO_URI)}", f"test database (created and dropped by this run): {TEST_DB}"]
