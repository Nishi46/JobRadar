"""Tests for turning postings, profiles and match candidates into prompt text.

All posting text here is synthetic.
"""

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from jobradar.render import (
    ats_hints_from_raw,
    header_paragraph,
    render_candidates,
    render_posting,
    render_profile,
    render_taxonomy,
)
from jobradar.taxonomy import SKILLS

FIXTURES = Path(__file__).parent / "fixtures"

PARAGRAPHS = [
    {"id": 1, "text": "About us: we build agents for insurers."},
    {"id": 2, "text": "- 5+ years building backend systems in Python"},
    {"id": 3, "text": "- Nice to have: experience with LangGraph"},
    {"id": 4, "text": "Base salary: $180,000 \u2013 $220,000 (New York, NY)"},
    {"id": 5, "text": "We offer great snacks."},
]


# --- taxonomy ----------------------------------------------------------------------------


def test_render_taxonomy_has_one_line_per_code_in_order() -> None:
    lines = render_taxonomy().splitlines()
    assert len(lines) == len(SKILLS)
    assert lines[0] == f"LLM_PROD — {SKILLS['LLM_PROD']}"
    assert "PY — Python" in lines


# --- posting -----------------------------------------------------------------------------

EM = "\u2014"


def test_header_paragraph_is_one_citable_line_with_id_0() -> None:
    header = header_paragraph(
        company="Acme AI",
        title="Senior AI Engineer",
        locations=["San Francisco, CA", "New York, NY"],
        departments=["Engineering", "Applied AI"],
        ats_hints={"workplace type": "Hybrid", "employment type": "FullTime"},
    )
    assert header == {
        "id": 0,
        "text": f"Acme AI {EM} Senior AI Engineer | Locations: New York, NY; San Francisco, CA"
        " | Departments: Engineering; Applied AI"
        " | workplace type: Hybrid | employment type: FullTime",
    }
    assert "\n" not in header["text"]


def test_header_paragraph_marks_missing_lists_and_omits_empty_hints() -> None:
    header = header_paragraph(company="C", title="T", locations=[], departments=[], ats_hints={})
    assert header["text"] == f"C {EM} T | Locations: (none listed) | Departments: (none listed)"
    assert header_paragraph(company="C", title="T", locations=[], departments=[]) == header


def test_header_paragraph_sorts_and_normalizes_locations_like_the_hash() -> None:
    # D-011: the hash sorts whitespace-normalized locations and drops empty ones, so the
    # model must see that same order (two inputs with one hash must render identically).
    a = header_paragraph(
        company="C", title="T", locations=["Remote  - US", "Boston, MA", " "], departments=[]
    )
    b = header_paragraph(
        company="C", title="T", locations=["Boston, MA", "Remote - US"], departments=[]
    )
    assert a == b
    assert "Locations: Boston, MA; Remote - US" in a["text"]


def test_render_posting_is_header_as_paragraph_0_then_numbered_paragraphs() -> None:
    text = render_posting(
        company="Acme AI",
        title="Senior AI Engineer",
        locations=["San Francisco, CA", "New York, NY"],
        departments=["Engineering"],
        paragraphs=PARAGRAPHS[:2],
        ats_hints={"workplace type": "Hybrid"},
    )
    assert text == (
        f"[0] Acme AI {EM} Senior AI Engineer | Locations: New York, NY; San Francisco, CA"
        " | Departments: Engineering | workplace type: Hybrid\n"
        "[1] About us: we build agents for insurers.\n"
        "[2] - 5+ years building backend systems in Python"
    )


def test_render_posting_header_matches_header_paragraph() -> None:
    # The [0] the model sees must be exactly the paragraph 0 validation checks against.
    args: dict[str, Any] = {
        "company": "C",
        "title": "T",
        "locations": ["Boston, MA"],
        "departments": ["Eng"],
        "ats_hints": {"remote": "yes"},
    }
    first_line = render_posting(paragraphs=PARAGRAPHS, **args).splitlines()[0]
    assert first_line == f"[0] {header_paragraph(**args)['text']}"


def test_render_posting_skips_a_caller_supplied_paragraph_0() -> None:
    stale = {"id": 0, "text": "an old header"}
    text = render_posting(
        company="C", title="T", locations=[], departments=[], paragraphs=[stale, *PARAGRAPHS[:1]]
    )
    assert "an old header" not in text
    assert [line[:3] for line in text.splitlines()] == ["[0]", "[1]"]


def test_render_posting_with_no_paragraphs_is_just_the_header() -> None:
    text = render_posting(company="C", title="T", locations=[], departments=[], paragraphs=[])
    assert text.splitlines() == [
        f"[0] C {EM} T | Locations: (none listed) | Departments: (none listed)"
    ]


# --- ATS hints ---------------------------------------------------------------------------


def test_ats_hints_from_ashby_fixture() -> None:
    raw = json.loads((FIXTURES / "ashby_board.json").read_text())["jobs"][0]
    assert ats_hints_from_raw("ashby", raw) == {
        "workplace type": "Hybrid",
        "remote": "yes",
        "employment type": "FullTime",
    }


def test_ats_hints_from_lever_fixture() -> None:
    raw = json.loads((FIXTURES / "lever_board.json").read_text())[0]
    assert ats_hints_from_raw("lever", raw) == {
        "workplace type": "hybrid",
        "employment type": "Full-time",
    }


def test_ats_hints_ashby_is_remote_false_and_missing_fields() -> None:
    assert ats_hints_from_raw("ashby", {"isRemote": False}) == {"remote": "no"}
    assert ats_hints_from_raw("ashby", {"workplaceType": None, "employmentType": ""}) == {}


def test_ats_hints_lever_tolerates_missing_or_odd_categories() -> None:
    assert ats_hints_from_raw("lever", {}) == {}
    assert ats_hints_from_raw("lever", {"categories": None, "workplaceType": "unspecified"}) == {
        "workplace type": "unspecified"
    }


def test_ats_hints_greenhouse_and_unknown_ats_are_empty() -> None:
    assert ats_hints_from_raw("greenhouse", {"workplaceType": "Remote"}) == {}
    assert ats_hints_from_raw("workday", {"workplaceType": "Remote"}) == {}


# --- profile -----------------------------------------------------------------------------


@dataclass
class FakeFacts:
    skills: list[str]
    tracks: list[str]
    seniority: str
    years_experience: int | None
    resume_text: str = "SECRET RESUME TEXT"  # must never reach the prompt


@dataclass
class FakeFilters:
    tracks: list[str] = field(default_factory=list)
    seniorities: list[str] = field(default_factory=list)
    locations: list[str] = field(default_factory=list)
    remote_ok: bool = True
    min_pay_usd: int | None = None


def test_render_profile_lists_facts_and_filters() -> None:
    text = render_profile(
        FakeFacts(skills=["PY", "EVALS"], tracks=["platform"], seniority="mid", years_experience=4),
        FakeFilters(locations=["New York, NY"], remote_ok=False, min_pay_usd=170000),
    )
    assert "Skills:\n- PY — Python\n- EVALS — " in text
    assert "Tracks: platform" in text
    assert "Seniority: mid" in text
    assert "Years of experience: 4" in text
    assert "Locations: New York, NY" in text
    assert "Remote OK: no" in text
    assert "Minimum pay: USD 170,000 per year" in text
    assert "SECRET" not in text


def test_render_profile_handles_empty_values() -> None:
    text = render_profile(
        FakeFacts(skills=["NOT_A_CODE"], tracks=[], seniority="unknown", years_experience=None),
        FakeFilters(),
    )
    assert "Skills:\n- NOT_A_CODE\n" in text  # unknown codes are shown bare, not dropped
    assert "Tracks: (none)" in text
    assert "Years of experience: unknown" in text
    assert "Track filter: any" in text
    assert "Minimum pay: none" in text
    assert "Remote OK: yes" in text


# --- candidates --------------------------------------------------------------------------


def facts(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "track": {"value": ["platform"], "evidence": [1]},
        "seniority": {"value": "senior", "evidence": [2]},
        "years_min": {"value": 5, "evidence": [2]},
        "salary": {
            "ranges": [
                {
                    "location": "New York, NY",
                    "min": 180000,
                    "max": 220000,
                    "currency": "USD",
                    "pay_type": "annual_base",
                }
            ],
            "evidence": 4,
        },
        "locations": {"value": ["New York, NY"], "evidence": [4]},
        "remote_policy": {"value": "hybrid", "evidence": []},
        "visa_sponsorship": {"value": "unstated", "evidence": []},
        "required_skills": [{"code": "PY", "evidence": 2}, {"code": "BACKEND", "evidence": 2}],
        "preferred_skills": [{"code": "FW", "evidence": 3}],
    }
    base.update(overrides)
    return base


def candidate(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "posting_id": 42,
        "company": "Acme AI",
        "title": "Senior AI Engineer",
        "display_salary": facts()["salary"]["ranges"],
        "facts": facts(),
        "paragraphs": PARAGRAPHS,
    }
    base.update(overrides)
    return base


def test_render_candidates_shows_facts_and_only_cited_paragraphs() -> None:
    text = render_candidates([candidate()])
    assert text.startswith("<candidate posting_id=42>\n")
    assert text.endswith("</candidate>")
    assert "Company: Acme AI" in text
    assert "Title: Senior AI Engineer" in text
    assert "Track: platform" in text
    assert "Seniority: senior" in text
    assert "Remote policy: hybrid" in text
    assert "Minimum years: 5" in text
    assert "Locations: New York, NY" in text
    assert "Pay: USD 180,000\u2013220,000 annual_base (New York, NY)" in text
    assert "Required skills: PY, BACKEND" in text
    assert "Preferred skills: FW" in text
    # Paragraphs 1-4 are cited (2 by several fields, but shown once, in id order); 5 is not.
    cited = [line for line in text.splitlines() if line.startswith("[")]
    assert cited == [f"[{p['id']}] {p['text']}" for p in PARAGRAPHS[:4]]


def test_render_candidates_handles_missing_pay_and_years() -> None:
    f = facts(
        years_min={"value": None, "evidence": []},
        salary={"ranges": [], "evidence": None},
        locations={"value": ["New York, NY"], "evidence": [1]},
    )
    text = render_candidates([candidate(facts=f, display_salary=[])])
    assert "Pay: not published" in text
    assert "Minimum years: unknown" in text
    assert "[4]" not in text  # nothing cites paragraph 4 any more


def test_render_candidates_ignores_cited_ids_that_do_not_exist() -> None:
    f = facts(track={"value": ["platform"], "evidence": [1, 99]})
    text = render_candidates([candidate(facts=f)])
    assert "[99]" not in text


def test_render_candidates_accepts_a_pydantic_model_for_facts() -> None:
    from jobradar.facts import PostingFacts

    model = PostingFacts.model_validate(facts())
    assert render_candidates([candidate(facts=model)]) == render_candidates([candidate()])


def test_render_candidates_separates_several_candidates() -> None:
    text = render_candidates([candidate(), candidate(posting_id=7)])
    assert "</candidate>\n\n<candidate posting_id=7>" in text


def test_render_candidates_empty_list() -> None:
    assert render_candidates([]) == "(no candidates)"
