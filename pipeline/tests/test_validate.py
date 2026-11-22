"""Every validation rule, the salary number matcher, ATS normalization and the pay decision.

All posting text here is synthetic.
"""

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from jobradar.facts import PostingFacts, SalaryRange
from jobradar.validate import (
    Issue,
    amounts_in_text,
    ats_salary_ranges,
    decide_pay,
    validate_facts,
)

FIXTURES = Path(__file__).parent / "fixtures"

PARAGRAPHS: list[dict[str, Any]] = [
    {"id": 1, "text": "Senior AI Engineer, hybrid in San Francisco or New York."},
    {"id": 2, "text": "You'll build our agent platform and ship product features."},
    {"id": 3, "text": "- 5+ years of Python. Nice to have: RAG experience."},
    {"id": 4, "text": "Base salary: $180,000 \u2013 $220,000 (SF); $170K \u2013 $210K (NYC)."},
    {"id": 5, "text": "We sponsor visas."},
]


def base() -> dict[str, Any]:
    """A fully valid PostingFacts payload over PARAGRAPHS; tests mutate a copy."""
    return {
        "track": {"value": ["platform", "product"], "evidence": [2]},
        "seniority": {"value": "senior", "evidence": [1]},
        "years_min": {"value": 5, "evidence": [3]},
        "salary": {
            "ranges": [
                _range("San Francisco, CA", 180000, 220000),
                _range("New York, NY", 170000, 210000),
            ],
            "evidence": 4,
        },
        "locations": {"value": ["San Francisco, CA", "New York, NY"], "evidence": [1]},
        "remote_policy": {"value": "hybrid", "evidence": [1]},
        "visa_sponsorship": {"value": "yes", "evidence": [5]},
        "required_skills": [{"code": "PY", "evidence": 3}, {"code": "AGENTS", "evidence": 2}],
        "preferred_skills": [{"code": "RAG", "evidence": 3}],
    }


def _range(
    location: str, lo: int, hi: int, currency: str = "USD", pay_type: str = "annual_base"
) -> dict[str, Any]:
    return {"location": location, "min": lo, "max": hi, "currency": currency, "pay_type": pay_type}


def check(data: dict[str, Any], paragraphs: list[dict[str, Any]] = PARAGRAPHS) -> list[Issue]:
    return validate_facts(PostingFacts.model_validate(data), paragraphs)


def fields(issues: list[Issue]) -> list[str]:
    return [i.field for i in issues]


def test_valid_facts_have_no_issues() -> None:
    assert check(base()) == []


def test_issue_messages_are_nonempty_sentences() -> None:
    data = base()
    data["track"]["evidence"] = [99]
    [issue] = check(data)
    assert issue.field == "track.evidence[0]"
    assert "99" in issue.message


# --- evidence ids --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("mutate", "field"),
    [
        (lambda d: d["seniority"].update(evidence=[1, 42]), "seniority.evidence[1]"),
        (lambda d: d["years_min"].update(evidence=[0]), "years_min.evidence[0]"),
        (lambda d: d["locations"].update(evidence=[6]), "locations.evidence[0]"),
        (lambda d: d["remote_policy"].update(evidence=[-1]), "remote_policy.evidence[0]"),
        (lambda d: d["visa_sponsorship"].update(evidence=[7]), "visa_sponsorship.evidence[0]"),
        (lambda d: d["salary"].update(evidence=9), "salary.evidence"),
        (lambda d: d["required_skills"][1].update(evidence=8), "required_skills[1].evidence"),
        (lambda d: d["preferred_skills"][0].update(evidence=8), "preferred_skills[0].evidence"),
    ],
)
def test_unknown_evidence_ids_are_flagged(mutate: Any, field: str) -> None:
    data = base()
    mutate(data)
    assert field in fields(check(data))


def test_cited_id_that_doesnt_exist_doesnt_also_trigger_text_checks() -> None:
    # One clear issue is better repair-prompt material than a cascade of follow-ons.
    data = base()
    data["salary"]["evidence"] = 9
    assert fields(check(data)) == ["salary.evidence"]


# --- evidence required unless the value abstains -------------------------------------------


@pytest.mark.parametrize(
    "field",
    ["track", "seniority", "years_min", "locations", "remote_policy", "visa_sponsorship"],
)
def test_evidence_required_for_known_values(field: str) -> None:
    data = base()
    data[field]["evidence"] = []
    assert f"{field}.evidence" in fields(check(data))


@pytest.mark.parametrize(
    ("field", "abstain"),
    [
        ("track", ["unknown"]),
        ("seniority", "unknown"),
        ("years_min", None),
        ("remote_policy", "unknown"),
        ("visa_sponsorship", "unstated"),
    ],
)
def test_abstaining_needs_no_evidence(field: str, abstain: Any) -> None:
    data = base()
    data[field] = {"value": abstain, "evidence": []}
    assert check(data) == []


def test_empty_locations_need_no_evidence() -> None:
    data = base()
    data["locations"] = {"value": [], "evidence": []}
    data["salary"] = {"ranges": [], "evidence": None}  # ranges would have nothing to match
    assert check(data) == []


def test_salary_ranges_need_a_citation() -> None:
    data = base()
    data["salary"]["evidence"] = None
    assert fields(check(data)) == ["salary.evidence"]


def test_no_salary_stated_is_valid() -> None:
    data = base()
    data["salary"] = {"ranges": [], "evidence": None}
    assert check(data) == []


# --- track ---------------------------------------------------------------------------------


def test_track_must_not_be_empty() -> None:
    data = base()
    data["track"] = {"value": [], "evidence": []}
    assert fields(check(data)) == ["track.value"]


def test_unknown_track_cannot_be_combined() -> None:
    data = base()
    data["track"]["value"] = ["platform", "unknown"]
    assert fields(check(data)) == ["track.value"]


# --- skills --------------------------------------------------------------------------------


def test_skill_codes_must_be_in_taxonomy() -> None:
    data = base()
    data["required_skills"][1]["code"] = "LANGCHAIN"
    data["preferred_skills"][0]["code"] = "py"  # codes are case-sensitive
    assert fields(check(data)) == ["required_skills[1].code", "preferred_skills[0].code"]


def test_duplicate_codes_within_a_list() -> None:
    data = base()
    data["required_skills"].append({"code": "PY", "evidence": 3})
    assert fields(check(data)) == ["required_skills[2].code"]


def test_code_cannot_be_both_required_and_preferred() -> None:
    data = base()
    data["preferred_skills"].append({"code": "PY", "evidence": 3})
    assert fields(check(data)) == ["preferred_skills[1].code"]


# --- years_min -----------------------------------------------------------------------------


def test_years_number_must_appear_in_a_cited_paragraph() -> None:
    data = base()
    data["years_min"] = {"value": 7, "evidence": [3]}
    assert fields(check(data)) == ["years_min.value"]


def test_years_number_found_in_any_cited_paragraph() -> None:
    data = base()
    data["years_min"]["evidence"] = [1, 3]
    assert check(data) == []


def test_years_number_must_be_a_whole_number_in_the_text() -> None:
    # "5" must not match inside "15" or "$150".
    paragraphs = [*PARAGRAPHS[:2], {"id": 3, "text": "15+ years of Python, $150 stipend."}]
    paragraphs += PARAGRAPHS[3:]
    data = base()
    assert fields(check(data, paragraphs)) == ["years_min.value"]


@pytest.mark.parametrize("text", ["3-10+ years", "3\u201310+ years", "five or more years"])
def test_years_number_forms(text: str) -> None:
    value = 5 if "five" in text else 3
    paragraphs = [*PARAGRAPHS[:2], {"id": 3, "text": text}, *PARAGRAPHS[3:]]
    data = base()
    data["years_min"]["value"] = value
    data["required_skills"] = []
    data["preferred_skills"] = []
    assert check(data, paragraphs) == []


# --- salary --------------------------------------------------------------------------------


def test_min_must_not_exceed_max() -> None:
    data = base()
    data["salary"]["ranges"][0] = _range("San Francisco, CA", 220000, 180000)
    assert "salary.ranges[0].min" in fields(check(data))


@pytest.mark.parametrize(("lo", "hi", "field"), [(0, 220000, "min"), (180000, -5, "max")])
def test_amounts_must_be_positive(lo: int, hi: int, field: str) -> None:
    data = base()
    data["salary"]["ranges"][0] = _range("San Francisco, CA", lo, hi)
    assert f"salary.ranges[0].{field}" in fields(check(data))


@pytest.mark.parametrize("currency", ["usd", "US", "US$", "DOLLARS", ""])
def test_currency_must_be_three_uppercase_letters(currency: str) -> None:
    data = base()
    data["salary"]["ranges"][0]["currency"] = currency
    assert fields(check(data)) == ["salary.ranges[0].currency"]


def test_every_number_must_appear_in_the_cited_paragraph() -> None:
    data = base()
    data["salary"]["ranges"][1] = _range("New York, NY", 175000, 210000)  # invented min
    assert fields(check(data)) == ["salary.ranges[1].min"]


def test_numbers_elsewhere_in_the_posting_dont_count() -> None:
    # The citation is what makes the number checkable: 180000 sits in paragraph 4, not 5.
    data = base()
    data["salary"]["evidence"] = 5
    assert fields(check(data)) == [
        "salary.ranges[0].min",
        "salary.ranges[0].max",
        "salary.ranges[1].min",
        "salary.ranges[1].max",
    ]


def test_invented_salary_is_blocked() -> None:
    # The DESIGN §6 "invented salary" guarantee: a range with no number in the text fails.
    paragraphs = [*PARAGRAPHS[:3], {"id": 4, "text": "Competitive salary and equity."}]
    paragraphs.append(PARAGRAPHS[4])
    assert "salary.ranges[0].min" in fields(check(base(), paragraphs))


def test_range_location_must_be_one_of_the_locations() -> None:
    data = base()
    data["salary"]["ranges"][1]["location"] = "Seattle, WA"
    assert fields(check(data)) == ["salary.ranges[1].location"]


def test_range_location_match_ignores_case_and_spacing() -> None:
    data = base()
    data["salary"]["ranges"][1]["location"] = "  new york,   NY "
    assert check(data) == []


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("$180,000 \u2013 $220,000", {180000, 220000}),
        ("180,000-220,000 USD", {180000, 220000}),
        ("$180K \u2013 $220K", {180000, 220000}),
        ("180k to 220k", {180000, 220000}),
        ("$180.5K", {180500}),
        ("180 000 \u2013 220 000 €", {180000, 220000}),
        ("180\u00a0000 \u2013 220\u202f000", {180000, 220000}),
        ("€180.000 \u2013 €220.000", {180000, 220000}),
        ("$85/hr", {85}),
        ("$85 - $110 per hour", {85, 110}),
        ("$1.2M OTE", {1200000}),
        ("$200,000 to $225,000, based on experience.", {200000, 225000}),
    ],
)
def test_amounts_in_text(text: str, expected: set[int]) -> None:
    assert expected <= amounts_in_text(text)


def test_shared_suffix_applies_to_both_ends_of_a_range() -> None:
    # "$150-200K" means 150,000-200,000; the bare 150 is kept too (it might be hourly).
    assert {150, 150000, 200000} <= amounts_in_text("$150-200K base")
    assert {150000, 200000} <= amounts_in_text("$150 \u2013 $200k")


def test_fractional_amounts_accept_either_rounding() -> None:
    # SalaryRange holds ints, so "$85.50/hr" may come back as 85 or 86.
    assert {85, 86} <= amounts_in_text("$85.50/hr")


def test_amounts_dont_split_or_glue_numbers() -> None:
    found = amounts_in_text("Series B, $1,250,000 raised; 401k match")
    assert 1250000 in found
    assert 250000 not in found
    assert 250 not in found


def test_hourly_range_validates() -> None:
    paragraphs = [*PARAGRAPHS[:3], {"id": 4, "text": "Contract rate: $85/hr - $110/hr."}]
    paragraphs.append(PARAGRAPHS[4])
    data = base()
    data["salary"]["ranges"] = [_range("San Francisco, CA", 85, 110, pay_type="hourly")]
    assert check(data, paragraphs) == []


def test_multiple_issues_are_all_reported() -> None:
    data = base()
    data["track"]["value"] = []
    data["required_skills"][0]["code"] = "NOPE"
    data["salary"]["ranges"][0]["currency"] = "usd"
    assert set(fields(check(data))) == {
        "track.value",
        "required_skills[0].code",
        "salary.ranges[0].currency",
    }


def test_does_not_mutate_inputs() -> None:
    data = base()
    facts = PostingFacts.model_validate(data)
    before = facts.model_copy(deep=True)
    paragraphs = copy.deepcopy(PARAGRAPHS)
    validate_facts(facts, paragraphs)
    assert facts == before
    assert paragraphs == PARAGRAPHS


# --- ATS compensation ----------------------------------------------------------------------


def _ashby_tier(title: str | None, components: list[dict[str, Any]]) -> dict[str, Any]:
    return {"id": "t", "title": title, "tierSummary": "", "components": components}


def _ashby_component(
    lo: float | None,
    hi: float | None,
    *,
    kind: str = "Salary",
    interval: str = "1 YEAR",
    currency: str | None = "USD",
) -> dict[str, Any]:
    return {
        "compensationType": kind,
        "interval": interval,
        "currencyCode": currency,
        "minValue": lo,
        "maxValue": hi,
    }


def _ashby(tiers: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "compensationTierSummary": "x" if tiers else None,
        "compensationTiers": tiers,
        "summaryComponents": [],
        "scrapeableCompensationSalarySummary": None,
    }


def test_ashby_fixture_salary_component() -> None:
    board = json.loads((FIXTURES / "ashby_board.json").read_text())
    comp = board["jobs"][0]["compensation"]
    assert ats_salary_ranges("ashby", comp) == [
        SalaryRange(
            location="Zone A", min=180000, max=220000, currency="USD", pay_type="annual_base"
        )
    ]


def test_ashby_hidden_pay_is_none() -> None:
    hidden: dict[str, Any] = {
        "compensationTiers": [],
        "summaryComponents": [],
        "compensationTierSummary": None,
        "scrapeableCompensationSalarySummary": None,
    }
    assert ats_salary_ranges("ashby", hidden) is None
    assert ats_salary_ranges("ashby", None) is None
    assert ats_salary_ranges("ashby", {}) is None


def test_ashby_one_range_per_tier_titled_or_blank() -> None:
    comp = _ashby(
        [
            _ashby_tier("San Francisco", [_ashby_component(200000, 250000)]),
            _ashby_tier("", [_ashby_component(180000, 225000)]),
            _ashby_tier(None, [_ashby_component(100000, 150000, currency="CAD")]),
        ]
    )
    ranges = ats_salary_ranges("ashby", comp)
    assert ranges is not None
    assert [(r.location, r.min, r.max, r.currency) for r in ranges] == [
        ("San Francisco", 200000, 250000, "USD"),
        ("", 180000, 225000, "USD"),
        ("", 100000, 150000, "CAD"),
    ]


def test_ashby_hourly_and_unusable_components() -> None:
    comp = _ashby(
        [
            _ashby_tier(
                "A",
                [
                    _ashby_component(
                        0.1, 0.25, kind="EquityPercentage", interval="NONE", currency=None
                    ),
                    _ashby_component(20000, 40000, kind="Bonus"),
                    _ashby_component(50000, 90000, kind="Commission"),
                    _ashby_component(60.5, 80, interval="1 HOUR"),
                ],
            ),
            _ashby_tier("B", [_ashby_component(10000, 12000, interval="1 MONTH")]),
            _ashby_tier("C", [_ashby_component(5000, 5000, interval="1 TIME")]),
            _ashby_tier("D", [_ashby_component(None, 100000)]),
            _ashby_tier("E", [_ashby_component(150000, 120000)]),
        ]
    )
    assert ats_salary_ranges("ashby", comp) == [
        # 60.5 rounds half-up: Python's round() would give 60 (banker's rounding).
        SalaryRange(location="A", min=61, max=80, currency="USD", pay_type="hourly")
    ]


def test_ashby_with_only_unusable_components_is_none() -> None:
    comp = _ashby([_ashby_tier("A", [_ashby_component(0.1, 0.2, kind="EquityPercentage")])])
    assert ats_salary_ranges("ashby", comp) is None


def test_ashby_falls_back_to_summary_components() -> None:
    comp = _ashby([])
    comp["summaryComponents"] = [_ashby_component(150000, 190000)]
    assert ats_salary_ranges("ashby", comp) == [
        SalaryRange(location="", min=150000, max=190000, currency="USD", pay_type="annual_base")
    ]


def test_lever_fixture_salary_range() -> None:
    board = json.loads((FIXTURES / "lever_board.json").read_text())
    assert ats_salary_ranges("lever", board[0]["salaryRange"]) == [
        SalaryRange(location="", min=180000, max=240000, currency="USD", pay_type="annual_base")
    ]


@pytest.mark.parametrize(
    ("comp", "expected"),
    [
        (
            {"min": 60, "max": 85, "currency": "USD", "interval": "per-hour-wage"},
            [SalaryRange(location="", min=60, max=85, currency="USD", pay_type="hourly")],
        ),
        ({"min": 5000, "max": 7000, "currency": "USD", "interval": "per-month-salary"}, None),
        ({"min": 0, "max": 0, "currency": "USD", "interval": "per-year-salary"}, None),
        ({"min": 100000, "currency": "USD", "interval": "per-year-salary"}, None),
        (
            {"min": 100000, "max": 120000, "currency": "usd", "interval": "per-year-salary"},
            [
                SalaryRange(
                    location="", min=100000, max=120000, currency="USD", pay_type="annual_base"
                )
            ],
        ),
        ({"min": 100000, "max": 120000, "currency": None, "interval": "per-year-salary"}, None),
        (None, None),
    ],
)
def test_lever_salary_range(comp: dict[str, Any] | None, expected: Any) -> None:
    assert ats_salary_ranges("lever", comp) == expected


def test_greenhouse_and_unknown_ats_are_none() -> None:
    lever_like = {"min": 1, "max": 2, "currency": "USD", "interval": "per-year-salary"}
    assert ats_salary_ranges("greenhouse", lever_like) is None
    assert ats_salary_ranges("workday", lever_like) is None


def test_malformed_ats_payload_is_none_not_an_error() -> None:
    assert ats_salary_ranges("ashby", {"compensationTiers": "oops"}) is None
    assert ats_salary_ranges("ashby", {"compensationTiers": [{"components": [7]}]}) is None
    assert (
        ats_salary_ranges(
            "lever", {"min": "lots", "max": True, "currency": "USD", "interval": "per-year-salary"}
        )
        is None
    )


# --- decide_pay ----------------------------------------------------------------------------


def sr(
    lo: int, hi: int, currency: str = "USD", location: str = "", pay_type: str = "annual_base"
) -> SalaryRange:
    return SalaryRange(location=location, min=lo, max=hi, currency=currency, pay_type=pay_type)  # type: ignore[arg-type]


def test_neither_source_has_pay() -> None:
    d = decide_pay([], None)
    assert (d.display_ranges, d.pay_source, d.disagreement, d.detail) == ([], "none", False, None)


def test_text_only() -> None:
    llm = [sr(180000, 220000, location="San Francisco, CA")]
    d = decide_pay(llm, None)
    assert (d.display_ranges, d.pay_source, d.disagreement) == (llm, "text", False)


def test_ats_agrees_within_5000() -> None:
    ats = [sr(180000, 220000)]
    d = decide_pay([sr(176000, 224000, location="New York, NY"), sr(1, 2)], ats)
    assert (d.display_ranges, d.pay_source, d.disagreement, d.detail) == (ats, "ats", False, None)


def test_ats_disagrees_beyond_5000() -> None:
    ats = [sr(180000, 220000)]
    d = decide_pay([sr(180000, 225001)], ats)
    assert (d.display_ranges, d.pay_source, d.disagreement) == (ats, "ats", True)
    assert d.detail is not None and "220000" in d.detail


def test_currency_must_match() -> None:
    d = decide_pay([sr(180000, 220000, currency="CAD")], [sr(180000, 220000)])
    assert d.disagreement


def test_every_ats_range_needs_a_matching_llm_range() -> None:
    ats = [sr(180000, 220000), sr(150000, 190000)]
    assert decide_pay([sr(180000, 220000)], ats).disagreement
    assert not decide_pay([sr(150000, 190000), sr(180000, 220000)], ats).disagreement


def test_ats_pay_with_no_text_pay_is_not_a_disagreement() -> None:
    # Many postings publish pay only in the ATS. Treating that as a disagreement would
    # trigger a retry that pressures the model to invent a salary (DESIGN §6).
    ats = [sr(180000, 220000)]
    d = decide_pay([], ats)
    assert (d.display_ranges, d.pay_source, d.disagreement) == (ats, "ats", False)
    assert d.detail is not None


# --- the header paragraph (id 0) -----------------------------------------------------------

HEADER = {
    "id": 0,
    "text": "Acme | Senior AI Engineer | New York, NY; San Francisco, CA | 5 openings",
}
WITH_HEADER = [HEADER, *PARAGRAPHS]


def test_header_is_citable_for_locations_and_remote() -> None:
    data = base()
    data["locations"]["evidence"] = [0]
    data["remote_policy"]["evidence"] = [0]
    data["seniority"]["evidence"] = [0]
    assert check(data, WITH_HEADER) == []


def test_salary_cannot_cite_the_header() -> None:
    data = base()
    data["salary"]["evidence"] = 0
    assert fields(check(data, WITH_HEADER)) == ["salary.evidence"]


def test_years_cannot_cite_the_header_even_if_it_has_the_number() -> None:
    # The header's "5 openings" contains the 5, but it isn't a years requirement.
    data = base()
    data["years_min"]["evidence"] = [0]
    assert fields(check(data, WITH_HEADER)) == ["years_min.evidence[0]"]
