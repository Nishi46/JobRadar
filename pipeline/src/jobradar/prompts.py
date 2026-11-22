"""Load versioned prompt files from `prompts/` and render them.

Why prompts live in files and not in Python strings: a prompt is the thing we iterate
on most in M2/M3, and every output row records `prompt_name` + `prompt_version`. A file
per version (`extract_posting.v1.md`, `extract_posting.v2.md`, ...) makes that version
mean something: v1 is frozen on disk, so an eval report or an `llm_runs` row that says
"v1" can always be traced back to the exact text that produced it. Changing a prompt
means adding a new file, which shows up clearly in review.

The model choice is in the same frontmatter on purpose: the prompt and the model are
tuned together, so switching models is a prompt change and bumps the version too.

File format:

    ---
    name: extract_posting          # must equal the filename stem before ".vN"
    version: 1                     # must equal N in the filename
    provider: anthropic            # anthropic | openai
    model: claude-haiku-5-5
    max_tokens: 8000
    effort: low                    # optional
    description: One line.
    ---

    ## system
    ...system prompt...

    ## user
    ...user message template...

Both sections are `string.Template`s (`$posting`, `$taxonomy`, ...). We use
`string.Template` rather than `str.format` because the prompts talk about JSON and
`{`/`}` would all need escaping. The cost is that a literal dollar sign must be written
`$$` (e.g. `$$180K`). `parse_prompt` checks every template at load time, so a stray
`$180K` fails when the prompt is loaded, not halfway through a batch of extractions.
Frontmatter is parsed by hand (flat `key: value` lines) to avoid a YAML dependency.
"""

import re
from dataclasses import dataclass
from pathlib import Path
from string import Template

# src/jobradar/prompts.py -> parents: jobradar, src, pipeline, <repo root>
PROMPTS_DIR = Path(__file__).resolve().parents[3] / "prompts"

_FILENAME = re.compile(r"^(?P<name>[a-z0-9_]+)\.v(?P<version>[1-9][0-9]*)\.md$")
_REQUIRED_KEYS = frozenset({"name", "version", "provider", "model", "max_tokens", "description"})
_OPTIONAL_KEYS = frozenset({"effort"})
_PROVIDERS = frozenset({"anthropic", "openai"})
_SYSTEM_MARKER = "## system"
_USER_MARKER = "## user"


@dataclass(frozen=True)
class Prompt:
    """One prompt version. `system` and `user_template` are raw templates (not rendered)."""

    name: str
    version: int
    provider: str
    model: str
    max_tokens: int
    effort: str | None
    description: str
    system: str
    user_template: str

    def render(self, **values: str) -> tuple[str, str]:
        """Return (system, user) with placeholders filled.

        Raises KeyError listing every missing placeholder at once, so a caller fixes them
        in one go. Extra values are ignored: `taxonomy` is typically passed for every
        prompt, and a prompt is allowed not to use it.
        """
        templates = (Template(self.system), Template(self.user_template))
        needed = {ident for t in templates for ident in t.get_identifiers()}
        missing = sorted(needed - values.keys())
        if missing:
            raise KeyError(f"prompt {self.name} v{self.version} needs values for: {missing}")
        system, user = (t.substitute(values) for t in templates)
        return system, user


def parse_prompt(text: str, *, filename: str) -> Prompt:
    """Parse one prompt file's text. `filename` (no directory) is checked against the
    frontmatter so the name/version on disk and in `llm_runs` can't disagree."""
    match = _FILENAME.match(filename)
    if match is None:
        raise ValueError(f"prompt filename {filename!r} must look like <name>.v<N>.md")

    meta, body = _split_frontmatter(text, filename)
    system, user = _split_sections(body, filename)

    if meta["name"] != match["name"]:
        raise ValueError(f"{filename}: frontmatter name {meta['name']!r} != filename")
    version = _parse_int(meta, "version", filename)
    if version != int(match["version"]):
        raise ValueError(f"{filename}: frontmatter version {version} != filename")
    if meta["provider"] not in _PROVIDERS:
        raise ValueError(f"{filename}: provider must be one of {sorted(_PROVIDERS)}")

    for section, template in (("system", system), ("user", user)):
        if not Template(template).is_valid():
            raise ValueError(
                f"{filename}: invalid placeholder in the {section} section "
                "(write a literal dollar sign as $$)"
            )

    return Prompt(
        name=meta["name"],
        version=version,
        provider=meta["provider"],
        model=meta["model"],
        max_tokens=_parse_int(meta, "max_tokens", filename),
        effort=meta.get("effort") or None,
        description=meta["description"],
        system=system,
        user_template=user,
    )


def load_prompt(
    name: str, version: int | None = None, *, prompts_dir: Path = PROMPTS_DIR
) -> Prompt:
    """Load `<name>.v<version>.md`; `version=None` means the highest version on disk.

    "Highest" is compared numerically, so v10 beats v9.
    """
    if version is None:
        versions = sorted(
            int(m["version"])
            for path in prompts_dir.glob(f"{name}.v*.md")
            if (m := _FILENAME.match(path.name)) and m["name"] == name
        )
        if not versions:
            raise FileNotFoundError(f"no prompt named {name!r} in {prompts_dir}")
        version = versions[-1]
    path = prompts_dir / f"{name}.v{version}.md"
    if not path.is_file():
        raise FileNotFoundError(f"prompt {name!r} v{version} not found at {path}")
    return parse_prompt(path.read_text(encoding="utf-8"), filename=path.name)


def _split_frontmatter(text: str, filename: str) -> tuple[dict[str, str], str]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise ValueError(f"{filename}: must start with a '---' frontmatter block")
    try:
        end = next(i for i in range(1, len(lines)) if lines[i].strip() == "---")
    except StopIteration:
        raise ValueError(f"{filename}: frontmatter block is not closed with '---'") from None

    meta: dict[str, str] = {}
    for line in lines[1:end]:
        if not line.strip():
            continue
        key, sep, value = line.partition(":")
        key = key.strip()
        if not sep:
            raise ValueError(f"{filename}: frontmatter line {line!r} is not 'key: value'")
        if key not in _REQUIRED_KEYS | _OPTIONAL_KEYS:
            raise ValueError(f"{filename}: unknown frontmatter key {key!r}")
        if key in meta:
            raise ValueError(f"{filename}: duplicate frontmatter key {key!r}")
        meta[key] = value.strip()

    missing = sorted(_REQUIRED_KEYS - meta.keys())
    if missing:
        raise ValueError(f"{filename}: frontmatter is missing {missing}")
    return meta, "\n".join(lines[end + 1 :])


def _split_sections(body: str, filename: str) -> tuple[str, str]:
    """Split on the exact marker lines. Other headings ("## Fields") are prompt content."""
    lines = body.splitlines()
    markers = [(i, line.strip()) for i, line in enumerate(lines)]
    system_at = [i for i, m in markers if m == _SYSTEM_MARKER]
    user_at = [i for i, m in markers if m == _USER_MARKER]
    if len(system_at) != 1 or len(user_at) != 1 or system_at[0] > user_at[0]:
        raise ValueError(
            f"{filename}: body needs exactly one '{_SYSTEM_MARKER}' section "
            f"followed by exactly one '{_USER_MARKER}' section"
        )
    # Text before "## system" is ignored (a blank line after the frontmatter, usually).
    system = "\n".join(lines[system_at[0] + 1 : user_at[0]]).strip()
    user = "\n".join(lines[user_at[0] + 1 :]).strip()
    return system, user


def _parse_int(meta: dict[str, str], key: str, filename: str) -> int:
    try:
        value = int(meta[key])
    except ValueError:
        raise ValueError(f"{filename}: {key} must be an integer, got {meta[key]!r}") from None
    if value < 1:
        raise ValueError(f"{filename}: {key} must be positive")
    return value
