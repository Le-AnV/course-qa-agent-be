"""Document loaders package."""

from app.services.ingestion.loader.docx_loader import docx_loader
from app.services.ingestion.loader.excel_loader import excel_loader
from app.services.ingestion.loader.pdf_loader import extract_raw_embedded_images, pdf_loader
from app.services.ingestion.loader.powerpoint_loader import powerpoint_loader, pptx_loader
from app.services.ingestion.loader.text_loader import text_loader

__all__ = [
    "docx_loader",
    "excel_loader",
    "extract_raw_embedded_images",
    "pdf_loader",
    "powerpoint_loader",
    "pptx_loader",
    "text_loader",
]
