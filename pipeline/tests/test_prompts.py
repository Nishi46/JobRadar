"""Tests for the prompt registry: file format, version rules, rendering, and the real prompts."""

from pathlib import Path
from typing import get_args

import pytest

from jobradar.facts import PayType, RemotePolicy, Seniority, Track, Visa
from jobradar.prompts import PROMPTS_DIR, load_prompt, parse_prompt
from jobradar.render import render_taxonomy
from jobradar.taxonomy import SKILL_CODES

VALID = """\
---
name: demo
version: 2
provider: anthropic
model: claude-haiku-5-5
max_tokens: 500
effort: low
description: A demo prompt.
---

## system
You read postings. Codes:
$taxonomy
Pay looks like $$180K.

## user
<posting>
$posting
</posting>
"""


def write(dir_: Path, filename: str, text: str) -> Path:
    path = dir_ / filename
    path.write_text(text, encoding="utf-8")
    return path


# --- parsing -----------------------------------------------------------------------------


def test_parse_reads_frontmatter_and_sections() -> None:
    prompt = parse_prompt(VALID, filename="demo.v2.md")
    assert prompt.name == "demo"
    assert prompt.version == 2
    assert prompt.provider == "anthropic"
    assert prompt.model == "claude-haiku-5-5"
    assert prompt.max_tokens == 500
    assert prompt.effort == "low"
    assert prompt.description == "A demo prompt."
    assert prompt.system.startswith("You read postings.")
    assert prompt.system.endswith("Pay looks like $$180K.")  # raw template, not rendered
    assert prompt.user_template == "<posting>\n$posting\n</posting>"


def test_effort_is_optional() -> None:
    prompt = parse_prompt(VALID.replace("effort: low\n", ""), filename="demo.v2.md")
    assert prompt.effort is None


def test_version_must_match_filename() -> None:
    with pytest.raises(ValueError, match="version"):
        parse_prompt(VALID, filename="demo.v3.md")


def test_name_must_match_filename() -> None:
    with pytest.raises(ValueError, match="name"):
        parse_prompt(VALID, filename="other.v2.md")


def test_filename_must_follow_the_pattern() -> None:
    with pytest.raises(ValueError, match="filename"):
        parse_prompt(VALID, filename="demo.md")


@pytest.mark.parametrize(
    "key", ["name", "version", "provider", "model", "max_tokens", "description"]
)
def test_missing_required_key_is_rejected(key: str) -> None:
    lines = [line for line in VALID.splitlines(keepends=True) if not line.startswith(f"{key}:")]
    with pytest.raises(ValueError, match=key):
        parse_prompt("".join(lines), filename="demo.v2.md")


def test_unknown_key_is_rejected() -> None:
    # A typo like "max_token" would otherwise silently fall back to nothing.
    text = VALID.replace("effort: low\n", "effort: low\ntemperature: 0\n")
    with pytest.raises(ValueError, match="temperature"):
        parse_prompt(text, filename="demo.v2.md")


def test_non_integer_numbers_are_rejected() -> None:
    with pytest.raises(ValueError, match="max_tokens"):
        parse_prompt(VALID.replace("max_tokens: 500", "max_tokens: lots"), filename="demo.v2.md")


def test_unknown_provider_is_rejected() -> None:
    with pytest.raises(ValueError, match="provider"):
        parse_prompt(VALID.replace("provider: anthropic", "provider: acme"), filename="demo.v2.md")


def test_missing_frontmatter_is_rejected() -> None:
    with pytest.raises(ValueError, match="frontmatter"):
        parse_prompt("## system\nhi\n## user\nthere\n", filename="demo.v2.md")


@pytest.mark.parametrize(
    "body",
    [
        "## system\nonly a system section\n",
        "## user\nonly a user section\n",
        "## user\nwrong order\n## system\nhere\n",
        "## system\na\n## system\nb\n## user\nc\n",
    ],
)
def test_sections_must_be_system_then_user_exactly_once(body: str) -> None:
    head = VALID.split("## system")[0]
    with pytest.raises(ValueError, match="section"):
        parse_prompt(head + body, filename="demo.v2.md")


def test_unescaped_dollar_is_caught_at_load_time() -> None:
    # "$180K" is not a valid placeholder; string.Template would only fail at render time,
    # i.e. in the middle of a batch run. Loading must fail first.
    text = VALID.replace("$$180K", "$180K")
    with pytest.raises(ValueError, match="placeholder"):
        parse_prompt(text, filename="demo.v2.md")


# --- rendering ---------------------------------------------------------------------------


def test_render_substitutes_both_sections() -> None:
    prompt = parse_prompt(VALID, filename="demo.v2.md")
    system, user = prompt.render(taxonomy="PY — Python", posting="[1] Hello")
    assert "PY — Python" in system
    assert "$180K" in system  # $$ is the escape for a literal dollar sign
    assert user == "<posting>\n[1] Hello\n</posting>"


def test_render_does_not_reinterpret_dollars_in_values() -> None:
    prompt = parse_prompt(VALID, filename="demo.v2.md")
    _, user = prompt.render(taxonomy="x", posting="Pay: $180,000 $taxonomy")
    assert "Pay: $180,000 $taxonomy" in user


def test_render_names_every_missing_placeholder() -> None:
    prompt = parse_prompt(VALID, filename="demo.v2.md")
    with pytest.raises(KeyError) as excinfo:
        prompt.render()
    assert "posting" in str(excinfo.value)
    assert "taxonomy" in str(excinfo.value)


# --- load_prompt -------------------------------------------------------------------------


def test_load_picks_highest_version_by_default(tmp_path: Path) -> None:
    write(tmp_path, "demo.v2.md", VALID)
    write(tmp_path, "demo.v10.md", VALID.replace("version: 2", "version: 10"))
    write(tmp_path, "demox.v99.md", VALID.replace("name: demo", "name: demox"))
    assert load_prompt("demo", prompts_dir=tmp_path).version == 10  # numeric, not lexical
    assert load_prompt("demo", 2, prompts_dir=tmp_path).version == 2


def test_load_missing_prompt_raises(tmp_path: Path) -> None:
    write(tmp_path, "demo.v2.md", VALID)
    with pytest.raises(FileNotFoundError, match="nope"):
        load_prompt("nope", prompts_dir=tmp_path)
    with pytest.raises(FileNotFoundError, match="v7"):
        load_prompt("demo", 7, prompts_dir=tmp_path)


# --- the real prompt files ---------------------------------------------------------------


def test_prompts_dir_is_the_repo_prompts_folder() -> None:
    assert PROMPTS_DIR.name == "prompts"
    assert (PROMPTS_DIR.parent / "pipeline").is_dir()


@pytest.mark.parametrize(
    ("name", "provider", "model", "effort"),
    [
        ("extract_posting", "anthropic", "claude-haiku-5-5", "low"),
        ("extract_profile", "anthropic", "claude-haiku-5-5", "low"),
        ("rerank", "openai", "gpt-6.2-sol", None),
    ],
)
def test_real_prompt_frontmatter(name: str, provider: str, model: str, effort: str | None) -> None:
    prompt = load_prompt(name, 1)
    assert (prompt.provider, prompt.model, prompt.max_tokens, prompt.effort) == (
        provider,
        model,
        8000,
        effort,
    )


@pytest.mark.parametrize(
    ("name", "values"),
    [
        ("extract_posting", {"posting": "[1] x"}),
        ("extract_profile", {"resume": "x"}),
        ("rerank", {"profile": "x", "candidates": "x"}),
    ],
)
def test_real_prompts_render_with_taxonomy_in_system(name: str, values: dict[str, str]) -> None:
    prompt = load_prompt(name)
    system, user = prompt.render(taxonomy=render_taxonomy(), **values)
    # The taxonomy goes in the system prompt so it is part of the stable, cacheable prefix.
    assert "$taxonomy" in prompt.system
    assert "$taxonomy" not in prompt.user_template
    for code in SKILL_CODES:
        assert f"{code} — " in system
    for placeholder in ("$taxonomy", "$posting", "$resume", "$profile", "$candidates"):
        assert placeholder not in system  # nothing left unrendered
        assert placeholder not in user
    assert user.strip()


def test_extract_posting_names_every_field_and_allowed_value() -> None:
    system = load_prompt("extract_posting").system
    fields = [
        "track",
        "seniority",
        "years_min",
        "salary",
        "ranges",
        "locations",
        "remote_policy",
        "visa_sponsorship",
        "required_skills",
        "preferred_skills",
        "evidence",
    ]
    for field in fields:
        assert f"`{field}`" in system, field
    for literal in (Track, Seniority, RemotePolicy, Visa, PayType):
        for value in get_args(literal):
            assert f"`{value}`" in system, value


def test_extract_profile_names_every_field() -> None:
    system = load_prompt("extract_profile").system
    for field in ("skills", "tracks", "seniority", "years_experience"):
        assert f"`{field}`" in system, field


def test_rerank_names_every_output_field() -> None:
    system = load_prompt("rerank").system
    for field in ("matches", "posting_id", "why", "evidence", "skills_to_learn"):
        assert f"`{field}`" in system, field


def test_extract_posting_explains_header_paragraph_0() -> None:
    # The header is citable as paragraph 0 (header-only locations/remote/title facts would
    # otherwise be uncitable), but pay and years must be cited where the numbers appear.
    system = load_prompt("extract_posting").system
    assert "`[0]` is the posting header" in system
    assert "Paragraph 0 (the header) can be cited" in system
    assert "never 0" in system
    assert "no paragraph id" not in system  # the pre-header-paragraph wording is gone
