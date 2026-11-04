"""Reviewable plans and durable transaction records."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Change(BaseModel):
    """One explicitly proposed filesystem operation."""

    model_config = ConfigDict(extra="forbid")
    id: str
    op: Literal["rename", "move", "mkdir", "archive"]
    src: str
    dst: str
    reason: str
    confidence: float = Field(ge=0, le=1)
    risk: Literal["low", "medium", "high"]
    pre_sha256: str


class CleanupProposal(BaseModel):
    """An empty-directory proposal that is never part of apply operations."""

    path: str
    reason: str
    requires_confirmation: Literal[True] = True


class Plan(BaseModel):
    """A complete proposal that can be inspected without mutation."""

    model_config = ConfigDict(extra="forbid")
    plan_id: str
    root: str
    created_at: datetime
    changes: list[Change]
    warnings: list[str]
    cleanup: list[CleanupProposal] = Field(default_factory=list)


class JournalEntry(BaseModel):
    """Prepared intents and outcomes; additive fields support crash recovery."""

    ts: datetime
    change_id: str
    op: str
    src: str
    dst: str | None
    result: Literal["ok", "skipped", "failed"]
    error: str | None = None
    post_sha256: str | None = None
    phase: Literal["prepare", "commit"] = "commit"
    pre_sha256: str | None = None
    file_id: str | None = None


class OperationReport(BaseModel):
    """Machine-readable preview or transaction outcome."""

    journal: str | None = None
    entries: list[JournalEntry] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    dry_run: bool = True
