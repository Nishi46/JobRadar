"""Lever postings API: the URL to fetch and the shape we accept back.

Lever answers with a bare JSON list of postings, not an object wrapping one, so the
board is validated with a `TypeAdapter(list[LeverPosting])`. A body that is not a list
(`{}`, `{"ok": false}`) is a broken response, not an empty board.

Only fields with a `postings` column are modeled, and they keep Lever's own camelCase
names so a model field can be checked against the API docs at a glance. `parse_board`
attaches each untouched JSON item as `ats_raw` (DATA_MODEL.md), so new Lever fields are
stored without a code change.

Required fields (`id`, `text`, `hostedUrl`) are the ones a posting can't exist without;
one posting missing them fails the whole board (D-007). Everything else is optional and
defaults to empty, including the description: a posting whose body lives only in
`lists` is still a real posting, and the cleaner copes with an empty document.

Pagination: the endpoint accepts `skip`/`limit` but the docs give no default limit,
and a real 84-posting board came back whole without them, so we don't page.
"""

import html
from typing import Any

from pydantic import BaseModel, TypeAdapter, field_validator

from jobradar.ats.base import PostingInput

BASE_URL = "https://api.lever.co/v0/postings"


def board_url(token: str) -> str:
    # mode=json beats the Accept header in Lever's precedence rules, so it is explicit.
    return f"{BASE_URL}/{token}?mode=json"


def _none_to(default: Any, value: Any) -> Any:
    # Shared by the "before" validators below: they run on the raw JSON, so a null in
    # an optional field is rescued before the type check would fail the whole board.
    return default if value is None else value


class LeverCategories(BaseModel):
    location: str | None = None
    allLocations: list[str] = []
    department: str | None = None
    team: str | None = None

    @field_validator("allLocations", mode="before")
    @classmethod
    def _null_locations_are_empty(cls, value: Any) -> Any:
        return _none_to([], value)


class LeverList(BaseModel):
    text: str = ""  # the section header, plain text (e.g. "Benefits & Perks")
    content: str = ""  # HTML, usually <li> items but sometimes wrapped in <div>/<p>

    @field_validator("text", "content", mode="before")
    @classmethod
    def _null_text_is_empty(cls, value: Any) -> Any:
        return _none_to("", value)


class LeverPosting(BaseModel):
    id: str
    text: str
    hostedUrl: str
    description: str = ""  # HTML; already combines Lever's `opening` and `descriptionBody`
    lists: list[LeverList] = []
    additional: str = ""  # HTML closing section, often the EEO statement
    categories: LeverCategories = LeverCategories()
    salaryRange: dict[str, Any] | None = None  # kept as a dict: stored untouched (R7)

    @field_validator("description", "additional", mode="before")
    @classmethod
    def _null_html_is_empty(cls, value: Any) -> Any:
        return _none_to("", value)

    @field_validator("lists", mode="before")
    @classmethod
    def _null_lists_are_empty(cls, value: Any) -> Any:
        return _none_to([], value)

    @field_validator("categories", mode="before")
    @classmethod
    def _null_categories_are_empty(cls, value: Any) -> Any:
        return _none_to({}, value)


# The board is a top-level list, which a BaseModel can't describe; a TypeAdapter can.
LEVER_BOARD = TypeAdapter(list[LeverPosting])


def _compose_description(posting: LeverPosting) -> str:
    """Join Lever's split description back into the one document a reader sees.

    Lever stores requirements, responsibilities and benefits in `lists`, outside
    `description`. The M2 extractor needs exactly those sections (skills, seniority,
    perks), so `description_html` must hold all of it, in hosted-page order.
    """
    parts = [posting.description]
    for section in posting.lists:
        # The header is plain text; escape it so "R&D" or "<5 yrs" can't break the HTML.
        # The content is already HTML, so it goes in as-is.
        parts.append(f"<h3>{html.escape(section.text)}</h3><ul>{section.content}</ul>")
    parts.append(posting.additional)
    return "".join(parts)


def _locations(categories: LeverCategories) -> list[str]:
    # allLocations already includes the primary location, so it is used alone when
    # present; older or sparser postings only have `location`.
    if categories.allLocations:
        return categories.allLocations
    return [categories.location] if categories.location else []


def _departments(categories: LeverCategories) -> list[str]:
    # Lever has two grouping levels; keep both, but once each (many boards set
    # team == department) and in broad-to-narrow order.
    names = [categories.department, categories.team]
    return list(dict.fromkeys(name for name in names if name))


def parse_board(body: Any) -> list[PostingInput]:
    """Validate a board response and map every posting onto `PostingInput`.

    Raises pydantic.ValidationError if the board is malformed (see module docstring).
    """
    postings = LEVER_BOARD.validate_python(body)
    # Validation succeeded, so body is a list in the same order: posting i came from item i.
    raw_items: list[dict[str, Any]] = body
    return [
        PostingInput(
            external_id=posting.id,
            url=posting.hostedUrl,
            title=posting.text,
            description_html=_compose_description(posting),
            locations_raw=_locations(posting.categories),
            departments=_departments(posting.categories),
            ats_compensation=posting.salaryRange,
            ats_raw=raw,
        )
        for posting, raw in zip(postings, raw_items, strict=True)
    ]
