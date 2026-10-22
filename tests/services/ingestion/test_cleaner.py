"""Unit tests for the ingestion Markdown cleaner module."""

from app.services.ingestion.cleaner import clean_markdown, clean_marker_markdown


def test_clean_markdown_empty_or_whitespace():
    assert clean_markdown("") == ""
    assert clean_markdown("   \n\n  ") == ""
    assert clean_markdown(None) == ""


def test_clean_markdown_protects_code_comments_and_locators():
    raw = """# Main Section
```python
#comment_without_space
#include <stdio.h>
##code_tag
x = 1
```
~~~bash
#bash_comment
echo "hello"
~~~
`inline_code()` with $x = 1$ math.
<!-- Slide number: 1 -->
<!-- page break -->
"""
    cleaned = clean_markdown(raw)

    assert "# Main Section" in cleaned
    # Ensure code contents and comments are 100% preserved
    assert "#comment_without_space" in cleaned
    assert "#include <stdio.h>" in cleaned
    assert "##code_tag" in cleaned
    assert "#bash_comment" in cleaned
    assert "`inline_code()`" in cleaned
    assert "$x = 1$" in cleaned
    # Ensure locators are untouched
    assert "<!-- Slide number: 1 -->" in cleaned
    assert "<!-- page break -->" in cleaned


def test_clean_markdown_page_artifacts_and_watermarks():
    raw = """Content before
Page 1
Trang 3/10
- 12 -
34
CONFIDENTIAL DRAFT
Content after
"""
    cleaned = clean_markdown(raw, watermarks=["CONFIDENTIAL DRAFT"])

    assert "Content before" in cleaned
    assert "Content after" in cleaned
    assert "Page 1" not in cleaned
    assert "Trang 3/10" not in cleaned
    assert "- 12 -" not in cleaned
    assert "34" not in cleaned
    assert "CONFIDENTIAL DRAFT" not in cleaned


def test_clean_markdown_hyphenation_and_line_wraps():
    # Soft hyphen break should rejoin ordinary words
    raw = "Đây là thông-\n tin quan trọng."
    cleaned = clean_markdown(raw)
    assert "thôngtin" in cleaned

    # Upper-case proper nouns like "Bà Rịa-\nVũng Tàu" should remain separate
    raw_proper = "Tỉnh Bà Rịa-\nVũng Tàu"
    cleaned_proper = clean_markdown(raw_proper)
    assert "Bà Rịa-\nVũng Tàu" in cleaned_proper

    # Soft line wrap rejoining when enabled
    wrapped = "Một câu dài\nđược tiếp tục ở đây."
    res_wrapped = clean_markdown(wrapped, rejoin_line_wraps=True)
    assert "Một câu dài được tiếp tục ở đây." in res_wrapped

    # Preserves bullet points even when rejoin_line_wraps is True
    bullets = "- Mục 1\n- Mục 2"
    assert clean_markdown(bullets, rejoin_line_wraps=True) == "- Mục 1\n- Mục 2"


def test_clean_markdown_formatting_and_whitespace():
    raw = "##Header without space\nLine with trailing space    \n\n\n\nToo many newlines"
    cleaned = clean_markdown(raw)
    assert "## Header without space" in cleaned
    assert "    " not in cleaned
    assert "\n\n\n" not in cleaned


def test_clean_markdown_preserves_currency():
    raw = "Prices are $10 and $20 each."
    cleaned = clean_markdown(raw)
    assert "$10 and $20" in cleaned


def test_clean_markdown_pipeline_and_alias():
    raw = """##Overview

Introductory text with\u00a0nbsp and zero-width\u200b char.
Page 2
<!-- Slide number: 5 -->
$$\\sum_{i=1}^n x_i = 10$$
```python
x = 1
#comment
```
"""
    cleaned = clean_markdown(raw)
    assert "## Overview" in cleaned
    assert "Page 2" not in cleaned
    assert "<!-- Slide number: 5 -->" in cleaned
    assert "$$\\sum_{i=1}^n x_i = 10$$" in cleaned
    assert "```python\nx = 1\n#comment\n```" in cleaned

    # Verify backwards-compatible alias
    assert clean_marker_markdown(raw) == cleaned
