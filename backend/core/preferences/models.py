"""Owner-scoped runtime preferences."""

from __future__ import annotations

from pydantic import BaseModel, Field


class AudioPreferences(BaseModel):
    tool_cues_enabled: bool = True


class UserPreferences(BaseModel):
    owner_id: str
    audio: AudioPreferences = Field(default_factory=AudioPreferences)


class AudioPreferencesPatch(BaseModel):
    tool_cues_enabled: bool | None = None


class UserPreferencesPatch(BaseModel):
    audio: AudioPreferencesPatch | None = None
