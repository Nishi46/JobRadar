import copy
import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from jobradar.ats.greenhouse import GreenhouseBoard, board_url, parse_board

# Made-up board: never commit a real posting (the content belongs to the employer).
FIXTURE = Path(__file__).parent / "fixtures" / "greenhouse_board.json"


@pytest.fixture
def body() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(FIXTURE.read_text())
    return data


def test_board_url_asks_for_content() -> None:
    assert board_url("acme") == "https://boards-api.greenhouse.io/v1/boards/acme/jobs?content=true"


def test_parses_the_fields_the_normalizer_needs(body: dict[str, Any]) -> None:
    job = GreenhouseBoard.model_validate(body).jobs[0]

    assert job.id == 1000001
    assert job.title == "Applied AI Engineer"
    assert job.absolute_url == "https://example.com/acme-robotics/jobs/1000001"
    assert job.location is not None
    assert job.location.name == "Remote - US"
    assert [d.name for d in job.departments] == ["Research"]


def test_content_is_unescaped_exactly_once(body: dict[str, Any]) -> None:
    # The source HTML contained the entity "&amp;" (for "R&D"). Greenhouse escapes the
    # whole document once more, so it arrives as "&amp;amp;". One unescape gives back the
    # real HTML; unescaping twice would corrupt it to a bare "&".
    job = GreenhouseBoard.model_validate(body).jobs[0]

    assert job.content == (
        "<div><p>Build evals for R&amp;D robots.</p><ul><li>Python</li></ul></div>"
    )


def test_missing_location_and_departments_do_not_fail_the_board(body: dict[str, Any]) -> None:
    job = GreenhouseBoard.model_validate(body).jobs[1]

    assert job.location is None
    assert job.departments == []


def test_location_key_absent_is_same_as_null(body: dict[str, Any]) -> None:
    del body["jobs"][1]["location"]

    assert GreenhouseBoard.model_validate(body).jobs[1].location is None


@pytest.mark.parametrize("field", ["id", "title", "absolute_url", "content"])
def test_job_missing_a_required_field_fails_the_whole_board(
    body: dict[str, Any], field: str
) -> None:
    # Dropping just the bad job would look like "posting missing from the board" and
    # count toward closing it (D-007). Failing the board means the poll doesn't count.
    broken = copy.deepcopy(body)
    del broken["jobs"][1][field]

    with pytest.raises(ValidationError):
        GreenhouseBoard.model_validate(broken)


def test_empty_board_is_valid() -> None:
    assert GreenhouseBoard.model_validate({"jobs": []}).jobs == []


def test_body_without_jobs_key_is_invalid() -> None:
    # {} is a broken response, not an empty board; it must not count as a poll (D-007).
    with pytest.raises(ValidationError):
        GreenhouseBoard.model_validate({})


def test_null_departments_become_empty_list(body: dict[str, Any]) -> None:
    # departments is optional: a null must not fail the whole board.
    body["jobs"][0]["departments"] = None

    assert GreenhouseBoard.model_validate(body).jobs[0].departments == []


def test_parse_board_pairs_each_job_with_its_full_raw_item(body: dict[str, Any]) -> None:
    pairs = parse_board(body)

    assert [job.id for job, _ in pairs] == [1000001, 1000002]
    job, raw = pairs[0]
    assert raw == body["jobs"][0]  # the untouched item, for ats_raw
    assert raw["content"].startswith("&lt;div&gt;")  # raw stays escaped; the model unescapes
    assert raw["departments"][0]["id"] == 31  # nested fields the model ignores are kept
    assert job.content.startswith("<div>")


def test_parse_board_rejects_a_broken_board() -> None:
    with pytest.raises(ValidationError):
        parse_board({})
