"""Portable document and extraction result schemas."""

from typing import Any

from pydantic import BaseModel, Field

from curator.models import SkippedItem


class Heading(BaseModel):
    """A heading with its one-based source line."""

    level: int
    text: str
    line: int


class CodeBlock(BaseModel):
    """A literal code block and its source location."""

    language: str | None = None
    body: str
    line: int


class Word(BaseModel):
    """PDF word coordinates in points, measured from the page top."""

    text: str
    x0: float
    top: float
    x1: float
    bottom: float


class Page(BaseModel):
    """A PDF page, including table cells and text geometry."""

    number: int
    width: float
    height: float
    text: str
    words: list[Word] = Field(default_factory=list)
    tables: list[list[list[str | None]]] = Field(default_factory=list)
    needs_ocr: bool = False


class Symbol(BaseModel):
    """A code declaration located by tree-sitter."""

    kind: str
    name: str
    line: int
    col: int
    end_line: int
    signature: str


class Document(BaseModel):
    """Normalized text with optional source-specific structure."""

    path: str
    title: str | None = None
    headings: list[Heading] = Field(default_factory=list)
    body: str = ""
    pages: list[Page] | None = None
    frontmatter: dict[str, Any] = Field(default_factory=dict)
    links: list[str] = Field(default_factory=list)
    code_blocks: list[CodeBlock] = Field(default_factory=list)
    needs_ocr: bool = False
    language: str | None = None
    symbols: list[Symbol] = Field(default_factory=list)
    parse_errors: bool = False


class ExtractionResult(BaseModel):
    """Expected failures are reported rather than thrown away."""

    path: str
    document: Document | None = None
    error: str | None = None
    warnings: list[str] = Field(default_factory=list)


class ExtractionReport(BaseModel):
    """Batch extraction counts include rejected files and image-only PDFs."""

    results: list[ExtractionResult]
    skipped: list[SkippedItem]
    scanned_pdfs: int
