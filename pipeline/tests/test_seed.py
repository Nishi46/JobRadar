from pathlib import Path

import pytest

from jobradar.seed import BoardConfig, parse_seed_csv

HEADER = "company,ats,board_token,stage,in_skills_analysis,verified,jobs_at_check,checked_on,notes"
REAL_SEED = Path(__file__).parents[1] / "seed" / "companies.csv"


def write_csv(tmp_path: Path, *rows: str) -> Path:
    path = tmp_path / "companies.csv"
    path.write_text("\n".join([HEADER, *rows]) + "\n")
    return path


def test_verified_in_scope_row_becomes_enabled_board(tmp_path: Path) -> None:
    path = write_csv(tmp_path, "7AI,ashby,sevenai,A,true,true,46,2026-11-16,note")

    assert parse_seed_csv(path) == [
        BoardConfig(
            company="7AI",
            ats="ashby",
            board_token="sevenai",
            stage="A",
            in_skills_analysis=True,
            enabled=True,
        )
    ]


def test_unverified_in_scope_row_is_loaded_but_disabled(tmp_path: Path) -> None:
    path = write_csv(tmp_path, "Acme,greenhouse,acme,B,false,false,,2026-11-16,unconfirmed")

    [board] = parse_seed_csv(path)

    assert board.enabled is False


def test_out_of_scope_ats_is_skipped(tmp_path: Path) -> None:
    path = write_csv(
        tmp_path,
        "Adfin,custom,,A,false,false,,2026-11-16,custom site",
        "Foo,gem,,B,false,false,,2026-11-16,gem board",
    )

    assert parse_seed_csv(path) == []


def test_in_scope_row_without_token_is_skipped(tmp_path: Path) -> None:
    # Banco Plata in the real seed: Greenhouse-backed, but no token to poll.
    path = write_csv(tmp_path, "Banco Plata,greenhouse,,C,false,false,,2026-11-16,no token")

    assert parse_seed_csv(path) == []


def test_token_case_is_preserved_and_whitespace_stripped(tmp_path: Path) -> None:
    # Ashby tokens are case-sensitive; stray spaces are never part of a token.
    path = write_csv(tmp_path, " AfterQuery , ashby , AfterQuery ,B,false,true,42,2026-11-16,")

    [board] = parse_seed_csv(path)

    assert board.board_token == "AfterQuery"
    assert board.company == "AfterQuery"


def test_empty_stage_becomes_none(tmp_path: Path) -> None:
    path = write_csv(tmp_path, "Acme,lever,acme,,false,true,3,2026-11-16,")

    [board] = parse_seed_csv(path)

    assert board.stage is None


def test_bad_boolean_raises_with_line_number(tmp_path: Path) -> None:
    path = write_csv(tmp_path, "Acme,lever,acme,A,yes please,true,3,2026-11-16,")

    with pytest.raises(ValueError, match="line 2"):
        parse_seed_csv(path)


def test_duplicate_board_raises(tmp_path: Path) -> None:
    # Two rows for one (ats, board_token) would make the upsert's result depend on row order.
    path = write_csv(
        tmp_path,
        "Acme,ashby,acme,A,false,true,3,2026-11-16,",
        "Acme Again,ashby,acme,B,false,true,3,2026-11-16,",
    )

    with pytest.raises(ValueError, match="duplicate"):
        parse_seed_csv(path)


def test_real_seed_file_parses() -> None:
    boards = parse_seed_csv(REAL_SEED)

    assert boards, "the committed seed list should yield at least one board"
    assert all(b.ats in ("greenhouse", "ashby", "lever") for b in boards)
