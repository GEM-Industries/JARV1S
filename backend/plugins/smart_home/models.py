"""Shared smart_home return types."""

from __future__ import annotations

from pydantic import BaseModel, Field


class DeviceSummary(BaseModel):
    entity_id: str
    name: str
    domain: str
    state: str
    area_name: str | None = None
    brightness_pct: int | None = None
    color_temp_kelvin: int | None = None
    color_mode: str | None = None
    rgb_color: list[int] | None = None
    capabilities: list[str] = Field(default_factory=list)


class RefreshHomeAssistantResult(BaseModel):
    outcome: str
    message: str
    config_entry_id: str | None = None
    candidate_count: int = 0
    candidates: list[DeviceSummary] = Field(default_factory=list)
    error: str | None = None


class OrganizeDeviceResult(BaseModel):
    entity_id: str
    name: str
    area_name: str | None = None
    area_id: str | None = None
    message: str
