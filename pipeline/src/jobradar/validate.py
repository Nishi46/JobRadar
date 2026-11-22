"""Check an extraction in code, and decide which pay to show (DESIGN §4-5, D-003).

Why code checks the model instead of trusting it: every rule here is exact (an id exists,
a number appears in a paragraph, a code is in the taxonomy), so code can enforce it for
free on every posting, while an eval can only sample. The big one is the invented-salary
guarantee (DESIGN §6): ~100 no-salary postings in the golden set can only bound that error
rate at ~3%, but "every salary number must appear in the cited paragraph" blocks it outright.

`validate_facts` returns *all* problems as `Issue`s rather than raising on the first: the
extraction pipeline pastes them back to the model in ONE repair retry, and a model fixes
more when it sees the whole list. Messages are written for that reader: what is wrong and
what a correct answer looks like.

`ats_salary_ranges` / `decide_pay` implement the ATS pay cross-check (DESIGN §5): when the
ATS publishes structured pay, that is what users see; the model's ranges are still stored
separately (in `posting_facts.facts`) so ATS pay stays usable as an eval label in M3.
"""

import re
from dataclasses import dataclass
from decimal import ROUND_FLOOR, ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from jobradar.facts import PaySource, PayType, PostingFacts, SalaryRange, SkillCite
from jobradar.render import HEADER_PARAGRAPH_ID
from jobradar.taxonomy import SKILL_CODES

# DESIGN §6 scores salary "correct within $5K", so the cross-check uses the same bar.
PAY_AGREEMENT_TOLERANCE = 5_000

_CURRENCY = re.compile(r"[A-Z]{3}")

# The header (company | title | locations ...) is citable for facts it states, such as
# locations or remote policy, but never for a number: "5 openings" or a location's
# digits would let a salary or years_min pass the numbers-in-text check without the
# description stating it.
_NUMBERS_NOT_IN_HEADER = (
    "{field} must cite the description paragraph where the number appears, not the header "
    f"({HEADER_PARAGRAPH_ID})."
)


@dataclass(frozen=True)
class Issue:
    field: str  # e.g. "salary.ranges[0].min", "required_skills[2].code"
    message: str  # written so it can be pasted back to the model in the repair retry


@dataclass(frozen=True)
class PayDecision:
    display_ranges: list[SalaryRange]  # what users see
    pay_source: PaySource
    disagreement: bool  # ATS and model ranges differ (logged; triggers the one retry)
    detail: str | None  # human-readable explanation for logs; None when nothing to note


# --- finding numbers in posting text -------------------------------------------------------

# One monetary amount. Alternatives are tried in order, so the grouped forms win over a
# bare number and "180,000" is never read as "180" then "000". The lookbehind stops a match
# from starting mid-number. Verbose mode keeps literal spaces inside [...] classes.
_AMOUNT = re.compile(
    r"""
    (?<![\d.,])
    (?:
        (?P<comma>\d{1,3}(?:,\d{3})+(?:\.\d+)?)               # 180,000   1,250,000.50
      | (?P<space>\d{1,3}(?:[ \u00a0\u202f\u2009]\d{3})+)(?!\d) # 180 000 (incl. nbsp/thin)
      | (?P<dot>\d{1,3}(?:\.\d{3})+)(?![\d.,])(?!\s?[kKmM]\b)  # 180.000 (European)
      | (?P<plain>\d+(?:\.\d+)?)                              # 180   180.5   85
    )
    (?:\s?(?P<suffix>[kKmM])(?![A-Za-z]))?                    # 180K  1.2M (not "180 Main")
    """,
    re.VERBOSE,
)

# What may sit between the two ends of a range for a trailing suffix to cover both:
# "$150-200K", "150 to 200k", "$150 - $200k" (hyphen, en or em dash).
_RANGE_JOIN = re.compile(r"\s*(?:-|\u2013|\u2014|to)\s*[$€£]?\s*")

_MULTIPLIER = {"k": Decimal(1_000), "m": Decimal(1_000_000)}


def _amount_value(match: re.Match[str]) -> Decimal:
    if match["comma"]:
        digits = match["comma"].replace(",", "")
    elif match["space"]:
        digits = re.sub(r"\D", "", match["space"])
    elif match["dot"]:
        digits = match["dot"].replace(".", "")
    else:
        digits = match["plain"]
    return Decimal(digits)


def _whole_numbers(value: Decimal) -> set[int]:
    # SalaryRange holds ints, so a stated "$85.50/hr" is right as either 85 or 86.
    if value == value.to_integral_value():
        return {int(value)}
    return {int(value.to_integral_value(ROUND_FLOOR)), int(value.to_integral_value(ROUND_HALF_UP))}


def amounts_in_text(text: str) -> set[int]:
    """Every whole-number amount a salary in this text could have been copied from.

    Accepts "$180,000", "180,000", "$180K", "180k", "$180.5K", "180 000", "180.000" and
    hourly "$85/hr" (-> 85). A suffix on the end of a range also applies to the start
    ("$150-200K" -> 150000 and 200000; the bare 150 is kept too, in case it is hourly).
    Leaning permissive is safe: a stray extra number can only make the check accept a
    number that IS in the text, never one that isn't.
    """
    matches = list(_AMOUNT.finditer(text))
    found: set[int] = set()
    for i, m in enumerate(matches):
        value = _amount_value(m)
        suffix = m["suffix"]
        found |= _whole_numbers(value * _MULTIPLIER[suffix.lower()] if suffix else value)
        if not suffix and i + 1 < len(matches):
            nxt = matches[i + 1]
            if nxt["suffix"] and _RANGE_JOIN.fullmatch(text, m.end(), nxt.start()):
                found |= _whole_numbers(value * _MULTIPLIER[nxt["suffix"].lower()])
    return found


# Years are small integers; postings sometimes spell them out ("five or more years").
_NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "fifteen": 15,
    "twenty": 20,
}  # fmt: skip
_WHOLE_INT = re.compile(r"(?<!\d)\d+(?!\d)")
_WORD = re.compile(r"[a-z]+")


def _integers_in_text(text: str) -> set[int]:
    found = {int(n) for n in _WHOLE_INT.findall(text)}
    found |= {_NUMBER_WORDS[w] for w in _WORD.findall(text.lower()) if w in _NUMBER_WORDS}
    return found


# --- validate_facts ------------------------------------------------------------------------


def _norm_location(location: str) -> str:
    # Case- and space-insensitive: "New York,  NY" and "new york, ny" are the same place.
    return " ".join(location.split()).casefold()


class _Checker:
    """Accumulates issues for one PostingFacts; one method per rule family."""

    def __init__(self, paragraphs: list[dict[str, Any]]) -> None:
        self.text_by_id: dict[int, str] = {int(p["id"]): str(p["text"]) for p in paragraphs}
        self.issues: list[Issue] = []
        ids = sorted(self.text_by_id)
        self.valid_ids = f"valid ids are {ids[0]}-{ids[-1]}" if ids else "the posting is empty"

    def add(self, field: str, message: str) -> None:
        self.issues.append(Issue(field=field, message=message))

    def evidence(
        self, field: str, evidence: list[int], *, abstained: bool, header_ok: bool = True
    ) -> list[str]:
        """Check a citation list; return the texts of the cited paragraphs that exist."""
        if not evidence and not abstained:
            self.add(
                f"{field}.evidence",
                f"{field} has a value but cites no paragraph. Cite the paragraph id(s) "
                "that state it, or use the 'unknown'/'unstated'/null/[] answer if the "
                "posting doesn't say.",
            )
        texts = []
        for i, eid in enumerate(evidence):
            if eid == HEADER_PARAGRAPH_ID and not header_ok:
                self.add(f"{field}.evidence[{i}]", _NUMBERS_NOT_IN_HEADER.format(field=field))
            elif eid in self.text_by_id:
                texts.append(self.text_by_id[eid])
            else:
                self.add(
                    f"{field}.evidence[{i}]",
                    f"Paragraph {eid} does not exist ({self.valid_ids}).",
                )
        return texts

    def skill(self, field: str, cite: SkillCite, seen: set[str], required: set[str]) -> None:
        if cite.code not in SKILL_CODES:
            self.add(
                f"{field}.code",
                f"'{cite.code}' is not a taxonomy code. Use a listed code exactly "
                "(uppercase), or drop the skill if none fits.",
            )
        elif cite.code in seen:
            self.add(f"{field}.code", f"'{cite.code}' is listed twice; keep one entry.")
        elif cite.code in required:
            self.add(
                f"{field}.code",
                f"'{cite.code}' is in both required_skills and preferred_skills; keep it "
                "in one list (preferred only if the posting marks it optional).",
            )
        seen.add(cite.code)
        if cite.evidence not in self.text_by_id:
            self.add(
                f"{field}.evidence",
                f"Paragraph {cite.evidence} does not exist ({self.valid_ids}).",
            )


def validate_facts(facts: PostingFacts, paragraphs: list[dict[str, Any]]) -> list[Issue]:
    """Every rule violation in `facts` given the posting's `paragraphs`; [] means valid.

    `paragraphs` is `postings.paragraphs`: [{"id": 1, "text": "..."}, ...] (D-011).
    """
    c = _Checker(paragraphs)

    # track: non-empty, and "unknown" means "can't tell", so it can't sit beside a real track.
    track = facts.track.value
    if not track:
        c.add("track.value", 'track must list at least one track; use ["unknown"] if unclear.')
    elif "unknown" in track and len(track) > 1:
        c.add(
            "track.value",
            f"'unknown' cannot be combined with other tracks (got {track}). Either list the "
            'tracks the posting supports, or use ["unknown"] alone.',
        )
    c.evidence("track", facts.track.evidence, abstained=track in ([], ["unknown"]))

    c.evidence("seniority", facts.seniority.evidence, abstained=facts.seniority.value == "unknown")

    years = facts.years_min.value
    year_texts = c.evidence(
        "years_min", facts.years_min.evidence, abstained=years is None, header_ok=False
    )
    if (
        years is not None
        and year_texts
        and not any(years in _integers_in_text(t) for t in year_texts)
    ):
        c.add(
            "years_min.value",
            f"years_min is {years}, but that number doesn't appear in the cited "
            "paragraph(s). Use the lowest number of years the posting states, citing the "
            "paragraph it's in, or null if no number is stated.",
        )

    locations = facts.locations.value
    c.evidence("locations", facts.locations.evidence, abstained=not locations)
    c.evidence(
        "remote_policy",
        facts.remote_policy.evidence,
        abstained=facts.remote_policy.value == "unknown",
    )
    c.evidence(
        "visa_sponsorship",
        facts.visa_sponsorship.evidence,
        abstained=facts.visa_sponsorship.value == "unstated",
    )

    _check_salary(c, facts, {_norm_location(loc) for loc in locations})

    required: set[str] = set()
    for i, cite in enumerate(facts.required_skills):
        c.skill(f"required_skills[{i}]", cite, required, set())
    preferred: set[str] = set()
    for i, cite in enumerate(facts.preferred_skills):
        c.skill(f"preferred_skills[{i}]", cite, preferred, required)

    return c.issues


def _check_salary(c: _Checker, facts: PostingFacts, locations: set[str]) -> None:
    salary = facts.salary
    cited_text: str | None = None
    if salary.evidence is None:
        if salary.ranges:
            c.add(
                "salary.evidence",
                "salary has ranges but no citation. Cite the one paragraph that states "
                "the pay, or return no ranges if the posting doesn't state pay.",
            )
    elif salary.evidence == HEADER_PARAGRAPH_ID:
        c.add("salary.evidence", _NUMBERS_NOT_IN_HEADER.format(field="salary"))
    elif salary.evidence in c.text_by_id:
        cited_text = c.text_by_id[salary.evidence]
    else:
        c.add("salary.evidence", f"Paragraph {salary.evidence} does not exist ({c.valid_ids}).")

    # Only check numbers against a paragraph that exists: a bad id is one clear issue, not
    # one per number.
    amounts = amounts_in_text(cited_text) if cited_text is not None else None
    for i, r in enumerate(salary.ranges):
        field = f"salary.ranges[{i}]"
        for name, value in (("min", r.min), ("max", r.max)):
            if value <= 0:
                c.add(f"{field}.{name}", f"{name} must be a positive amount (got {value}).")
            elif amounts is not None and value not in amounts:
                c.add(
                    f"{field}.{name}",
                    f"{name} {value} does not appear in paragraph {salary.evidence}. Copy "
                    "the number exactly as stated (e.g. '$180K' -> 180000), and only "
                    "include pay the posting states.",
                )
        if r.min > r.max:
            c.add(f"{field}.min", f"min ({r.min}) is greater than max ({r.max}).")
        if not _CURRENCY.fullmatch(r.currency):
            c.add(
                f"{field}.currency",
                f"currency '{r.currency}' must be a 3-letter uppercase ISO code like 'USD'.",
            )
        if _norm_location(r.location) not in locations:
            c.add(
                f"{field}.location",
                f"Range location '{r.location}' is not one of locations.value. Use the "
                "exact normalized location string from locations.value (add the location "
                "there if the posting lists it).",
            )


# --- ATS compensation ----------------------------------------------------------------------

# Only base pay maps cleanly onto SalaryRange. Ashby lists commission and bonus as separate
# components, and summing them into an OTE would be a number the employer never wrote, so
# those are ignored, as are monthly/one-time intervals (PayType has no slot for them).
_ASHBY_PAY_TYPES: dict[str, PayType] = {"1 YEAR": "annual_base", "1 HOUR": "hourly"}
_LEVER_PAY_TYPES: dict[str, PayType] = {
    "per-year-salary": "annual_base",
    "per-hour-wage": "hourly",
}


def _whole_amount(value: Any) -> int | None:
    # bool is an int subclass in Python; `True` is never a salary.
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    try:
        return int(Decimal(str(value)).to_integral_value(ROUND_HALF_UP))
    except InvalidOperation:  # nan / inf
        return None


def _ats_range(
    location: str, lo: Any, hi: Any, currency: Any, pay_type: PayType
) -> SalaryRange | None:
    """A SalaryRange from raw ATS values, or None if any part is missing or nonsensical."""
    lo_int, hi_int = _whole_amount(lo), _whole_amount(hi)
    if lo_int is None or hi_int is None or lo_int <= 0 or lo_int > hi_int:
        return None
    if not isinstance(currency, str) or not _CURRENCY.fullmatch(code := currency.strip().upper()):
        return None
    return SalaryRange(location=location, min=lo_int, max=hi_int, currency=code, pay_type=pay_type)


def _ashby_component(component: Any, location: str) -> SalaryRange | None:
    if not isinstance(component, dict) or component.get("compensationType") != "Salary":
        return None
    pay_type = _ASHBY_PAY_TYPES.get(str(component.get("interval")))
    if pay_type is None:
        return None
    return _ats_range(
        location,
        component.get("minValue"),
        component.get("maxValue"),
        component.get("currencyCode"),
        pay_type,
    )


def _ashby_ranges(comp: dict[str, Any]) -> list[SalaryRange]:
    """One range per tier's Salary component.

    Ashby has no location field on a tier. Its `title` is the label Ashby shows next to
    the range, which is sometimes a place ("San Francisco", "Tier 1 - SF Bay Area"),
    sometimes a level ("L5 • Senior") or zone ("Zone A"), and often empty. We keep the
    title as `location` (or "" when empty): it's the most faithful label for display, a
    real place name still lets the pay filter match it, and a non-place simply doesn't
    match, so the filter falls back to the highest range (DESIGN §4).
    """
    ranges: list[SalaryRange] = []
    tiers = comp.get("compensationTiers")
    if isinstance(tiers, list) and tiers:
        for tier in tiers:
            if not isinstance(tier, dict) or not isinstance(tier.get("components"), list):
                continue
            title = tier.get("title")
            location = title.strip() if isinstance(title, str) else ""
            ranges += [r for c in tier["components"] if (r := _ashby_component(c, location))]
    elif isinstance(summary := comp.get("summaryComponents"), list):
        # Not seen in real data yet (hidden pay empties both), but it's the same shape.
        ranges += [r for c in summary if (r := _ashby_component(c, ""))]
    return ranges


def _lever_ranges(comp: dict[str, Any]) -> list[SalaryRange]:
    # Lever has one range per posting with no location attached, hence "".
    pay_type = _LEVER_PAY_TYPES.get(str(comp.get("interval")))
    if pay_type is None:
        return []
    r = _ats_range("", comp.get("min"), comp.get("max"), comp.get("currency"), pay_type)
    return [r] if r else []


def ats_salary_ranges(
    ats: str, ats_compensation: dict[str, Any] | None
) -> list[SalaryRange] | None:
    """Normalize `postings.ats_compensation` into SalaryRanges; None = no usable ATS pay.

    Ashby: `compensation.compensationTiers[*].components[*]` with compensationType
    "Salary" (minValue, maxValue, currencyCode, interval "1 YEAR" | "1 HOUR"); Ashby
    sends the object even when pay is hidden, with empty tiers and a null summary.
    Lever: `salaryRange` {min, max, currency, interval "per-year-salary" | "per-hour-wage"}.
    Greenhouse's public API has no structured pay. Anything malformed is skipped rather
    than raised: ATS payloads are untrusted, and a bad one must not fail an extraction.
    """
    if not isinstance(ats_compensation, dict):
        return None
    if ats == "ashby":
        ranges = _ashby_ranges(ats_compensation)
    elif ats == "lever":
        ranges = _lever_ranges(ats_compensation)
    else:
        return None
    return ranges or None


def _agrees(llm: SalaryRange, ats: SalaryRange) -> bool:
    return (
        llm.currency == ats.currency
        and abs(llm.min - ats.min) <= PAY_AGREEMENT_TOLERANCE
        and abs(llm.max - ats.max) <= PAY_AGREEMENT_TOLERANCE
    )


def _describe(ranges: list[SalaryRange]) -> str:
    return "; ".join(f"{r.min}-{r.max} {r.currency} {r.pay_type} at '{r.location}'" for r in ranges)


def decide_pay(llm_ranges: list[SalaryRange], ats_ranges: list[SalaryRange] | None) -> PayDecision:
    """Which ranges to display, where they came from, and whether the sources disagree.

    ATS pay wins whenever it exists: it's structured data the employer entered, so it
    can't be misread. They agree when every ATS range has some model range with the same
    currency within $5,000 on both min and max (extra model ranges, e.g. more cities, are
    fine). Location is not compared: ATS tiers rarely carry a usable one.

    ATS pay with NO model ranges is not a disagreement: many postings publish pay only in
    the ATS, and the text genuinely states none. Flagging it would spend the retry asking
    the model to find a salary that isn't in the text, i.e. pressure it to invent one.
    """
    if ats_ranges:
        if not llm_ranges:
            return PayDecision(
                display_ranges=list(ats_ranges),
                pay_source="ats",
                disagreement=False,
                detail="ATS publishes pay; the posting text states none.",
            )
        unmatched = [a for a in ats_ranges if not any(_agrees(m, a) for m in llm_ranges)]
        detail = None
        if unmatched:
            detail = (
                f"ATS range(s) with no text range within {PAY_AGREEMENT_TOLERANCE} in the "
                f"same currency: {_describe(unmatched)}. Text ranges: {_describe(llm_ranges)}."
            )
        return PayDecision(
            display_ranges=list(ats_ranges),
            pay_source="ats",
            disagreement=bool(unmatched),
            detail=detail,
        )
    if llm_ranges:
        return PayDecision(list(llm_ranges), pay_source="text", disagreement=False, detail=None)
    return PayDecision(display_ranges=[], pay_source="none", disagreement=False, detail=None)
