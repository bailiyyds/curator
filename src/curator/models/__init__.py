"""Inventory schemas."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel


class FileEntry(BaseModel):
    """One observed file."""

    path: str
    size: int
    mtime: float
    sha256: str
    kind: Literal["pdf", "markdown", "text", "code", "image", "binary", "unknown"]
    language: str | None = None
    error: str | None = None


class SkippedItem(BaseModel):
    """An explicit scan omission."""

    path: str
    reason: str


class Inventory(BaseModel):
    """Stable directory snapshot."""

    root: str
    created_at: datetime
    files: list[FileEntry]
    skipped: list[SkippedItem]
