"""Load and manage prompt templates from a YAML file."""

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from app.config.config import settings


class PromptManager:
    """Manage and retrieve prompt templates stored in a YAML configuration file.

    Attributes:
        prompt_path (Path): The path to the YAML file containing the prompts.
        prompts (dict[str, Any]): The dictionary containing all loaded prompts.
    """

    def __init__(self, prompt_path: str | Path | None = None) -> None:
        """Initialize the PromptManager.

        Args:
            prompt_path (str | Path | None): Path to the YAML file. Defaults to settings.prompt_file.
        """
        self.prompt_path = Path(prompt_path or settings.prompt_file)

        with self.prompt_path.open("r", encoding="utf-8") as file:
            self.prompts: dict[str, Any] = yaml.safe_load(file) or {}

    def get(self, name: str) -> dict[str, Any]:
        """Retrieve a prompt template dictionary by name.

        Args:
            name (str): The key name of the prompt in the YAML file.

        Raises:
            KeyError: If the prompt name is not found in the file.

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

        return {
            key: template.format(**kwargs) if isinstance(template, str) else template
            for key, template in prompt.items()
        }


@lru_cache(maxsize=1)
def get_prompt_manager(prompt_path: str | Path | None = None) -> PromptManager:
    """Singleton getter for PromptManager."""
    return PromptManager(prompt_path=prompt_path)
