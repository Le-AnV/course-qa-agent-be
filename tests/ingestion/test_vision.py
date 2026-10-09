import base64
from unittest.mock import MagicMock

from langchain_core.messages import HumanMessage, SystemMessage
from PIL import Image

from app.services.ingestion.vision import (
    encode_image,
    enrich_markdown_with_vlm,
    generate_image_description,
)


def test_encode_image():
    # Create a small RGB test image
    img = Image.new("RGB", (20, 20), color="blue")
    b64_str = encode_image(img)

    assert isinstance(b64_str, str)
    assert len(b64_str) > 0

    # Verify that base64 string decodes to valid PNG bytes
    decoded = base64.b64decode(b64_str)
    assert decoded.startswith(b"\x89PNG\r\n\x1a\n")


def test_generate_image_description():
    mock_vlm = MagicMock()
    mock_vlm.invoke.return_value.content = "  A flowchart showing system architecture.  "

    mock_prompt_manager = MagicMock()
    mock_prompt_manager.get.return_value = {
        "system-prompt": "You are an image description assistant.",
        "user-prompt": "Describe this image in markdown.",
    }

    result = generate_image_description(
        vlm=mock_vlm,
        image_base64="sample_base64_string",
        prompt_manager=mock_prompt_manager,
    )

    # Asserts return content is stripped
    assert result == "A flowchart showing system architecture."

    # Asserts prompt_manager was queried
    mock_prompt_manager.get.assert_called_once_with("describe-image-prompt")

    # Asserts vlm.invoke was called with correctly structured messages
    mock_vlm.invoke.assert_called_once()
    call_args = mock_vlm.invoke.call_args[0][0]
    assert len(call_args) == 2

    system_msg = call_args[0]
    assert isinstance(system_msg, SystemMessage)
    assert system_msg.content == "You are an image description assistant."

    human_msg = call_args[1]
    assert isinstance(human_msg, HumanMessage)
    assert isinstance(human_msg.content, list)
    assert human_msg.content[0] == {"type": "text", "text": "Describe this image in markdown."}
    assert human_msg.content[1] == {
        "type": "image_url",
        "image_url": {"url": "data:image/png;base64,sample_base64_string"},
    }


def test_enrich_markdown_with_vlm_matching_image():
    markdown = "## Section 1\n\n![Architecture Diagram](arch.png)\n\nSummary text."
    descriptions = {"arch.png": "Detailed diagram of microservices."}

    enriched = enrich_markdown_with_vlm(markdown, descriptions)

    assert "> Image: Detailed diagram of microservices." in enriched
    assert "![Architecture Diagram](arch.png)\n\n> Image: Detailed diagram of microservices.\n" in enriched
    assert "Summary text." in enriched


def test_enrich_markdown_with_vlm_unmatched_image():
    markdown = "![Unmatched Image](missing.png)"
    descriptions = {"other_image.png": "Description for something else"}

    enriched = enrich_markdown_with_vlm(markdown, descriptions)

    # When no matching description exists, markdown remains untouched
    assert enriched == markdown


def test_enrich_markdown_with_vlm_multiple_images():
    markdown = "![Img 1](img1.png)\n\nText\n\n![Img 2](img2.png)\n\n![Img 3](img3.png)"
    descriptions = {
        "img1.png": "Description for 1",
        "img3.png": "Description for 3",
    }

    enriched = enrich_markdown_with_vlm(markdown, descriptions)

    assert "> Image: Description for 1" in enriched
    assert "> Image: Description for 3" in enriched
    assert "> Image: Description for 2" not in enriched
    assert "![Img 2](img2.png)" in enriched
