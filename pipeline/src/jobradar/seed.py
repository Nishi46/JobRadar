"""Read the hand-edited seed list (pipeline/seed/companies.csv) into validated board configs.

The CSV owns board *configuration* only (D16). Observations such as last_polled_at
live in the boards table and are never derived from this file.
"""

import csv
import sys
from pathlib import Path
from typing import Any, Literal

import psycopg
from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

from jobradar.db import connect

SUPPORTED_ATS = ("greenhouse", "ashby", "lever")
SEED_CSV = Path(__file__).parents[2] / "seed" / "companies.csv"

# Only CSV-owned columns appear in DO UPDATE SET: observation columns (last_polled_at,
# was_empty_last_poll, ...) belong to the poller and must survive a reload (D16).
# The WHERE skips no-op updates, so updated_at only moves when the config really changed.
_UPSERT_BOARD = """
    INSERT INTO boards (ats, board_token, company, stage, in_skills_analysis, enabled)
    VALUES (%(ats)s, %(board_token)s, %(company)s, %(stage)s, %(in_skills_analysis)s, %(enabled)s)
    ON CONFLICT (ats, board_token) DO UPDATE SET
        company            = EXCLUDED.company,
        stage              = EXCLUDED.stage,
        in_skills_analysis = EXCLUDED.in_skills_analysis,
        enabled            = EXCLUDED.enabled,
        updated_at         = now()
    WHERE (boards.company, boards.stage, boards.in_skills_analysis, boards.enabled)
          IS DISTINCT FROM
          (EXCLUDED.company, EXCLUDED.stage, EXCLUDED.in_skills_analysis, EXCLUDED.enabled)
"""

# Boards removed from the CSV are disabled, never deleted (D15). The two arrays are
# zipped back into (ats, board_token) pairs by unnest.
_DISABLE_MISSING = """
    UPDATE boards SET enabled = false, updated_at = now()
    WHERE enabled
      AND (ats, board_token) NOT IN (SELECT * FROM unnest(%s::text[], %s::text[]))
"""


class BoardConfig(BaseModel):
    """One pollable board, as declared in the seed CSV."""

    model_config = ConfigDict(frozen=True, str_strip_whitespace=True)

    company: str
    ats: Literal["greenhouse", "ashby", "lever"]
    board_token: str
    stage: str | None
    in_skills_analysis: bool
    enabled: bool

    @field_validator("stage", mode="before")
    @classmethod
    def blank_stage_is_none(cls, value: str | None) -> str | None:
        return value.strip() or None if isinstance(value, str) else value


def parse_seed_csv(path: Path) -> list[BoardConfig]:
    """Return one BoardConfig per pollable row.

    Rows on an unsupported ATS, or with no board token, are skipped: there is nothing
    to poll. An unverified row is kept but disabled. Raises ValueError on bad values
    or a duplicate (ats, board_token), naming the CSV line.
    """
    boards: list[BoardConfig] = []
    seen: dict[tuple[str, str], int] = {}

    with path.open(newline="") as f:
        # Line 1 is the header, so the first data row is line 2.
        for line, row in enumerate(csv.DictReader(f), start=2):
            ats = row["ats"].strip()
            token = row["board_token"].strip()
            if ats not in SUPPORTED_ATS or not token:
                continue

            try:
                board = BoardConfig.model_validate(
                    {
                        "company": row["company"],
                        "ats": ats,
                        "board_token": token,
                        "stage": row["stage"],
                        "in_skills_analysis": row["in_skills_analysis"],
                        "enabled": row["verified"],
                    }
                )
            except ValidationError as e:
                raise ValueError(f"{path.name} line {line}: {e}") from e

            key = (board.ats, board.board_token)
            if key in seen:
                first = seen[key]
                raise ValueError(
                    f"{path.name} line {line}: duplicate board {key}, first seen on line {first}"
                )
            seen[key] = line
            boards.append(board)

    return boards


def sync_boards(conn: psycopg.Connection[Any], boards: list[BoardConfig]) -> int:
    """Make the boards table match the seed list, in one transaction.

    Returns the number of boards disabled because they are no longer in the list.
    Refuses an empty list: it would disable every board, and an empty seed list is far
    more likely a broken file than a real intent.
    """
    if not boards:
        raise ValueError("refusing to sync an empty board list: it would disable every board")

    with conn.transaction(), conn.cursor() as cur:
        cur.executemany(_UPSERT_BOARD, [b.model_dump() for b in boards])
        cur.execute(
            _DISABLE_MISSING,
            ([b.ats for b in boards], [b.board_token for b in boards]),
        )
        return cur.rowcount


def main() -> None:
    boards = parse_seed_csv(SEED_CSV)
    with connect() as conn:
        disabled = sync_boards(conn, boards)
    print(f"synced {len(boards)} boards from {SEED_CSV.name}; disabled {disabled}", file=sys.stderr)


if __name__ == "__main__":
    main()
