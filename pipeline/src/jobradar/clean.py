"""Turn ATS description HTML into plain text split into numbered paragraphs.

Why paragraphs: in M2 the extraction LLM must cite the paragraph IDs its facts
come from, and code then checks the citation (for example, that a salary number
actually appears in the cited paragraph's text). That only works if:

- paragraphs are meaningful units: one bullet is one paragraph, a heading is
  its own paragraph, and words from different blocks never glue together;
- the text is faithful: we only remove markup and normalize whitespace. We
  never add, reword or "fix" characters, so "$180,000 &ndash; $220,000" keeps its
  en dash (U+2013) after unescaping;
- IDs are positions (1-based), so the same HTML always gets the same IDs.

Why the hash covers what it covers (D-006): "edited" should mean "the
extraction might come out differently", so `content_hash` covers exactly what
the LLM reads: the cleaned title, the locations and the cleaned text. Escaping
changes, stray whitespace and location reordering are not edits.

Stdlib only: `html.parser.HTMLParser` is forgiving enough for ATS HTML and
keeps this module free of new dependencies.
"""

import hashlib
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any

# Bump whenever the output for the same input HTML can change (new block tag,
# different bullet prefix, different whitespace rule, ...). A bump changes
# cleaned_text, and therefore content_hash, for many postings at once; storing
# the version next to the hash (D-006) is what makes that mass change explainable.
CLEANER_VERSION: int = 1

# Tags that start/end a paragraph. Lists and tables are handled separately below
# but their outer tags are boundaries too.
_BLOCK_TAGS = frozenset(
    {
        "address",
        "article",
        "aside",
        "blockquote",
        "caption",
        "center",
        "dd",
        "details",
        "div",
        "dl",
        "dt",
        "fieldset",
        "figcaption",
        "figure",
        "footer",
        "form",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "header",
        "hr",
        "main",
        "nav",
        "p",
        "pre",
        "section",
        "summary",
        "table",
        "tbody",
        "tfoot",
        "thead",
    }
)
_LIST_TAGS = frozenset({"ul", "ol", "menu"})
_CELL_TAGS = frozenset({"td", "th"})
# Contents of these are never text a reader sees.
_SKIP_TAGS = frozenset({"script", "style", "noscript", "template"})

BULLET_PREFIX = "- "
CELL_SEPARATOR = " | "

# Zero-width characters are invisible but would break "does this number appear
# in the cited paragraph" substring checks, so they are removed. ZWJ (U+200D)
# is kept because it is part of emoji sequences.
_ZERO_WIDTH = str.maketrans("", "", "\u200b\u2060\ufeff")
# For str patterns, \s matches all Unicode whitespace, including U+00A0 (&nbsp;),
# thin spaces and ideographic spaces.
_WHITESPACE_RUN = re.compile(r"\s+")


def _normalize(text: str) -> str:
    return _WHITESPACE_RUN.sub(" ", text.translate(_ZERO_WIDTH)).strip()


@dataclass(frozen=True)
class Paragraph:
    id: int  # 1-based position in the description (D12)
    text: str

    def to_json(self) -> dict[str, Any]:
        return {"id": self.id, "text": self.text}


@dataclass(frozen=True)
class CleanedDescription:
    text: str  # "\n\n".join(p.text for p in paragraphs)
    paragraphs: list[Paragraph]


class _ParagraphCollector(HTMLParser):
    """Walks the HTML once, cutting text into paragraphs at block boundaries."""

    def __init__(self) -> None:
        # convert_charrefs=True: entities arrive in handle_data already unescaped.
        super().__init__(convert_charrefs=True)
        self.paragraphs: list[str] = []
        self._buf: list[str] = []
        self._skip_depth = 0
        # One entry per open <ul>/<ol>: True while an <li> is open in it. Text is
        # a bullet if any enclosing list has an open item (covers nesting and
        # text after a nested list inside an outer <li>).
        self._lists: list[bool] = []
        # Cells of the open <tr>, or None when not inside a row.
        self._cells: list[str] | None = None

    # -- helpers ---------------------------------------------------------------

    def _flush(self) -> None:
        """End the current paragraph; empty paragraphs are dropped."""
        text = _normalize("".join(self._buf))
        self._buf = []
        if not text:
            return
        if any(self._lists):
            text = BULLET_PREFIX + text
        self.paragraphs.append(text)

    def _commit_cell(self) -> None:
        if self._cells is None:
            return
        text = _normalize("".join(self._buf))
        self._buf = []
        if text:  # empty layout cells would only add " |  | " noise
            self._cells.append(text)

    def _end_row(self) -> None:
        if self._cells is None:
            return
        self._commit_cell()
        row = CELL_SEPARATOR.join(self._cells)
        self._cells = None
        if row:
            self.paragraphs.append(row)

    def _boundary(self) -> None:
        """A block boundary: a paragraph break, or just a space inside a table row."""
        if self._cells is not None:
            # Blocks inside a cell (<p>, <br>, <div>) must not split the row, but
            # must not glue words either.
            self._buf.append(" ")
        else:
            self._flush()

    # -- HTMLParser callbacks --------------------------------------------------

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        if tag == "tr":
            self._end_row()  # also closes an unclosed previous row
            self._flush()
            self._cells = []
        elif tag in _CELL_TAGS:
            if self._cells is None:  # a cell outside any row: treat as a block
                self._flush()
            else:
                self._commit_cell()
        elif tag in _LIST_TAGS:
            self._boundary()
            self._lists.append(False)
        elif tag == "li":
            self._boundary()
            if not self._lists:  # <li> with no list around it
                self._lists.append(False)
            self._lists[-1] = True
        elif tag == "br":
            # Job posts often use <br> as a poor-man's paragraph break, and a
            # finer split only makes citations more precise, so a single <br>
            # ends the paragraph. <br><br> yields an empty paragraph, which is
            # dropped.
            self._boundary()
        elif tag in _BLOCK_TAGS:
            if tag == "table":
                self._end_row()
            self._boundary()
        # Anything else is inline: it contributes only its text.

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if self._skip_depth:
            return
        if tag == "tr":
            self._end_row()
        elif tag in _CELL_TAGS:
            if self._cells is None:
                self._flush()
            else:
                self._commit_cell()
        elif tag in _LIST_TAGS:
            self._boundary()
            if self._lists:
                self._lists.pop()
        elif tag == "li":
            self._boundary()
            if self._lists:
                self._lists[-1] = False
        elif tag == "br":  # browsers treat a stray </br> like <br>
            self._boundary()
        elif tag in _BLOCK_TAGS:
            if tag == "table":
                self._end_row()
            self._boundary()

    def handle_data(self, data: str) -> None:
        if not self._skip_depth:
            self._buf.append(data)

    def finish(self) -> list[str]:
        self.close()  # flushes any text HTMLParser was still buffering
        self._end_row()
        self._flush()
        return self.paragraphs


def clean_description(html: str) -> CleanedDescription:
    """Strip markup from `html` and split it into numbered paragraphs.

    Deterministic: the same HTML always yields the same text and paragraph IDs.
    """
    collector = _ParagraphCollector()
    collector.feed(html)
    paragraphs = [Paragraph(id=i, text=t) for i, t in enumerate(collector.finish(), start=1)]
    return CleanedDescription(
        text="\n\n".join(p.text for p in paragraphs),
        paragraphs=paragraphs,
    )


def content_hash(title: str, locations: Sequence[str], cleaned_text: str) -> str:
    """sha256 hex over exactly what the extraction LLM reads (D-006).

    Normalization: whitespace in the title and each location is collapsed,
    empty locations are dropped, and locations are sorted so an ATS reordering
    them is not an "edit". (M2 should therefore show the LLM locations in this
    same sorted order.) `cleaned_text` is hashed as given: it is already
    normalized by `clean_description`.

    Serialization is a JSON array, so field boundaries are unambiguous:
    ("a b", ["c"]) and ("a", ["b c"]) cannot produce the same bytes.

    CLEANER_VERSION is deliberately *not* inside the hash. It is stored next to
    it instead. A cleaner change that alters a posting's text already changes
    the hash through `cleaned_text`; putting the version inside would also mark
    every untouched posting as edited on each bump and trigger re-extractions
    (real money) for inputs the LLM would see identically.
    """
    normalized_locations = sorted(n for n in (_normalize(loc) for loc in locations) if n)
    payload = json.dumps(
        [_normalize(title), normalized_locations, cleaned_text],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
