"""The one shape every ATS adapter hands to the rest of the pipeline.

Each ATS module (greenhouse, ashby, lever) exposes two functions:

    board_url(token: str) -> str
    parse_board(body: Any) -> list[PostingInput]

`parse_board` validates the whole response with Pydantic and maps each job onto
`PostingInput`. Everything downstream (the cleaner, the hash, the upsert) only ever
sees `PostingInput`, so adding a fourth ATS means writing one module and nothing else.

Failure rule shared by all adapters (D-007): if one job is missing a field a posting
cannot exist without, `parse_board` raises `pydantic.ValidationError` for the *whole
board*. Dropping just that job would look like "posting missing from the board" and
count toward closing a live posting. Optional fields never fail the board.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class PostingInput:
    """One posting as the ATS gave it, before cleaning. Maps onto `postings` columns."""

    external_id: str  # the ATS's own posting ID, as a string for every ATS
    url: str  # public link to the original posting (we always link back, never republish)
    title: str
    description_html: str  # real HTML: any ATS-specific escaping is already undone
    locations_raw: list[str] = field(default_factory=list)  # as given; normalized in M2
    departments: list[str] = field(default_factory=list)  # stored, not hashed (D-006)
    ats_compensation: dict[str, Any] | None = None  # structured pay, untouched (M3 labels)
    ats_raw: dict[str, Any] = field(default_factory=dict)  # the full untouched JSON item


@dataclass(frozen=True)
class AtsAdapter:
    """The two functions an ATS module provides, bundled so the worker can look them up."""

    board_url: Callable[[str], str]
    parse_board: Callable[[Any], list[PostingInput]]
