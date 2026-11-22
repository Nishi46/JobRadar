from jobradar.clean import (
    CLEANER_VERSION,
    CleanedDescription,
    Paragraph,
    clean_description,
    content_hash,
)


def texts(html: str) -> list[str]:
    return [p.text for p in clean_description(html).paragraphs]


# --- paragraphs ---------------------------------------------------------------


def test_simple_paragraphs_get_sequential_ids_from_one() -> None:
    result = clean_description("<p>First.</p><p>Second.</p><p>Third.</p>")

    assert result.paragraphs == [
        Paragraph(id=1, text="First."),
        Paragraph(id=2, text="Second."),
        Paragraph(id=3, text="Third."),
    ]


def test_paragraph_to_json_matches_stored_shape() -> None:
    assert Paragraph(id=1, text="Hi").to_json() == {"id": 1, "text": "Hi"}


def test_bullets_are_prefixed_and_one_per_paragraph() -> None:
    html = "<p>You have:</p><ul><li>Python</li><li>SQL</li></ul><ol><li>One</li></ol>"

    assert texts(html) == ["You have:", "- Python", "- SQL", "- One"]


def test_adjacent_list_items_do_not_glue() -> None:
    assert texts("<ul><li>a</li><li>b</li></ul>") == ["- a", "- b"]


def test_unclosed_list_items_still_split() -> None:
    assert texts("<ul><li>a<li>b</ul><p>after</p>") == ["- a", "- b", "after"]


def test_nested_list_items_each_become_a_bullet() -> None:
    html = "<ul><li>Outer<ul><li>Inner</li></ul></li><li>Next</li></ul>"

    assert texts(html) == ["- Outer", "- Inner", "- Next"]


def test_headings_are_their_own_paragraphs() -> None:
    html = "<h2>About us</h2><p>We build things.</p><h3>Requirements</h3>"

    assert texts(html) == ["About us", "We build things.", "Requirements"]


def test_br_splits_paragraphs_and_double_br_leaves_no_empty_one() -> None:
    html = "<p>Line one<br>Line two<br/><br />Line three</p>"

    assert texts(html) == ["Line one", "Line two", "Line three"]


def test_nested_divs_do_not_create_empty_paragraphs() -> None:
    html = "<div><div><div><p>Only text</p></div></div></div><div></div><p></p><p> </p>"

    assert texts(html) == ["Only text"]


def test_text_directly_inside_div_next_to_block_is_split() -> None:
    assert texts("<div>Intro<p>Para</p>Outro</div>") == ["Intro", "Para", "Outro"]


def test_entities_and_nbsp_are_unescaped_to_plain_text() -> None:
    html = "<p>R&amp;D&nbsp;team &lt;3 &quot;fast&quot; caf&eacute;&#39;s</p>"

    assert texts(html) == ['R&D team <3 "fast" café\'s']


def test_unicode_whitespace_and_newlines_collapse_to_single_spaces() -> None:
    html = "<p>\n   Build\u00a0\u00a0\u2009 the\t\tthing \r\n  now  </p>"

    assert texts(html) == ["Build the thing now"]


def test_script_style_and_noscript_contents_are_dropped() -> None:
    html = (
        "<style>p { color: red; }</style><p>Keep</p>"
        "<script>var x = '<p>no</p>';</script><noscript><p>Enable JS</p></noscript>"
    )

    assert texts(html) == ["Keep"]


def test_inline_tags_neither_glue_nor_split_words() -> None:
    html = '<p>We use <b>Python</b>, <em>Go</em> and <a href="x">Rust</a>.</p>'

    assert texts(html) == ["We use Python, Go and Rust."]


def test_inline_tag_without_spaces_stays_as_in_source() -> None:
    assert texts("<p><b>Python</b>and</p>") == ["Pythonand"]
    assert texts("<p>sala<span>ry</span></p>") == ["salary"]


def test_table_cells_in_a_row_are_joined_with_pipes() -> None:
    html = (
        "<table><tr><th>Level</th><th>Pay</th></tr>"
        "<tr><td>Senior</td><td><p>$200k</p></td></tr></table>"
    )

    assert texts(html) == ["Level | Pay", "Senior | $200k"]


def test_empty_input_gives_no_paragraphs_and_empty_text() -> None:
    assert clean_description("") == CleanedDescription(text="", paragraphs=[])
    assert clean_description("<p></p><br><div> </div>") == CleanedDescription(
        text="", paragraphs=[]
    )


def test_text_is_paragraphs_joined_by_blank_lines() -> None:
    result = clean_description("<h2>Role</h2><p>Do work.</p><ul><li>Python</li></ul>")

    assert result.text == "Role\n\nDo work.\n\n- Python"
    assert result.text == "\n\n".join(p.text for p in result.paragraphs)


def test_plain_text_without_tags_is_one_paragraph() -> None:
    assert texts("Just some text") == ["Just some text"]


def test_salary_line_survives_verbatim_with_en_dash() -> None:
    result = clean_description("<p>$180,000 &ndash; $220,000 USD</p>")

    assert result.paragraphs == [Paragraph(id=1, text="$180,000 \u2013 $220,000 USD")]


def test_cleaning_is_deterministic() -> None:
    html = "<div><h1>Title</h1><ul><li>a</li><li>b<br>c</li></ul><p>x&nbsp;y</p></div>"

    assert clean_description(html) == clean_description(html)


# --- content_hash -------------------------------------------------------------


def test_hash_is_sha256_hex() -> None:
    h = content_hash("Engineer", ["Remote"], "text")

    assert len(h) == 64
    assert int(h, 16) >= 0


def test_hash_is_stable_across_location_reorder_and_whitespace() -> None:
    base = content_hash("ML Engineer", ["NYC", "SF"], "text")

    assert content_hash("  ML   Engineer\n", [" SF ", "NYC", ""], "text") == base


def test_hash_changes_when_title_location_or_text_changes() -> None:
    base = content_hash("ML Engineer", ["NYC"], "text")

    assert content_hash("ML Engineer II", ["NYC"], "text") != base
    assert content_hash("ML Engineer", ["SF"], "text") != base
    assert content_hash("ML Engineer", ["NYC", "SF"], "text") != base
    assert content_hash("ML Engineer", ["NYC"], "text!") != base


def test_hash_does_not_collide_across_field_boundaries() -> None:
    assert content_hash("a b", ["c"], "t") != content_hash("a", ["b c"], "t")
    assert content_hash("t", ["a", "b"], "x") != content_hash("t", ["a, b"], "x")
    assert content_hash("t", ["NYC"], "") != content_hash("t", [], "NYC")


def test_hash_is_deterministic() -> None:
    assert content_hash("T", ["L"], "X") == content_hash("T", ["L"], "X")


def test_cleaner_version_is_a_positive_int() -> None:
    assert isinstance(CLEANER_VERSION, int)
    assert CLEANER_VERSION >= 1
