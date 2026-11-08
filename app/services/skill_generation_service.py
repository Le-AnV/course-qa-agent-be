import json
import logging
import re
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from app.core.prompt_manager import get_prompt_manager
from app.services.llm.provider import get_llm_model
from app.services.skill_taxonomy_service import *

logger = logging.getLogger(__name__)

def generate_class_skill_taxonomy(
    class_name: str,
    class_code: str,
    credits: int = 3,
    semester: str = "",
    academic_year: str = "",
    class_description: str | None = None,
) -> list[dict[str, Any]]:
    """Synthesize 5-level Dreyfus skill taxonomy and rubrics directly from class metadata."""
    prompt_mgr = get_prompt_manager()
    prompt_data = prompt_mgr.get("class-skills-prompt")
    system_template = prompt_data.get("system-prompt", "")
    user_template = prompt_data.get("user-prompt", "")

    user_prompt = user_template.format(
        class_name=class_name,
        class_code=class_code,
        credits=credits,
        semester=semester,
        academic_year=academic_year,
        class_description=class_description or "(No detailed description provided, synthesize based on course title and domain)",
    )

    llm = get_llm_model()
    response = llm.invoke([
        SystemMessage(content=system_template),
        HumanMessage(content=user_prompt),
    ])

    raw_content = response.content if hasattr(response, "content") else str(response)
    if isinstance(raw_content, list):
        raw_content = "".join([part.get("text", "") if isinstance(part, dict) else str(part) for part in raw_content])

    json_match = re.search(r"\{[\s\S]*\}", raw_content)
    if not json_match:
        raise ValueError(f"LLM failed to return valid JSON output: {raw_content[:200]}")

    data = json.loads(json_match.group(0))
    skills_data = data.get("skills", [])
    
    # Chuẩn hóa code cho từng skill
    cleaned_skills = []
    for s in skills_data:
        code = normalize_skill_code(s.get("code") or s.get("name"))
        cleaned_skills.append({
            "code": code,
            "name": s.get("name", code),
            "category": s.get("category", "GENERAL"),
            "description": s.get("description", ""),
            "rubrics": s.get("rubrics", []),
        })

    return cleaned_skills
