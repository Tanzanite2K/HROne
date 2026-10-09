"""sample_seed.py must never wipe a real database by accident. No database needed: the guard runs before connecting."""
import pytest

from sample_seed import check_target, confirm, is_local

ATLAS = "mongodb+srv://fakeuser:fakepass@cluster.example.invalid/"  # obviously fake, not a real host


@pytest.mark.parametrize("uri, local", [
    ("mongodb://localhost:27017", True), ("mongodb://127.0.0.1:27017/", True), ("mongodb://u:p@localhost", True),
    (ATLAS, False), ("mongodb://db.example.com:27017", False), ("mongodb://localhost,db.example.com", False),
])
def test_is_local(uri, local):
    assert is_local(uri) is local


def test_remote_refused_by_default():
    with pytest.raises(SystemExit, match="remote cluster") as exc:
        check_target(ATLAS, "attendance_db", db_given=False, allow_remote=False)
    assert "fakepass" not in str(exc.value)  # credentials are redacted in the message


@pytest.mark.parametrize("db, given", [("attendance_db", True), ("hrone_dev", False), ("attendance_db", False)])
def test_remote_needs_explicit_disposable_db(db, given):
    with pytest.raises(SystemExit, match="disposable"):
        check_target(ATLAS, db, db_given=given, allow_remote=True)


def test_remote_allowed_only_with_both_flags():
    check_target(ATLAS, "hrone_dev", db_given=True, allow_remote=True)  # no exception


def test_local_allowed():
    check_target("mongodb://localhost:27017", "attendance_db", db_given=False, allow_remote=False)


def test_bad_db_name_refused():
    with pytest.raises(SystemExit):
        check_target("mongodb://localhost:27017", "a/b", db_given=True, allow_remote=False)


def test_confirmation_requires_typing_the_name():
    assert confirm("hrone_dev", assume_yes=False, ask=lambda _: "hrone_dev") is True
    assert confirm("hrone_dev", assume_yes=False, ask=lambda _: "y") is False
    assert confirm("hrone_dev", assume_yes=True) is True
