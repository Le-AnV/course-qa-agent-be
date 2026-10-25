from typing import Literal

from pydantic import BaseModel, Field


class CitationLocation(BaseModel):
    """Locator information for jumping to exact document sections."""

    page_number: int | None = Field(None, description="1-based page index for PDF documents")
    slide_number: int | None = Field(None, description="1-based slide index for PPTX presentations")
    paragraph_index: int | None = Field(None, description="Paragraph index for DOCX documents")
    chapter: str | None = Field(None, description="Chapter or section name from syllabus/slides")
    class_code: str | None = Field(None, description="Class code or identifier")


class CitationItem(BaseModel):
    """A single citation referencing an internal document or external web source."""

    citation_id: int = Field(..., description="Sequential reference index starting at 1")
    source_type: Literal["curriculum", "internet"] = Field(..., description="Source categorization")
    document_name: str = Field(..., description="File name or web page title")
    document_type: Literal["pdf", "pptx", "docx", "web"] = Field(..., description="Document format")
    file_url: str | None = Field(None, description="Internal storage URL or download path")
    location: CitationLocation = Field(default_factory=CitationLocation)
    highlight_quote: str = Field(..., description="Verbatim quote from source text for frontend string matching")
    jump_url: str | None = Field(None, description="Full external URL containing #:~:text= for web links")


class ChatResponseWithCitations(BaseModel):
    """Structured Q&A response payload from Gemini LLM."""

    answer: str = Field(..., description="Markdown response containing inline tags like [1], [2]")
    citations: list[CitationItem] = Field(default_factory=list, description="Referenced source citations")


class CitationExcerptResponse(BaseModel):
    """Excerpt payload returned when inspecting a citation."""

    citation_id: int
    document_name: str
    document_type: str
    location: CitationLocation
    highlight_quote: str
    surrounding_text: str | None = None
    jump_url: str | None = None
