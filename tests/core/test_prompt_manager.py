"""Tests for PromptManager."""

from pathlib import Path

import pytest

from app.core.prompt_manager import PromptManager, get_prompt_manager


def test_prompt_manager_loads_valid_yaml(tmp_path: Path):
    """Ensure PromptManager parses valid prompt YAML file correctly."""
    prompt_file = tmp_path / "test_prompt.yaml"
    prompt_file.write_text(
        "sample-prompt:\n  system-prompt: 'Hello System'\n  user-prompt: 'Hello User'\n",
        encoding="utf-8",
    )

    manager = PromptManager(prompt_file)
    data = manager.get("sample-prompt")

    assert data["system-prompt"] == "Hello System"
    assert data["user-prompt"] == "Hello User"


def test_prompt_manager_format(tmp_path: Path):
    """Ensure PromptManager formats templates with keyword arguments."""
    prompt_file = tmp_path / "test_prompt.yaml"
    prompt_file.write_text(
        "qa-prompt:\n  system: 'Course: {course_name}'\n  user: 'Question: {question}'\n  count: 3\n",
        encoding="utf-8",
    )

    manager = PromptManager(prompt_file)
    formatted = manager.format("qa-prompt", course_name="Python 101", question="What is a loop?")

    assert formatted["system"] == "Course: Python 101"
    assert formatted["user"] == "Question: What is a loop?"
    assert formatted["count"] == 3


def test_prompt_manager_key_not_found(tmp_path: Path):
    """Ensure PromptManager raises KeyError when a key does not exist."""
    prompt_file = tmp_path / "test_prompt.yaml"
    prompt_file.write_text("existing-key:\n  text: 'some text'\n", encoding="utf-8")

    manager = PromptManager(prompt_file)
    with pytest.raises(KeyError, match="Prompt 'non-existent' not found in"):
        manager.get("non-existent")


def test_prompt_manager_file_not_found(tmp_path: Path):
    """Ensure PromptManager raises FileNotFoundError for missing file."""
    with pytest.raises(FileNotFoundError):
        PromptManager(tmp_path / "missing.yaml")


def test_prompt_manager_loads_default_config_file():
    """Ensure default PromptManager loads describe-image-prompt from app/config/prompt.yaml."""
    manager = get_prompt_manager()
    data = manager.get("describe-image-prompt")

    assert "system-prompt" in data
    assert "user-prompt" in data


def test_prompt_manager_loads_directory_of_yamls(tmp_path: Path):
    """Ensure PromptManager merges all yaml files inside a directory."""
    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir()

    (prompts_dir / "p1.yaml").write_text("prompt-one:\n  text: 'One'\n", encoding="utf-8")
    (prompts_dir / "p2.yaml").write_text("prompt-two:\n  text: 'Two'\n", encoding="utf-8")

    manager = PromptManager(prompts_dir)
    assert manager.get("prompt-one")["text"] == "One"
    assert manager.get("prompt-two")["text"] == "Two"

