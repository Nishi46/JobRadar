from typing import Any

import psycopg
import pytest

from jobradar.seed import BoardConfig, sync_boards


def board(token: str, **overrides: Any) -> BoardConfig:
    fields: dict[str, Any] = {
        "company": token.title(),
        "ats": "ashby",
        "board_token": token,
        "stage": "A",
        "in_skills_analysis": False,
        "enabled": True,
    }
    return BoardConfig(**(fields | overrides))


def fetch(conn: psycopg.Connection[Any], token: str) -> tuple[Any, ...]:
    row = conn.execute(
        "SELECT company, stage, enabled, last_job_count, updated_at FROM boards "
        "WHERE ats = 'ashby' AND board_token = %s",
        (token,),
    ).fetchone()
    assert row is not None, f"board {token} not found"
    return tuple(row)


@pytest.fixture(autouse=True)
def empty_boards(db: psycopg.Connection[Any]) -> None:
    # Start every test from an empty table; the db fixture rolls this back afterwards.
    db.execute("DELETE FROM boards")


def test_inserts_new_boards(db: psycopg.Connection[Any]) -> None:
    sync_boards(db, [board("alpha"), board("beta")])

    count = db.execute("SELECT count(*) FROM boards").fetchone()
    assert count == (2,)


def test_rerun_changes_nothing(db: psycopg.Connection[Any]) -> None:
    boards = [board("alpha"), board("beta")]
    sync_boards(db, boards)
    before = fetch(db, "alpha")

    disabled = sync_boards(db, boards)

    assert disabled == 0
    assert fetch(db, "alpha") == before  # including updated_at: a no-op is not an update
    assert db.execute("SELECT count(*) FROM boards").fetchone() == (2,)


def test_config_change_updates_config_but_keeps_observations(
    db: psycopg.Connection[Any],
) -> None:
    sync_boards(db, [board("alpha")])
    db.execute("UPDATE boards SET last_job_count = 46 WHERE board_token = 'alpha'")

    sync_boards(db, [board("alpha", stage="B", company="Alpha AI")])

    company, stage, _, last_job_count, _ = fetch(db, "alpha")
    assert (company, stage) == ("Alpha AI", "B")
    assert last_job_count == 46  # D16: the load step never touches observation columns


def test_board_missing_from_list_is_disabled_not_deleted(
    db: psycopg.Connection[Any],
) -> None:
    sync_boards(db, [board("alpha"), board("beta")])

    disabled = sync_boards(db, [board("alpha")])

    assert disabled == 1
    assert fetch(db, "beta")[2] is False  # D15: still there, just disabled
    assert fetch(db, "alpha")[2] is True


def test_same_token_on_another_ats_is_a_different_board(
    db: psycopg.Connection[Any],
) -> None:
    sync_boards(db, [board("alpha"), board("alpha", ats="greenhouse")])

    assert db.execute("SELECT count(*) FROM boards").fetchone() == (2,)


def test_empty_list_is_refused(db: psycopg.Connection[Any]) -> None:
    sync_boards(db, [board("alpha")])

    with pytest.raises(ValueError, match="empty"):
        sync_boards(db, [])

    assert fetch(db, "alpha")[2] is True
