import copy
import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from jobradar.ats.ashby import AshbyBoard, board_url, parse_board

# Made-up board: never commit a real posting (the content belongs to the employer).
FIXTURE = Path(__file__).parent / "fixtures" / "ashby_board.json"

FIRST_ID = "0b3f6c1e-1111-4a2b-9c3d-000000000001"
SECOND_ID = "0b3f6c1e-2222-4a2b-9c3d-000000000002"


@pytest.fixture
def body() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(FIXTURE.read_text())
    return data


def test_board_url_asks_for_compensation() -> None:
    assert board_url("acme") == (
        "https://api.ashbyhq.com/posting-api/job-board/acme?includeCompensation=true"
    )


def test_board_url_keeps_token_case() -> None:
    # Ashby board names are case-sensitive: lowercasing "AcmeRobotics" gives a 404.
    assert "/job-board/AcmeRobotics?" in board_url("AcmeRobotics")


def test_parse_board_maps_jobs_onto_posting_input(body: dict[str, Any]) -> None:
    postings = parse_board(body)

    assert [p.external_id for p in postings] == [FIRST_ID, SECOND_ID]
    first = postings[0]
    assert first.url == f"https://example.com/acme-robotics/{FIRST_ID}"
    assert first.title == "Applied AI Engineer"
    # Ashby sends real HTML, so it passes through unchanged (no unescape, unlike Greenhouse).
    assert (
        first.description_html == "<p>Build evals for R&amp;D robots.</p><ul><li>Python</li></ul>"
    )
    assert first.departments == ["Engineering", "Applied AI"]


def test_secondary_locations_follow_primary_deduped_in_order(body: dict[str, Any]) -> None:
    # The fixture repeats the primary location in secondaryLocations; it must appear once.
    assert parse_board(body)[0].locations_raw == ["San Francisco, CA", "New York, NY"]


def test_compensation_is_kept_untouched(body: dict[str, Any]) -> None:
    posting = parse_board(body)[0]

    assert posting.ats_compensation == body["jobs"][0]["compensation"]
    assert posting.ats_compensation is not None
    assert posting.ats_compensation["summaryComponents"][0]["minValue"] == 180000


def test_null_compensation_becomes_none(body: dict[str, Any]) -> None:
    assert parse_board(body)[1].ats_compensation is None


def test_absent_compensation_becomes_none(body: dict[str, Any]) -> None:
    del body["jobs"][1]["compensation"]

    assert parse_board(body)[1].ats_compensation is None


def test_missing_team_gives_department_only(body: dict[str, Any]) -> None:
    assert parse_board(body)[1].departments == ["Design"]


def test_team_equal_to_department_is_not_repeated(body: dict[str, Any]) -> None:
    body["jobs"][0]["team"] = "Engineering"

    assert parse_board(body)[0].departments == ["Engineering"]


@pytest.mark.parametrize(
    "field", ["location", "secondaryLocations", "department", "team", "compensation", "isListed"]
)
def test_null_optional_fields_do_not_fail_the_board(body: dict[str, Any], field: str) -> None:
    body["jobs"][0][field] = None

    assert len(parse_board(body)) == 2


def test_all_optional_fields_absent_gives_empty_lists(body: dict[str, Any]) -> None:
    for key in ["location", "secondaryLocations", "department", "team", "compensation"]:
        body["jobs"][0].pop(key, None)

    first = parse_board(body)[0]
    assert first.locations_raw == []
    assert first.departments == []
    assert first.ats_compensation is None


def test_empty_strings_are_dropped(body: dict[str, Any]) -> None:
    body["jobs"][0]["location"] = ""
    body["jobs"][0]["team"] = ""

    first = parse_board(body)[0]
    assert first.locations_raw == ["New York, NY", "San Francisco, CA"]  # secondaries only
    assert first.departments == ["Engineering"]


def test_unlisted_job_is_skipped(body: dict[str, Any]) -> None:
    ids = [p.external_id for p in parse_board(body)]

    assert "0b3f6c1e-3333-4a2b-9c3d-000000000003" not in ids


def test_unlisted_job_missing_required_field_still_fails_the_board(
    body: dict[str, Any],
) -> None:
    # The whole response is validated before filtering: a broken item means a broken
    # response, and we don't trust the rest of it either.
    del body["jobs"][2]["title"]

    with pytest.raises(ValidationError):
        parse_board(body)


@pytest.mark.parametrize("field", ["id", "title", "jobUrl", "descriptionHtml"])
def test_job_missing_a_required_field_fails_the_whole_board(
    body: dict[str, Any], field: str
) -> None:
    # Dropping just the bad job would look like "posting missing from the board" and
    # count toward closing it (D-007). Failing the board means the poll doesn't count.
    broken = copy.deepcopy(body)
    del broken["jobs"][1][field]

    with pytest.raises(ValidationError):
        AshbyBoard.model_validate(broken)


def test_empty_board_is_valid() -> None:
    assert parse_board({"apiVersion": "1", "jobs": []}) == []


def test_body_without_jobs_key_is_invalid() -> None:
    # {} is a broken response, not an empty board; it must not count as a poll (D-007).
    with pytest.raises(ValidationError):
        parse_board({})


def test_parse_board_keeps_the_full_raw_item(body: dict[str, Any]) -> None:
    raw = parse_board(body)[0].ats_raw

    assert raw == body["jobs"][0]  # the untouched item, for ats_raw
    # Fields the model ignores are kept, so new Ashby fields are stored without a code change.
    assert raw["workplaceType"] == "Hybrid"
    assert raw["descriptionPlain"].startswith("Build evals")
    assert raw["secondaryLocations"][0]["address"]["addressLocality"] == "New York"
