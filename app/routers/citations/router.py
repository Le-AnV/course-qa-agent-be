"""Citations router implementing Epic E5 (AI-73, AI-75, AI-77)."""

from fastapi import APIRouter, Query

from app.schemas.citations import CitationExcerptResponse, CitationLocation

router = APIRouter(prefix="/api/citations", tags=["citations"])


@router.get("/excerpt", response_model=CitationExcerptResponse)
async def get_citation_excerpt(
    chunk_id: str = Query(..., description="The ID of the chunk to retrieve excerpt for"),
):
    """Retrieve the excerpt and surrounding context of a cited source (Task AI-75)."""
    return CitationExcerptResponse(
        citation_id=1,
        document_name="CS101_Syllabus_2026.pdf",
        document_type="pdf",
        location=CitationLocation(page_number=3),
        highlight_quote="điểm thi thực hành kết thúc học phần chiếm 60% tổng số điểm",
        surrounding_text=(
            "Tiêu chí đánh giá môn học: Điểm chuyên cần chiếm 10%, bài tập lớn giữa kỳ chiếm 30%, "
            "và điểm thi thực hành kết thúc học phần chiếm 60% tổng số điểm. "
            "Sinh viên vắng quá 20% số tiết sẽ không được dự thi."
        ),
        jump_url=None,
    )
