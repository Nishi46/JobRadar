"""The taxonomy in code must match the table in docs/SKILLS.md, the human source of truth."""

import re
from pathlib import Path

from jobradar.taxonomy import SKILL_CODES, SKILLS

SKILLS_MD = Path(__file__).resolve().parents[2] / "docs" / "SKILLS.md"

# A table row like: | `LLM_PROD` | Shipping LLM features to production | 70% | 1 |
_ROW = re.compile(r"^\|\s*`([A-Z_]+)`\s*\|\s*([^|]+?)\s*\|")


def _parse_skills_md() -> dict[str, str]:
    text = SKILLS_MD.read_text(encoding="utf-8")
    section = text.split("## Starting taxonomy", 1)[1].split("\n## ", 1)[0]
    rows = (_ROW.match(line) for line in section.splitlines())
    return {m.group(1): m.group(2) for m in rows if m}


def test_codes_match_skills_md() -> None:
    documented = _parse_skills_md()
    assert len(documented) == 34  # guards the parser itself
    assert set(SKILLS) == set(documented)


def test_descriptions_match_skills_md() -> None:
    assert _parse_skills_md() == SKILLS


def test_skill_codes_is_the_key_set() -> None:
    assert frozenset(SKILLS) == SKILL_CODES
    assert isinstance(SKILL_CODES, frozenset)
