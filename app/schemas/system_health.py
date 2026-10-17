"""Schemas for AI-109 health check & system monitoring."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

# Component statuses:
#   up / down          — live probe result (DB, Qdrant, LLM API reachable)
#   configured /       — configuration readiness only (no external call):
#   unconfigured          used by the fast /health liveness path for the LLM.
ComponentStatus = Literal["up", "down", "configured", "unconfigured"]

# Overall roll-up across all components.
SystemStatus = Literal["ok", "degraded"]


class ComponentHealth(BaseModel):
    """One monitored dependency and whether it currently looks healthy."""

    name: str = Field(min_length=1)
    status: ComponentStatus
    healthy: bool
    detail: str = Field(min_length=1)


class SystemHealthRead(BaseModel):
    """Aggregated health for the backend and its three runtime dependencies."""

    status: SystemStatus
    service: str = Field(min_length=1)
    components: list[ComponentHealth]
