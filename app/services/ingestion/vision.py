"""Vision processing module for semantic image description.

This module provides functions to process extracted images using Vision Language Models (VLMs),
generate semantic descriptions, and seamlessly integrate them back into Markdown documents.
"""

import base64
import re
from io import BytesIO

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_google_genai import ChatGoogleGenerativeAI
from PIL import Image

from app.core.prompt_manager import PromptManager


def encode_image(img: Image.Image) -> str:
    """Encode a PIL Image object to a base64 string.

    Args:
        img (Image.Image): The PIL image to encode.

    Returns:
        str: The base64-encoded string representation of the image.
    """
    buffered = BytesIO()
    img.save(buffered, format="PNG")
    return base64.b64encode(buffered.getvalue()).decode("utf-8")


def generate_image_description(
    vlm: ChatGoogleGenerativeAI, image_base64: str, prompt_manager: PromptManager
) -> str:
    """Generate a semantic description for an image using the VLM.

    Args:
        vlm (ChatGoogleGenerativeAI): The initialized Vision Language Model.
        image_base64 (str): The base64-encoded string of the image.
        prompt_manager (PromptManager): Manager to load prompt templates.

    Returns:
        str: The generated Markdown description of the image.
    """
    # 1. Extract raw prompts without immediately formatting variables via str.format()
    prompt_data = prompt_manager.get("describe-image-prompt")
    system_prompt = prompt_data["system-prompt"]
    user_prompt = prompt_data["user-prompt"]

    # 2. Build the messages manually.
    # Using SystemMessage and HumanMessage avoids issues where ChatPromptTemplate
    # mistakenly parses curly braces (e.g. from LaTeX formulas in the prompt) as variables.
    messages = [
        SystemMessage(content=system_prompt),
        HumanMessage(
            content=[
                {"type": "text", "text": user_prompt},
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/png;base64,{image_base64}"},
                },
            ]
        ),
    ]

    # 3. Invoke model
    response = vlm.invoke(messages)
    return str(response.content).strip()


def enrich_markdown_with_vlm(markdown: str, image_descriptions: dict[str, str]) -> str:
    """Inject VLM-generated descriptions underneath corresponding image tags in Markdown.

    This function searches for Markdown image syntax `![alt](image_name)` and appends
    a blockquote containing the semantic description immediately after the image.

    Args:
        markdown (str): The original markdown document text containing image tags.
        image_descriptions (Dict[str, str]): A dictionary mapping image filenames to their
            VLM-generated descriptions.

    Returns:
        str: The enriched markdown document.
    """

    def replace_image(match: re.Match) -> str:
        img_tag = match.group(0)  # e.g: ![](_page_0_Figure_1.png)
        img_name = match.group(1)  # e.g: _page_0_Figure_1.png

        # Insert the description inside a blockquote beneath the image tag
        if img_name in image_descriptions:
            desc = image_descriptions[img_name]
            return f"{img_tag}\n\n> Image: {desc}\n"

        return img_tag

    # Regex pattern to match standard Markdown image tags: ![...](...)
    pattern = r"!\[.*?\]\((.*?)\)"
    enriched_markdown = re.sub(pattern, replace_image, markdown)
    return enriched_markdown
