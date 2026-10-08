"""Document splitter and chunker module."""
from typing import List, Optional, Tuple

from langchain_core.documents import Document
from langchain_text_splitters import RecursiveCharacterTextSplitter, MarkdownHeaderTextSplitter

from app.config.config import get_settings


import re

def markdown_chunker(
    markdown: str,
    metadata: Optional[dict] = None,
    headers_to_split_on: Optional[List[Tuple[str, str]]] = None
) -> List[Document]: 
    """Split markdown text into chunks based on headers and add page numbers.
    
    Args:
        markdown: The markdown text to split.
        metadata: Optional dictionary containing metadata.
        headers_to_split_on: Optional list of tuples specifying headers to split on.
        
    Returns:
        List of Document chunks split by headers.
    """
    settings = get_settings()
    headers = headers_to_split_on or settings.headers_to_split_on

    markdown_splitter = MarkdownHeaderTextSplitter(
        headers_to_split_on=headers,
        strip_headers=True
    )

    chunks = markdown_splitter.split_text(markdown)
    
    # Calculate starting page number for each chunk
    page_breaks = [m.start() for m in re.finditer(r"<!--\s*page\s*break\s*-->", markdown, flags=re.IGNORECASE)]
    search_start = 0

    for chunk in chunks:
        # Find chunk position in original markdown
        snippet = chunk.page_content[:50].strip()
        if snippet:
            idx = markdown.find(snippet, search_start)
            if idx != -1:
                search_start = idx
            else:
                idx = search_start
        else:
            idx = search_start

        page_num = 1 + sum(1 for pb in page_breaks if pb < idx)
        chunk.metadata["page_number"] = page_num

    if metadata:
        for chunk in chunks:
            chunk.metadata.update(metadata)
            
    # Clean up page break tags inside chunk contents
    for chunk in chunks:
        chunk.page_content = re.sub(r"<!--\s*page\s*break\s*-->\s*", "\n\n", chunk.page_content, flags=re.IGNORECASE)

    return chunks


def recursive_chunker(
    documents: List[Document],
    chunk_size: Optional[int] = None,
    chunk_overlap: Optional[int] = None,
    separators: Optional[List[str]] = None,
    metadata: Optional[dict] = None
) -> List[Document]:
    """Split documents into smaller chunks using recursive character splitting.
    
    Args:
        documents: List of Documents to split.
        chunk_size: Maximum character count per document chunk. 
            Defaults to settings.chunk_size.
        chunk_overlap: Overlap character count between consecutive chunks. 
            Defaults to settings.chunk_overlap.
        separators: List of string separators used for splitting. 
            Defaults to settings.separators.
        metadata: Optional dictionary containing metadata (e.g., file name, class, 
            chapter, starting page number) to add to or update in each chunk.
            
    Returns:
        List of recursively chunked Documents.
    """
    settings = get_settings()
    size = chunk_size if chunk_size is not None else settings.chunk_size
    overlap = chunk_overlap if chunk_overlap is not None else settings.chunk_overlap
    seps = separators if separators is not None else settings.separators

    splitter = RecursiveCharacterTextSplitter(
        separators=seps,
        chunk_size=size,
        chunk_overlap=overlap
    )

    chunks = splitter.split_documents(documents)
    
    if metadata:
        for chunk in chunks:
            chunk.metadata.update(metadata)
            
    return chunks