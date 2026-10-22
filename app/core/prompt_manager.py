"""Load and manage prompt templates from YAML files or directories."""

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from app.config.config import settings


class PromptManager:
    """Manage and retrieve prompt templates stored in YAML files or a directory.

    Attributes:
        prompt_path (Path): The path to the YAML file or directory containing prompts.
        prompts (dict[str, Any]): The dictionary containing all loaded prompts.
    """

    def __init__(self, prompt_path: str | Path | None = None) -> None:
        """Initialize the PromptManager.

        Args:
            prompt_path (str | Path | None): Path to a YAML file or directory.
                Defaults to settings.prompt_file (or prompt_dir if configured).
        """
        raw_path = prompt_path or getattr(settings, "prompt_file", None)
        self.prompt_path = Path(raw_path) if raw_path else Path(__file__).resolve().parent.parent / "config" / "prompts"

        self.prompts: dict[str, Any] = {}
        self._load_prompts()

    def _load_prompts(self) -> None:
        """Load prompts from a single file, a directory of YAML files, or fallback directory."""
        # 1. If path is a directory, load all .yaml / .yml files inside
        if self.prompt_path.is_dir():
            for yaml_file in sorted(self.prompt_path.glob("*.y*ml")):
                with yaml_file.open("r", encoding="utf-8") as file:
                    data = yaml.safe_load(file) or {}
                    self.prompts.update(data)
            return

        # 2. If path is an existing file, load directly
        if self.prompt_path.is_file():
            with self.prompt_path.open("r", encoding="utf-8") as file:
                self.prompts = yaml.safe_load(file) or {}
            return

        # 3. If file path doesn't exist, check if a prompts/ directory exists in the same folder
        fallback_dir = self.prompt_path.parent / "prompts"
        if fallback_dir.is_dir():
            for yaml_file in sorted(fallback_dir.glob("*.y*ml")):
                with yaml_file.open("r", encoding="utf-8") as file:
                    data = yaml.safe_load(file) or {}
                    self.prompts.update(data)
            return

        raise FileNotFoundError(f"Prompts location not found: {self.prompt_path}")

    def get(self, name: str) -> dict[str, Any]:
        """Retrieve a prompt template dictionary by name.

        Args:
            name (str): The key name of the prompt in the YAML file.

        Raises:
            KeyError: If the prompt name is not found.

        Returns:
            dict[str, Any]: The prompt template containing its sub-keys.
        """
        if name not in self.prompts:
            raise KeyError(f"Prompt '{name}' not found in {self.prompt_path}.")

        return self.prompts[name]

    def format(self, name: str, **kwargs: Any) -> dict[str, Any]:
        """Format a prompt template's strings using the provided keyword arguments.

        Args:
            name (str): The key name of the prompt.
            **kwargs (Any): Keyword arguments to format the prompt strings.

        Returns:
            dict[str, Any]: The prompt dictionary with all string templates formatted.
        """
        prompt = self.get(name)

        # Ensure backward/forward compatibility between `chunk` and `context`
        formatted_args = dict(kwargs)
        if "context" in formatted_args and "chunk" not in formatted_args:
            formatted_args["chunk"] = formatted_args["context"]
        elif "chunk" in formatted_args and "context" not in formatted_args:
            formatted_args["context"] = formatted_args["chunk"]

        return {
            key: template.format(**formatted_args) if isinstance(template, str) else template
            for key, template in prompt.items()
        }


@lru_cache(maxsize=1)
def get_prompt_manager(prompt_path: str | Path | None = None) -> PromptManager:
    """Singleton getter for PromptManager."""
    return PromptManager(prompt_path=prompt_path)
