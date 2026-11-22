import copy
import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from jobradar.ats.lever import LEVER_BOARD, board_url, parse_board

# Made-up board: never commit a real posting (the content belongs to the employer).
FIXTURE = Path(__file__).parent / "fixtures" / "lever_board.json"


@pytest.fixture
def body() -> list[dict[str, Any]]:
    data: list[dict[str, Any]] = json.loads(FIXTURE.read_text())
    return data


def test_board_url_asks_for_json_and_keeps_token_case() -> None:
    # Tokens are stored exactly as the URL needs them (DATA_MODEL.md), so no lowercasing.
    assert board_url("SimbeRobotics") == "https://api.lever.co/v0/postings/SimbeRobotics?mode=json"


def test_parses_the_fields_the_normalizer_needs(body: list[dict[str, Any]]) -> None:
    posting = LEVER_BOARD.validate_python(body)[0]

    assert posting.id == "0a1b2c3d-0000-4000-8000-000000000001"
    assert posting.text == "Applied AI Engineer"
    assert posting.hostedUrl.startswith("https://jobs.example.com/acme-robotics/")
    assert posting.categories.team == "Perception"
    assert posting.lists[1].text == "Benefits & Perks"


def test_parse_board_maps_postings_onto_posting_input(body: list[dict[str, Any]]) -> None:
    postings = parse_board(body)

    assert [p.external_id for p in postings] == [
        "0a1b2c3d-0000-4000-8000-000000000001",
        "0a1b2c3d-0000-4000-8000-000000000002",
        "0a1b2c3d-0000-4000-8000-000000000003",
    ]
    first = postings[0]
    assert first.url == body[0]["hostedUrl"]  # the posting page, not applyUrl
    assert first.title == "Applied AI Engineer"
    assert first.locations_raw == ["San Francisco, CA", "Toronto, ON", "Remote - US"]
    assert first.departments == ["Engineering", "Perception"]


def test_description_is_one_document_in_page_order(body: list[dict[str, Any]]) -> None:
    # description, then each list under its header, then additional: the order a
    # reader sees on the hosted page, so the M2 extractor sees requirements and benefits.
    html_doc = parse_board(body)[0].description_html

    assert html_doc == (
        "<div>Acme Robotics builds warehouse robots.</div>"
        "<div><p>Build evals for our planner.</p></div>"
        "<h3>Requirements</h3><ul><li>Python</li><li>3+ years shipping ML</li></ul>"
        "<h3>Benefits &amp; Perks</h3><ul><li>Equity</li><li>Lunch</li></ul>"
        "<div>Acme is an equal opportunity employer.</div>"
    )


def test_list_header_is_escaped_because_it_is_plain_text(body: list[dict[str, Any]]) -> None:
    # A bare "&" or "<" in a header would otherwise become broken HTML in our document.
    body[0]["lists"][0]["text"] = "Skills <must-have>"

    html_doc = parse_board(body)[0].description_html

    assert "<h3>Skills &lt;must-have&gt;</h3>" in html_doc


def test_no_lists_and_empty_additional_leave_just_the_description(
    body: list[dict[str, Any]],
) -> None:
    assert parse_board(body)[1].description_html == "<p>Design the operator console.</p>"


def test_locations_fall_back_to_primary_when_all_locations_missing_or_empty(
    body: list[dict[str, Any]],
) -> None:
    postings = parse_board(body)

    assert postings[1].locations_raw == ["Toronto, ON"]  # allLocations key absent
    assert postings[2].locations_raw == ["Remote - US"]  # allLocations is []


def test_no_location_at_all_gives_no_locations(body: list[dict[str, Any]]) -> None:
    del body[1]["categories"]["location"]

    assert parse_board(body)[1].locations_raw == []


def test_departments_skip_missing_team_and_dedupe_in_order(body: list[dict[str, Any]]) -> None:
    postings = parse_board(body)

    assert postings[1].departments == ["Design"]  # no team
    assert postings[2].departments == ["Research"]  # team == department, kept once


def test_salary_range_is_stored_untouched(body: list[dict[str, Any]]) -> None:
    postings = parse_board(body)

    assert postings[0].ats_compensation == body[0]["salaryRange"]
    assert postings[1].ats_compensation is None  # key absent
    assert postings[2].ats_compensation is None  # explicit null


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("categories", None),
        ("lists", None),
        ("description", None),
        ("additional", None),
    ],
)
def test_null_optional_fields_do_not_fail_the_board(
    body: list[dict[str, Any]], field: str, value: Any
) -> None:
    body[0][field] = value

    assert len(parse_board(body)) == 3


def test_missing_categories_gives_no_locations_or_departments(
    body: list[dict[str, Any]],
) -> None:
    del body[0]["categories"]

    posting = parse_board(body)[0]
    assert posting.locations_raw == []
    assert posting.departments == []


def test_missing_description_still_keeps_lists(body: list[dict[str, Any]]) -> None:
    del body[0]["description"]

    assert parse_board(body)[0].description_html.startswith("<h3>Requirements</h3>")


@pytest.mark.parametrize("field", ["id", "text", "hostedUrl"])
def test_posting_missing_a_required_field_fails_the_whole_board(
    body: list[dict[str, Any]], field: str
) -> None:
    # Dropping just the bad posting would look like "posting missing from the board" and
    # count toward closing it (D-007). Failing the board means the poll doesn't count.
    broken = copy.deepcopy(body)
    del broken[1][field]

    with pytest.raises(ValidationError):
        parse_board(broken)


def test_empty_list_is_a_valid_empty_board() -> None:
    assert parse_board([]) == []


@pytest.mark.parametrize("broken", [{}, {"ok": False}, None, "oops"])
def test_non_list_body_is_invalid(broken: Any) -> None:
    # Lever answers with a bare list. Anything else is a broken response, not an empty
    # board, and must not count as a poll (D-007).
    with pytest.raises(ValidationError):
        parse_board(broken)


def test_parse_board_keeps_the_full_raw_item(body: list[dict[str, Any]]) -> None:
    raw = parse_board(body)[0].ats_raw

    assert raw == body[0]  # the untouched item, for ats_raw
    assert raw["workplaceType"] == "hybrid"  # unmodeled fields are kept
    assert raw["categories"]["commitment"] == "Full-time"  # nested ones too
    assert raw["lists"][1]["text"] == "Benefits & Perks"  # raw stays unescaped
