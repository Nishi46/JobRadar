"""`PostingFacts`: exactly the JSON the extraction model returns (DESIGN §5).

Why the schema is deliberately loose: it doubles as the structured-output schema sent to
the provider, and Anthropic structured outputs reject (or silently ignore) JSON Schema
constraints like `minimum`, `pattern` or `minItems`. So this module only fixes the *shape*
and the allowed enum values. Every rule that needs a number, a regex or the posting text
(evidence ids exist, salary numbers appear in the cited paragraph, codes are in the
taxonomy...) lives in `validate.py`, where a failure becomes an `Issue` the repair retry
can show the model, instead of an opaque schema rejection.

Why every field carries `evidence` (paragraph ids, 1-based, from `postings.paragraphs`):
citations make each fact checkable in code and debuggable by a human, and they give the
model a reason not to guess. "unknown" / "unstated" / null / [] are always allowed answers;
abstaining is better than inventing (DESIGN §6: a wrong answer is worse than a missing one).

`extra="forbid"` puts `additionalProperties: false` on every object, which structured
outputs requires, and makes a model that drifts from the schema fail loudly instead of
having its extra keys dropped.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict

Track = Literal["platform", "product", "forward_deployed", "research", "other", "unknown"]
Seniority = Literal["junior", "mid", "senior", "staff_plus", "manager", "unknown"]
RemotePolicy = Literal["onsite", "hybrid", "remote", "unknown"]
Visa = Literal["yes", "no", "unstated"]
PayType = Literal["annual_base", "ote", "hourly"]
# Set by code (validate.decide_pay), never by the model; lives here so all pay types sit together.
PaySource = Literal["ats", "text", "none"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TrackField(_Strict):
    # A list because generalist roles are legitimately several tracks (labeling rules, §5).
    value: list[Track]
    evidence: list[int]


class SeniorityField(_Strict):
    value: Seniority
    evidence: list[int]


class YearsField(_Strict):
    # The lowest stated number ("3+ with a degree / 7+" -> 3); null when not stated.
    value: int | None
    evidence: list[int]


class LocationsField(_Strict):
    # Normalized: "City, ST" (US), "City, Country", or "Remote - US" style.
    value: list[str]
    evidence: list[int]


class RemoteField(_Strict):
    value: RemotePolicy
    evidence: list[int]


class VisaField(_Strict):
    value: Visa
    evidence: list[int]


class SalaryRange(_Strict):
    # `location` must be one of LocationsField.value (D-003); ATS-derived ranges may use "".
    location: str
    min: int
    max: int
    currency: str  # ISO 4217, e.g. "USD"; format checked in validate.py
    pay_type: PayType


class SalaryField(_Strict):
    ranges: list[SalaryRange]
    # ONE citation covers the whole list (D-003). Null with empty ranges = "not stated".
    evidence: int | None


class SkillCite(_Strict):
    code: str  # a taxonomy code; checked against taxonomy.SKILL_CODES in validate.py
    evidence: int


class PostingFacts(_Strict):
    track: TrackField
    seniority: SeniorityField
    years_min: YearsField
    salary: SalaryField
    locations: LocationsField
    remote_policy: RemoteField
    visa_sponsorship: VisaField
    required_skills: list[SkillCite]
    preferred_skills: list[SkillCite]
