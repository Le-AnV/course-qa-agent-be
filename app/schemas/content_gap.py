"""Pydantic schemas for Content Gap dashboard and reporting (AI-91, Phase 3)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class ContentGapQuestion(BaseModel):
    """An individual student inquiry that triggered web search fallback."""

    id: str = Field(..., description="Message UUID or query reference ID")
    question: str = Field(..., description="Actual student question text")
    timestamp: datetime = Field(..., description="When the inquiry was made")
    concept_tag: str | None = Field(default=None, description="Extracted key concept or subtopic")


class ContentGapReasonGroup(BaseModel):
    """Pedagogical grouping of student questions under a specific web fallback reason."""

    reason_type: Literal[
        "missing_code",
        "missing_comparison",
        "debugging_error",
        "missing_concept",
    ] = Field(..., description="System key for the pedagogical reason")
    title: str = Field(..., description="Human-readable reason title (e.g. Missing Practical Code Implementation)")
    explanation: str = Field(
        ...,
        description="Clear explanation of why lecture materials failed to answer and forced web search",
    )
    questions: list[ContentGapQuestion] = Field(
        default_factory=list,
        description="List of specific student questions belonging to this reason",
    )
    question_count: int = Field(default=0, description="Number of questions under this reason")


class QuestionPattern(BaseModel):
    """A synthesized recurring question theme summarizing multiple similar student questions."""

    pattern: str = Field(..., description="Synthesized question theme (e.g. Comparing C++ vs Python memory & performance)")
    inquiry_count: int = Field(default=1, description="Number of times this question was asked with web fallback")
    total_inquiry_count: int = Field(default=1, description="Total questions asked on this theme (material + web)")
    student_count: int = Field(default=1, description="Number of distinct students who asked it")
    pedagogical_category: str = Field(default="In-Scope Comparison", description="Category of difficulty")
    sample_question: str | None = Field(default=None, description="One representative student wording")
    suggested_action: str | None = Field(default=None, description="Actionable curriculum advice specific to this pattern")
    scope_type: str = Field(default="core_curriculum", description="Whether this is core_curriculum or enrichment")


class StudentWeaknessProfile(BaseModel):
    """Pedagogical analysis of student learning difficulties and conceptual misconceptions."""

    struggle_summary: str = Field(..., description="What concept or mechanism students are finding difficult")
    severity: Literal["high", "medium", "low"] = Field(default="high", description="Weakness severity")
    question_patterns: list[QuestionPattern] = Field(
        default_factory=list,
        description="Top recurring question themes (synthesized and deduplicated)",
    )


class CurriculumGapProfile(BaseModel):
    """Pedagogical analysis of missing coverage in lecture slides and actionable advice."""

    missing_points: list[str] = Field(
        default_factory=list,
        description="Specific topics/concepts missing in lecture slides",
    )
    recommendation: str = Field(
        ...,
        description="Actionable recommendation for teacher on what slide/material to add next",
    )


class ContentGapTopicReport(BaseModel):
    """Report and metrics for a single course category/topic."""

    category_id: str = Field(..., description="UUID string of the topic")
    category_name: str = Field(..., description="Name of the curriculum topic")
    parent_id: str | None = Field(default=None, description="Parent category UUID if this is a subtopic")
    material_answers: int = Field(default=0, description="Number of queries answered directly from lecture slides")
    web_fallbacks: int = Field(default=0, description="Total student inquiries requiring web search")
    dedup_web_fallbacks: int = Field(default=0, description="Deduplicated search count (max 1 per student per day)")
    total_queries: int = Field(default=0, description="Total inquiries in this topic")
    fallback_ratio: float = Field(default=0.0, description="Ratio of web search fallbacks (0.0 to 1.0)")
    distinct_students: int = Field(default=0, description="Count of distinct students who searched this topic")
    class_percentage: float = Field(default=0.0, description="Percentage of class affected (0.0 to 100.0)")
    status: Literal["critical", "warning", "good"] = Field(
        ...,
        description="'critical' = Needs Materials, 'warning' = Moderate, 'good' = Well Covered",
    )
    student_weakness: StudentWeaknessProfile = Field(
        ...,
        description="Insights on student knowledge weaknesses and synthesized question themes",
    )
    curriculum_gap: CurriculumGapProfile = Field(
        ...,
        description="Insights on missing lecture materials and teacher action recommendations",
    )
    reason_groups: list[ContentGapReasonGroup] = Field(
        default_factory=list,
        description="Detailed reason groups kept for drilldown",
    )


class ContentGapDashboardResponse(BaseModel):
    """Complete dashboard response for Content Gap Overview and Modal dialog."""

    class_id: uuid.UUID = Field(..., description="Target class UUID")
    total_web_fallbacks: int = Field(default=0, description="Total queries answered via external web search")
    total_material_answers: int = Field(default=0, description="Total queries answered from class documents")
    overall_coverage_rate: float = Field(default=100.0, description="Percentage answered from materials")
    critical_topics_count: int = Field(default=0, description="Count of topics marked as Needs Materials")
    total_students_enrolled: int = Field(default=0, description="Active students in the class")
    topics: list[ContentGapTopicReport] = Field(
        default_factory=list,
        description="All analyzed course topics sorted by deficit severity",
    )
    last_updated_at: datetime = Field(..., description="Timestamp of when analytics were computed")
