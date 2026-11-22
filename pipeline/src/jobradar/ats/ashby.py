"""Ashby public job-posting API: the URL to fetch and the shape we accept back.

Only fields with a `postings` column are modeled. `parse_board` maps each parsed job
onto the shared `PostingInput` and attaches its untouched JSON item as `ats_raw`
(DATA_MODEL.md), so new Ashby fields are stored without a code change. Required fields
are the ones a posting can't exist without; one job missing them fails the whole board,
because dropping just that job would count as a missed poll and eventually close a live
posting (D-007). Optional fields never fail the board, and Ashby documents that fields
empty in Ashby are left out of the response, so absent and null are treated the same.

Unlike Greenhouse, Ashby publishes structured pay on this endpoint (with
`includeCompensation=true`), and `descriptionHtml` is already real HTML.
"""

from typing import Any

from pydantic import BaseModel, field_validator

from jobradar.ats.base import PostingInput

BASE_URL = "https://api.ashbyhq.com/posting-api/job-board"


def board_url(token: str) -> str:
    # The token is the board name as it appears in the URL, and Ashby treats it
    # case-sensitively, so it is used exactly as stored (never lowercased).
    return f"{BASE_URL}/{token}?includeCompensation=true"


class AshbySecondaryLocation(BaseModel):
    location: str | None = None


class AshbyJob(BaseModel):
    # Ashby IDs are UUID strings, not ints like Greenhouse.
    id: str
    title: str
    # camelCase names match the API, so validation needs no alias layer.
    jobUrl: str
    descriptionHtml: str
    location: str | None = None
    secondaryLocations: list[AshbySecondaryLocation] = []
    department: str | None = None
    team: str | None = None
    # Kept as an opaque dict: it goes into ats_compensation untouched as M3 ground truth,
    # so modeling its nested tiers here would only add ways to fail the board.
    compensation: dict[str, Any] | None = None
    # None (absent or null) means "listed"; only an explicit false hides a job.
    isListed: bool | None = None

    @field_validator("secondaryLocations", mode="before")
    @classmethod
    def _null_secondary_locations_are_empty(cls, value: Any) -> Any:
        # "before" runs on the raw JSON, so a null is rescued before the list type check.
        return [] if value is None else value


class AshbyBoard(BaseModel):
    jobs: list[AshbyJob]


def _unique_non_empty(values: list[str | None]) -> list[str]:
    # Order matters (primary location first), so dict.fromkeys rather than a set.
    return list(dict.fromkeys(v for v in values if v))


def parse_board(body: Any) -> list[PostingInput]:
    """Validate a board response and map every listed job onto `PostingInput`.

    Raises pydantic.ValidationError if the board is malformed (see module docstring).
    The whole response is validated before unlisted jobs are filtered out, so a broken
    unlisted item still fails the board: a malformed item means we can't trust the rest.
    """
    board = AshbyBoard.model_validate(body)
    # Validation preserves list order, so job i came from item i.
    raw_items: list[dict[str, Any]] = body["jobs"]
    return [
        PostingInput(
            external_id=job.id,
            url=job.jobUrl,
            title=job.title,
            description_html=job.descriptionHtml,
            locations_raw=_unique_non_empty(
                [job.location, *(s.location for s in job.secondaryLocations)]
            ),
            # Ashby often sets team == department; dedupe so it isn't stored twice.
            departments=_unique_non_empty([job.department, job.team]),
            ats_compensation=job.compensation,
            ats_raw=raw,
        )
        for job, raw in zip(board.jobs, raw_items, strict=True)
        # An unlisted job is reachable only by direct link, not a public board posting.
        # Skipping it makes it count as missing from this poll, so a posting that gets
        # unlisted closes after 3 polls (D-007), which is the right outcome: we only
        # surface jobs the employer is advertising.
        if job.isListed is not False
    ]
