"""Tests for mm_bridge.directives — the VibeDeck directive parser."""

from __future__ import annotations

from mm_bridge.directives import Directive, extract


def test_single_open_file() -> None:
    cleaned, directives = extract('<openFile path="src/a.py" />')
    assert cleaned == ""
    assert directives == [Directive("open_file", {"path": "src/a.py"})]


def test_multiple_open_files_preserve_between_text() -> None:
    cleaned, directives = extract(
        'Check <openFile path="a.py"/> and <openFile path="b.py"/>'
    )
    assert cleaned == "Check  and "
    assert directives == [
        Directive("open_file", {"path": "a.py"}),
        Directive("open_file", {"path": "b.py"}),
    ]


def test_open_file_with_line_attr() -> None:
    cleaned, directives = extract('<openFile path="x.py" line="42" />')
    assert cleaned == ""
    assert len(directives) == 1
    assert directives[0].kind == "open_file"
    assert directives[0].attrs == {"path": "x.py", "line": "42"}


def test_leave_channel_without_reason() -> None:
    cleaned, directives = extract("bye <leaveChannel />")
    assert cleaned == "bye "
    assert directives == [Directive("leave_channel", {})]


def test_leave_channel_with_reason() -> None:
    cleaned, directives = extract('<leaveChannel reason="done" />')
    assert cleaned == ""
    assert directives == [Directive("leave_channel", {"reason": "done"})]


def test_mixed_directives_keep_order() -> None:
    cleaned, directives = extract(
        '<openFile path="a"/> text <leaveChannel reason="x"/>'
    )
    assert cleaned == " text "
    assert [d.kind for d in directives] == ["open_file", "leave_channel"]
    assert directives[0].attrs == {"path": "a"}
    assert directives[1].attrs == {"reason": "x"}


def test_no_directives_returns_original_text() -> None:
    cleaned, directives = extract("just text")
    assert cleaned == "just text"
    assert directives == []


def test_case_insensitive_tag_names() -> None:
    cleaned, directives = extract('<OPENFILE path="x"/>')
    assert cleaned == ""
    assert directives == [Directive("open_file", {"path": "x"})]


def test_case_insensitive_leave_channel() -> None:
    cleaned, directives = extract("<LEAVECHANNEL />")
    assert cleaned == ""
    assert directives == [Directive("leave_channel", {})]


def test_open_file_inside_plain_fence_not_extracted() -> None:
    text = '```\n<openFile path="a"/>\n```'
    cleaned, directives = extract(text)
    assert directives == []
    assert cleaned == text


def test_open_file_inside_language_tagged_fence_not_extracted() -> None:
    text = '```xml\n<openFile path="a"/>\n```'
    cleaned, directives = extract(text)
    assert directives == []
    assert cleaned == text


def test_leave_channel_inside_plain_fence_not_extracted() -> None:
    text = '```\n<leaveChannel reason="done"/>\n```'
    cleaned, directives = extract(text)
    assert directives == []
    assert cleaned == text


def test_leave_channel_inside_language_tagged_fence_not_extracted() -> None:
    text = '```md\n<leaveChannel />\n```'
    cleaned, directives = extract(text)
    assert directives == []
    assert cleaned == text


def test_open_file_inside_inline_code_span_not_extracted() -> None:
    text = 'Use `<openFile path="a"/>` to attach files.'
    cleaned, directives = extract(text)
    assert directives == []
    assert cleaned == text


def test_leave_channel_inside_inline_code_span_not_extracted() -> None:
    text = 'Emit `<leaveChannel reason="bye"/>` to leave.'
    cleaned, directives = extract(text)
    assert directives == []
    assert cleaned == text


def test_directive_outside_fence_still_extracted_when_fence_present() -> None:
    text = (
        'Example below:\n'
        '```\n'
        '<openFile path="example.py"/>\n'
        '```\n'
        'Now actually do it: <openFile path="real.py"/>'
    )
    cleaned, directives = extract(text)
    assert directives == [Directive("open_file", {"path": "real.py"})]
    # Fenced block preserved verbatim, real directive stripped.
    assert '```\n<openFile path="example.py"/>\n```' in cleaned
    assert "real.py" not in cleaned


def test_directive_outside_code_span_still_extracted_when_span_present() -> None:
    text = (
        'The directive `<openFile path="x"/>` works like this: '
        '<openFile path="real.py"/>'
    )
    cleaned, directives = extract(text)
    assert directives == [Directive("open_file", {"path": "real.py"})]
    assert '`<openFile path="x"/>`' in cleaned
    assert "real.py" not in cleaned


def test_extra_attrs_preserved() -> None:
    cleaned, directives = extract('<openFile path="a" follow="true"/>')
    assert cleaned == ""
    assert directives == [
        Directive("open_file", {"path": "a", "follow": "true"})
    ]


def test_open_file_with_no_attrs_is_not_filtered_here() -> None:
    # Regex for openFile requires \s+ before attrs so a truly empty
    # "<openFile/>" does NOT match — the JS reference has the same constraint.
    # But an openFile with whitespace-only inner content *does* match with
    # empty attrs, and the parser must not filter it (the caller checks path).
    cleaned, directives = extract("<openFile />")
    assert cleaned == ""
    assert directives == [Directive("open_file", {})]


def test_collapses_blank_line_runs_from_stripping() -> None:
    text = 'before\n\n<openFile path="a"/>\n\nafter'
    cleaned, directives = extract(text)
    assert directives == [Directive("open_file", {"path": "a"})]
    # The stripped directive leaves "before\n\n\n\nafter" — collapse to one blank.
    assert cleaned == "before\n\nafter"


def test_text_between_directives_preserved_verbatim() -> None:
    text = '<openFile path="a"/>middle<openFile path="b"/>'
    cleaned, directives = extract(text)
    assert cleaned == "middle"
    assert [d.attrs["path"] for d in directives] == ["a", "b"]


# ── <state kind="..." on="..." note="..."/> — the turn-end state directive ──
# Same fence-aware machinery as the other two: the tag is documented in
# `CLAUDE-include.md` inside fences, and agents quote it when explaining
# themselves, so a fenced tag must stay visible text and set nothing.


def test_state_directive_minimal() -> None:
    cleaned, directives = extract('<state kind="idle" />')
    assert cleaned == ""
    assert directives == [Directive("state", {"kind": "idle"})]


def test_state_directive_full_attrs() -> None:
    cleaned, directives = extract(
        'M0 done.\n<state kind="awaiting" on="lead" note="M0 gate" />'
    )
    assert cleaned == "M0 done.\n"
    assert directives == [
        Directive("state", {"kind": "awaiting", "on": "lead", "note": "M0 gate"}),
    ]


def test_state_directive_case_insensitive() -> None:
    _, directives = extract('<STATE KIND="parked" />')
    assert directives[0].kind == "state"


def test_state_directive_inside_plain_fence_not_extracted() -> None:
    text = 'Use it like:\n```\n<state kind="awaiting" on="lead" />\n```\n'
    cleaned, directives = extract(text)
    assert cleaned == text
    assert directives == []


def test_state_directive_inside_inline_code_span_not_extracted() -> None:
    text = 'End your reply with `<state kind="parked" />` to park.'
    cleaned, directives = extract(text)
    assert cleaned == text
    assert directives == []


def test_two_state_directives_keep_textual_order() -> None:
    """Last-tag-wins is decided by the bridge; the parser preserves order."""
    _, directives = extract(
        'was <state kind="idle"/> now <state kind="blocked" note="quota"/>'
    )
    assert [d.attrs.get("kind") for d in directives] == ["idle", "blocked"]


def test_state_mixed_with_other_directives_keeps_order() -> None:
    cleaned, directives = extract(
        '<openFile path="a.py"/> done <state kind="awaiting" on="tijs"/>'
    )
    assert cleaned == " done "
    assert [d.kind for d in directives] == ["open_file", "state"]
