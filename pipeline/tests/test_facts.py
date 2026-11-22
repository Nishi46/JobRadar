"""PostingFacts must parse the model's JSON and stay usable as a structured-output schema."""

from typing import Any

import pytest
from pydantic import ValidationError

from jobradar.facts import PostingFacts, SalaryField, SalaryRange, TrackField


def facts_json() -> dict[str, Any]:
    return {
        "track": {"value": ["platform", "product"], "evidence": [2]},
        "seniority": {"value": "senior", "evidence": [1]},
        "years_min": {"value": 5, "evidence": [3]},
        "salary": {
            "ranges": [
                {
                    "location": "San Francisco, CA",
                    "min": 180000,
                    "max": 220000,
                    "currency": "USD",
                    "pay_type": "annual_base",
                }
            ],
            "evidence": 4,
        },
        "locations": {"value": ["San Francisco, CA"], "evidence": [1]},
        "remote_policy": {"value": "hybrid", "evidence": [1]},
        "visa_sponsorship": {"value": "unstated", "evidence": []},
        "required_skills": [{"code": "PY", "evidence": 3}],
        "preferred_skills": [{"code": "RAG", "evidence": 3}],
    }


def test_parses_model_json() -> None:
    facts = PostingFacts.model_validate(facts_json())
    assert facts.track.value == ["platform", "product"]
    assert facts.salary.ranges[0].max == 220000
    assert facts.required_skills[0].code == "PY"


def test_round_trips_through_json() -> None:
    facts = PostingFacts.model_validate(facts_json())
    assert PostingFacts.model_validate_json(facts.model_dump_json()) == facts


def test_nulls_where_allowed() -> None:
    data = facts_json()
    data["years_min"] = {"value": None, "evidence": []}
    data["salary"] = {"ranges": [], "evidence": None}
    facts = PostingFacts.model_validate(data)
    assert facts.years_min.value is None
    assert facts.salary == SalaryField(ranges=[], evidence=None)


@pytest.mark.parametrize(
    ("path", "bad"),
    [
        (("track", "value"), ["backend"]),
        (("seniority", "value"), "principal"),
        (("remote_policy", "value"), "remote-first"),
        (("visa_sponsorship", "value"), "maybe"),
    ],
)
def test_rejects_values_outside_the_literals(path: tuple[str, str], bad: Any) -> None:
    data = facts_json()
    data[path[0]][path[1]] = bad
    with pytest.raises(ValidationError):
        PostingFacts.model_validate(data)


def test_rejects_unknown_pay_type() -> None:
    with pytest.raises(ValidationError):
        SalaryRange(location="", min=1, max=2, currency="USD", pay_type="monthly")  # type: ignore[arg-type]


def test_rejects_extra_keys() -> None:
    # Extra keys mean the model drifted from the schema; fail loudly instead of dropping them.
    with pytest.raises(ValidationError):
        TrackField.model_validate({"value": ["platform"], "evidence": [1], "confidence": 0.9})


# Keywords Anthropic structured outputs doesn't support; numeric/string constraints belong in
# validate.py so the schema stays portable across providers.
_UNSUPPORTED = {
    "minimum",
    "maximum",
    "exclusiveMinimum",
    "exclusiveMaximum",
    "pattern",
    "minLength",
    "maxLength",
    "minItems",
    "maxItems",
    "multipleOf",
    "format",
}


def _keys(node: Any) -> set[str]:
    if isinstance(node, dict):
        return set(node) | {k for v in node.values() for k in _keys(v)}
    if isinstance(node, list):
        return {k for v in node for k in _keys(v)}
    return set()


def test_schema_has_no_constraint_keywords() -> None:
    assert _keys(PostingFacts.model_json_schema()) & _UNSUPPORTED == set()


def test_schema_forbids_additional_properties_everywhere() -> None:
    schema = PostingFacts.model_json_schema()
    objects = [schema, *schema["$defs"].values()]
    assert all(o.get("additionalProperties") is False for o in objects if o.get("type") == "object")
