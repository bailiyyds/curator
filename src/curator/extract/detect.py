"""Shared kind detection from content plus conservative extensions."""

from typing import Literal

Kind = Literal["pdf", "markdown", "text", "code", "image", "binary", "unknown"]
from curator.extract.code import LANGUAGES
from curator.safety.bincheck import is_binary


def detect_kind(suffix: str, head: bytes) -> tuple[Kind, str | None]:
    """Prefer content magic over extensions; preserve malformed PDFs for explicit errors."""
    if head.startswith(b"%PDF-"):
        return "pdf", None
    if head.startswith((b"\x89PNG", b"\xff\xd8\xff", b"GIF87a", b"GIF89a")):
        return "image", None
    if is_binary(head):
        return "binary", None
    ext = suffix.lower().lstrip(".")
    if ext in LANGUAGES:
        return "code", ext
    if ext in {"md", "markdown"}:
        return "markdown", None
    if ext in {"txt", "csv", "json", "toml", "yaml", "yml", "rst", "log"}:
        return "text", None
    if ext == "pdf":
        return "pdf", None
    if ext in {"png", "jpg", "jpeg", "gif", "webp", "svg"}:
        return "image", None
    return "unknown", None
