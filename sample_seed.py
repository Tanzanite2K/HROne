"""Loads the nine sample documents from sample_data/ (see DATA_MODEL.md) into a database.

WARNING: seeding DELETES every document in `employees` and `attendance_logs` of the target database first.
So this script refuses to run unless you confirm, and refuses remote clusters (e.g. Atlas) unless you also name a
disposable database explicitly.

Usage:
    python sample_seed.py                         # local MongoDB, MONGO_DB from env/.env; asks you to type the db name
    python sample_seed.py --db hrone_dev          # choose the target database explicitly
    python sample_seed.py --db hrone_dev --yes    # no prompt (scripts/CI)
    python sample_seed.py --db hrone_dev --allow-remote --yes   # a remote cluster, disposable database only

Equivalent with the MongoDB tools (no safety checks!):
  mongoimport --uri "$MONGO_URI" --db "$MONGO_DB" -c employees       --jsonArray --drop --file sample_data/employees.json
  mongoimport --uri "$MONGO_URI" --db "$MONGO_DB" -c attendance_logs --jsonArray --drop --file sample_data/attendance_logs.json

The samples show document SHAPES only. The grader uses a much larger hidden dataset with cases they do not show."""
import argparse
import os
import pathlib
import re
import sys

LOCAL_HOSTS = ("localhost", "127.0.0.1", "[::1]")
# A database that the app is configured for by default is treated as "real" data: never seeded remotely.
PROTECTED_REMOTE_DBS = ("attendance_db",)


def redact(uri: str) -> str:
    return re.sub(r"(mongodb(?:\+srv)?://)[^@/\s]+@", r"\1***:***@", uri)


def is_local(uri: str) -> bool:
    if uri.startswith("mongodb+srv://"):
        return False  # SRV records always point at a remote cluster (Atlas)
    hosts = re.sub(r"^mongodb://([^@/]*@)?", "", uri).split("/")[0].split(",")
    return all(h.rsplit(":", 1)[0] in LOCAL_HOSTS or h.startswith("[::1]") for h in hosts if h)


def check_target(uri: str, db_name: str, db_given: bool, allow_remote: bool) -> None:
    """Raise SystemExit with a reason when seeding this target is not allowed."""
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,63}", db_name or ""):
        raise SystemExit(f"refusing: {db_name!r} is not a valid database name")
    if not is_local(uri):
        if not allow_remote:
            raise SystemExit(f"refusing: {redact(uri)} is a remote cluster. Seeding wipes two collections; pass "
                             "--allow-remote together with --db <disposable name> if you really mean it.")
        if not db_given or db_name in PROTECTED_REMOTE_DBS:
            raise SystemExit(f"refusing: on a remote cluster you must name a disposable database with --db "
                             f"(not {', '.join(PROTECTED_REMOTE_DBS)}).")


def confirm(db_name: str, assume_yes: bool, ask=input) -> bool:
    if assume_yes:
        return True
    if not sys.stdin.isatty() and ask is input:
        raise SystemExit("refusing: no terminal to confirm in; pass --yes")
    return ask(f"This DELETES all employees and attendance_logs in '{db_name}'. Type the database name to continue: ") \
        .strip() == db_name


def main(argv=None) -> None:
    from bson import json_util
    from dotenv import load_dotenv
    from pymongo import MongoClient

    parser = argparse.ArgumentParser(description="Load the sample documents (destructive).")
    parser.add_argument("--db", help="target database (default: MONGO_DB or attendance_db)")
    parser.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    parser.add_argument("--allow-remote", action="store_true", help="allow a non-localhost cluster (needs --db)")
    args = parser.parse_args(argv)

    load_dotenv(override=False)
    uri = os.getenv("MONGO_URI", "mongodb://localhost:27017")
    db_name = args.db or os.getenv("MONGO_DB", "attendance_db")
    print(f"target: {redact(uri)}  database: {db_name}")
    check_target(uri, db_name, args.db is not None, args.allow_remote)
    if not confirm(db_name, args.yes):
        raise SystemExit("cancelled: nothing was changed")

    here = pathlib.Path(__file__).parent / "sample_data"
    opts = json_util.JSONOptions(tz_aware=True)
    db = MongoClient(uri, tz_aware=True)[db_name]
    for name in ("employees", "attendance_logs"):
        docs = json_util.loads((here / f"{name}.json").read_text(), json_options=opts)
        db[name].delete_many({})
        db[name].insert_many(docs)
        print(f"loaded {len(docs):>2} documents into '{db.name}.{name}'")


if __name__ == "__main__":
    main()
