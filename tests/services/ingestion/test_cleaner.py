
from app.services.ingestion.cleaner import (
    clean_hyphenation_and_wrapping,
    clean_latex_math,
    clean_markdown,
    clean_markdown_formatting,
    clean_marker_markdown,
    clean_page_artifacts,
    clean_unicode_and_ligatures,
    protect_special_blocks,
    restore_special_blocks,
)


def test_protect_and_restore_special_blocks():
    text = (
        "Intro\n"
        "```python\nprint('hello')\n```\n"
        "Formula: $$x^2 + y^2 = z^2$$\n"
        "Inline: $a + b = c$\n"
        "<!-- Slide number: 1 -->\n"
        "<!-- page break -->\n"
        "`inline_func()`\n"
        "End"
    )
    protected_text, placeholders = protect_special_blocks(text)

    assert "__PROTECTED_BLOCK_0__" in protected_text
    assert "print('hello')" not in protected_text
    assert "<!-- Slide number: 1 -->" not in protected_text

    restored = restore_special_blocks(protected_text, placeholders)
    assert restored == text


def test_clean_page_artifacts():
    raw = "Content\nPage 1\nMore content\n- 12 -\n34\nFinal"
    cleaned = clean_page_artifacts(raw)

    assert "Page 1" not in cleaned
    assert "- 12 -" not in cleaned
    assert "Final" in cleaned


def test_clean_unicode_and_ligatures():
    # Test fi ligature and curly quotes
    raw = "\ufb01le \u201chello\u201d \u00a0space"
    cleaned = clean_unicode_and_ligatures(raw)
    assert cleaned == 'file "hello"  space'


def test_clean_hyphenation_and_wrapping():
    # Broken word with hyphen across line
    raw = "thông-\n tin và tri thức"
    cleaned = clean_hyphenation_and_wrapping(raw)
    assert "thôngtin" in cleaned

    # With line wrapping enabled
    wrapped = "Một câu dài\nđược tiếp tục ở đây"
    res_wrapped = clean_hyphenation_and_wrapping(wrapped, rejoin_line_wraps=True)
    assert "Một câu dài được tiếp tục ở đây" in res_wrapped


def test_clean_latex_math():
    raw = "$a$ + $b$ = $c$"
    cleaned = clean_latex_math(raw)
    assert cleaned == "$a + b = c$"


def test_clean_markdown_formatting():
    raw = "##Header without space\nLine with space   \n\n\n\nToo many newlines"
    cleaned = clean_markdown_formatting(raw)
    assert "## Header without space" in cleaned
    assert "\n\n\n" not in cleaned


def test_clean_markdown_pipeline():
    raw = """##Header

\ufb01rst paragraph with ligature and text.
- 1 -
<!-- Slide number: 1 -->
$$\\sum_{i=1}^n x_i$$
```python
x = 1
# keep this comment
```
"""
    cleaned = clean_markdown(raw)
    assert "## Header" in cleaned
    assert "first paragraph" in cleaned
    assert "- 1 -" not in cleaned
    assert "<!-- Slide number: 1 -->" in cleaned
    assert "```python\nx = 1\n# keep this comment\n```" in cleaned

    # Verify alias works
    assert clean_marker_markdown(raw) == cleaned