"""Precise review findings and reproducible aggregate reports."""

from typing import Literal

from pydantic import BaseModel, Field

from curator.models import SkippedItem


class Finding(BaseModel):
    """One location with evidence; merged_rules preserves same-line rule identities."""

    id: str
    path: str
    line: int = Field(ge=1)
    col: int = Field(ge=1)
    rule: str
    severity: Literal["error", "warning", "info"]
    message: str
    evidence: str
    suggestion: str | None = None
    patch: str | None = None
    source: Literal["rule", "llm"] = "rule"
    merged_rules: list[str] = Field(default_factory=list)


class ReviewReport(BaseModel):
    """All skipped files, marker totals and rule counts remain inspectable."""

    root: str
    files_reviewed: int
    findings: list[Finding]
    skipped: list[SkippedItem]
    warnings: list[str]
    rule_counts: dict[str, int]
    marker_counts: dict[str, int]
    errors: list[SkippedItem] = Field(default_factory=list)
