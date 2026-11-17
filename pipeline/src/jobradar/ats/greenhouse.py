"""Greenhouse job-board API: the URL to fetch and the shape we accept back.

Only fields with a `postings` column are modeled. `parse_board` pairs each parsed job
with its untouched JSON item, which becomes `ats_raw` (DATA_MODEL.md), so new
Greenhouse fields are stored without a code change. Required fields are the ones a
posting can't exist without; one job missing them fails the whole board, because
dropping just that job would count as a missed poll and eventually close a live
posting (D-007). Optional fields never fail the board.
"""

import html
from typing import Any

from pydantic import BaseModel, field_validator

BASE_URL = "https://boards-api.greenhouse.io/v1/boards"


def board_url(token: str) -> str:
    return f"{BASE_URL}/{token}/jobs?content=true"


class GreenhouseLocation(BaseModel):
    name: str


class GreenhouseDepartment(BaseModel):
    name: str


class GreenhouseJob(BaseModel):
    id: int
    title: str
    absolute_url: str
    content: str
    location: GreenhouseLocation | None = None
    departments: list[GreenhouseDepartment] = []

    @field_validator("content")
    @classmethod
    def _unescape_content(cls, value: str) -> str:
        # Greenhouse HTML-escapes the whole document once. Undo it exactly once, here,
        # so description_html is real HTML for every ATS and the cleaner stays generic.
        return html.unescape(value)

    @field_validator("departments", mode="before")
    @classmethod
    def _null_departments_are_empty(cls, value: Any) -> Any:
        # "before" runs on the raw JSON, so a null is rescued before the list type check.
        return [] if value is None else value


class GreenhouseBoard(BaseModel):
    jobs: list[GreenhouseJob]


def parse_board(body: Any) -> list[tuple[GreenhouseJob, dict[str, Any]]]:
    """Validate a board response; return each job paired with its raw JSON item.

    Raises pydantic.ValidationError if the board is malformed (see module docstring).
    """
    board = GreenhouseBoard.model_validate(body)
    # Validation preserves list order, so job i came from item i.
    raw_items: list[dict[str, Any]] = body["jobs"]
    return list(zip(board.jobs, raw_items, strict=True))
