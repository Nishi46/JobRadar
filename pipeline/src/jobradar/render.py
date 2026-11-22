"""Turn database rows into the plain text the prompts embed.

Rendering is separate from the prompt files so the prompt wording can change (a new
prompt version) without touching how a posting is laid out, and vice versa. Every
function here is pure and deterministic: the same input always renders to the same
bytes, which keeps prompt caching effective and makes a trace easy to diff.

Grounding by reference: postings are shown as numbered paragraphs, `[id] text`, using
the ids stored in `postings.paragraphs` (D-011). The model cites those ids and code
checks them, so the numbering here must be exactly the stored ids, never re-counted.

Privacy: `render_profile` reads only the extracted profile facts and filters. Resume
text never passes through this module.
"""

from collections.abc import Iterable, Mapping, Sequence
from typing import Any, Protocol

from jobradar.taxonomy import SKILLS

# ---------------------------------------------------------------------------------------
# Taxonomy
# ---------------------------------------------------------------------------------------


def render_taxonomy() -> str:
    """One `CODE — description` line per skill, in SKILLS.md order.

    Goes into system prompts (it never changes between calls, so it sits in the
    cacheable prefix).
    """
    return "\n".join(f"{code} — {description}" for code, description in SKILLS.items())


# ---------------------------------------------------------------------------------------
# Postings
# ---------------------------------------------------------------------------------------


HEADER_PARAGRAPH_ID = 0


def header_paragraph(
    *,
    company: str,
    title: str,
    locations: list[str],
    departments: list[str],
    ats_hints: dict[str, str] | None = None,
) -> dict[str, Any]:
    """The posting header as citable paragraph 0.

    Why: locations, remote policy and often seniority (from the title) are stated only
    by the job board, not in the description. Without an id, the model couldn't cite
    them and validation would reject honest answers. Making the header paragraph 0
    lets the same evidence check cover it: the extraction pipeline prepends this dict
    to the paragraph list it validates against, so `validate_facts` needs no change.
    Ids 1..n stay the stored description ids (D-011).

    One line, so it is one paragraph. Locations are whitespace-normalized, de-blanked
    and SORTED exactly as `clean.content_hash` does (D-011): two postings with the same
    hash must render to the same prompt. Departments aren't hashed, so they keep the
    ATS order. ATS hints are appended as `key: value` parts.
    """
    parts = [
        f"{company} \u2014 {title}",
        f"Locations: {_join(_hash_order(locations), '; ')}",
        f"Departments: {_join(departments, '; ')}",
        *(f"{key}: {value}" for key, value in (ats_hints or {}).items()),
    ]
    return {"id": HEADER_PARAGRAPH_ID, "text": " | ".join(parts)}


def render_posting(
    *,
    company: str,
    title: str,
    locations: list[str],
    departments: list[str],
    paragraphs: list[dict[str, Any]],
    ats_hints: dict[str, str] | None = None,
) -> str:
    """`[0] header` (see header_paragraph), then one `[id] text` line per paragraph.

    A paragraph with id 0 in `paragraphs` is skipped: the header is always built here
    from the arguments, so a caller that already prepended it can't produce two [0]s.
    """
    header = header_paragraph(
        company=company,
        title=title,
        locations=locations,
        departments=departments,
        ats_hints=ats_hints,
    )
    body = [p for p in paragraphs if p["id"] != HEADER_PARAGRAPH_ID]
    return "\n".join(_paragraph_lines([header, *body]))


def ats_hints_from_raw(ats: str, ats_raw: Mapping[str, Any]) -> dict[str, str]:
    """Pick the few structured ATS fields that help extraction (remote policy mostly).

    Greenhouse's public board endpoint has no such fields, so it returns {}. Unknown
    or empty values are skipped rather than shown as "None": a hint that says nothing
    is noise. Compensation is deliberately NOT a hint: ATS pay is the independent label
    the extraction is cross-checked against (DESIGN §5), so the model must not see it.
    """
    hints: dict[str, str] = {}
    if ats == "ashby":
        _put(hints, "workplace type", ats_raw.get("workplaceType"))
        is_remote = ats_raw.get("isRemote")
        if isinstance(is_remote, bool):
            hints["remote"] = "yes" if is_remote else "no"
        _put(hints, "employment type", ats_raw.get("employmentType"))
    elif ats == "lever":
        _put(hints, "workplace type", ats_raw.get("workplaceType"))
        categories = ats_raw.get("categories")
        if isinstance(categories, Mapping):
            _put(hints, "employment type", categories.get("commitment"))
    return hints


# ---------------------------------------------------------------------------------------
# Profiles
# ---------------------------------------------------------------------------------------


class ProfileFactsLike(Protocol):
    """What render_profile needs from `profiles.ProfileFacts`.

    A Protocol (read-only properties) instead of importing ProfileFacts: profiles.py
    imports this module, so importing it back would be a cycle. Read-only members also
    let a `list[Track]` field satisfy `Sequence[str]`.
    """

    @property
    def skills(self) -> Sequence[str]: ...
    @property
    def tracks(self) -> Sequence[str]: ...
    @property
    def seniority(self) -> str: ...
    @property
    def years_experience(self) -> int | None: ...


class MatchFiltersLike(Protocol):
    """What render_profile needs from `profiles.MatchFilters`."""

    @property
    def tracks(self) -> Sequence[str]: ...
    @property
    def seniorities(self) -> Sequence[str]: ...
    @property
    def locations(self) -> Sequence[str]: ...
    @property
    def remote_ok(self) -> bool: ...
    @property
    def min_pay_usd(self) -> int | None: ...


def render_profile(facts: ProfileFactsLike, filters: MatchFiltersLike) -> str:
    """The profile summary the re-ranker sees: extracted facts plus the user's filters.

    Only codes and short labels: no resume text (personal data, and it would make the
    re-rank input large). Skill descriptions are included so the re-ranker doesn't have
    to look codes up.
    """
    skills = [
        f"- {code} — {SKILLS[code]}" if code in SKILLS else f"- {code}" for code in facts.skills
    ]
    years = facts.years_experience
    min_pay = filters.min_pay_usd
    lines = [
        "Skills:",
        *(skills or ["(none)"]),
        f"Tracks: {_join(facts.tracks, ', ', empty='(none)')}",
        f"Seniority: {facts.seniority}",
        f"Years of experience: {'unknown' if years is None else years}",
        "",
        "Filters the user set:",
        f"Track filter: {_join(filters.tracks, ', ', empty='any')}",
        f"Seniority filter: {_join(filters.seniorities, ', ', empty='any')}",
        f"Locations: {_join(filters.locations, ' | ', empty='any')}",
        f"Remote OK: {'yes' if filters.remote_ok else 'no'}",
        f"Minimum pay: {'none' if min_pay is None else f'USD {min_pay:,} per year'}",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------------------
# Re-rank candidates
# ---------------------------------------------------------------------------------------


def render_candidates(candidates: list[dict[str, Any]]) -> str:
    """Render the stage-1 candidates for the re-ranker, one `<candidate>` block each.

    Each candidate dict needs:
      - `posting_id`, `company`, `title`
      - `display_salary`: list of SalaryRange dicts (posting_facts.display_salary)
      - `facts`: the PostingFacts (posting_facts.facts JSON, or the model itself)
      - `paragraphs`: postings.paragraphs ({"id", "text"} dicts)

    Only the paragraphs the facts cite are included (deduped, in id order). That keeps
    20 candidates small enough for one call, and it means the re-ranker's own `why`
    citations can only point at paragraphs we already know support a fact.
    """
    if not candidates:
        return "(no candidates)"
    return "\n\n".join(_render_candidate(c) for c in candidates)


def _render_candidate(candidate: Mapping[str, Any]) -> str:
    facts = _as_dict(candidate["facts"])
    years = facts["years_min"]["value"]
    pay = [_format_range(r) for r in (_as_dict(r) for r in candidate["display_salary"])]
    cited = _cited_ids(facts)
    shown = [p for p in candidate["paragraphs"] if p["id"] in cited]
    required = [s["code"] for s in facts["required_skills"]]
    preferred = [s["code"] for s in facts["preferred_skills"]]
    lines = [
        f"<candidate posting_id={candidate['posting_id']}>",
        f"Company: {candidate['company']}",
        f"Title: {candidate['title']}",
        f"Track: {_join(facts['track']['value'], ', ')}",
        f"Seniority: {facts['seniority']['value']}",
        f"Minimum years: {'unknown' if years is None else years}",
        f"Remote policy: {facts['remote_policy']['value']}",
        f"Locations: {_join(facts['locations']['value'], ' | ')}",
        f"Pay: {'; '.join(pay) if pay else 'not published'}",
        f"Required skills: {_join(required, ', ', empty='(none)')}",
        f"Preferred skills: {_join(preferred, ', ', empty='(none)')}",
        "Cited paragraphs:",
        *(_paragraph_lines(shown) or ["(none)"]),
        "</candidate>",
    ]
    return "\n".join(lines)


def _cited_ids(facts: Mapping[str, Any]) -> set[int]:
    """Every paragraph id any field of the facts cites."""
    ids: set[int] = set()
    for field in (
        "track",
        "seniority",
        "years_min",
        "locations",
        "remote_policy",
        "visa_sponsorship",
    ):
        ids.update(facts[field]["evidence"])
    if facts["salary"]["evidence"] is not None:
        ids.add(facts["salary"]["evidence"])
    for skill in (*facts["required_skills"], *facts["preferred_skills"]):
        ids.add(skill["evidence"])
    return ids


_EN_DASH = "\u2013"  # escaped so ruff's ambiguous-character check stays quiet


def _format_range(r: Mapping[str, Any]) -> str:
    # Currency code instead of "$" so a CAD or GBP range can't be misread as USD.
    span = f"{r['min']:,}" if r["min"] == r["max"] else f"{r['min']:,}{_EN_DASH}{r['max']:,}"
    where = f" ({r['location']})" if r["location"] else ""
    return f"{r['currency']} {span} {r['pay_type']}{where}"


# ---------------------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------------------


def _paragraph_lines(paragraphs: Iterable[Mapping[str, Any]]) -> list[str]:
    return [f"[{p['id']}] {p['text']}" for p in sorted(paragraphs, key=lambda p: int(p["id"]))]


def _hash_order(locations: Iterable[str]) -> list[str]:
    """Same normalization as clean.content_hash: collapse whitespace, drop empties, sort.

    str.split() with no argument splits on any Unicode whitespace run, matching the
    cleaner's `\\s+` collapse.
    """
    return sorted(n for n in (" ".join(loc.split()) for loc in locations) if n)


def _join(items: Iterable[str], sep: str, *, empty: str = "(none listed)") -> str:
    items = list(items)
    return sep.join(items) if items else empty


def _put(hints: dict[str, str], key: str, value: Any) -> None:
    if isinstance(value, str) and value.strip():
        hints[key] = value.strip()


def _as_dict(value: Any) -> Mapping[str, Any]:
    """Accept either stored JSON (a dict) or a Pydantic model."""
    if isinstance(value, Mapping):
        return value
    dumped: Mapping[str, Any] = value.model_dump()
    return dumped
