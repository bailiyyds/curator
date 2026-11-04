"""Source-grounded API documentation and reviewable output plans."""

from pydantic import BaseModel, Field

from curator.models import SkippedItem


class ParameterDoc(BaseModel):
    """A parameter as declared, without inferred runtime values."""

    name: str
    annotation: str | None = None
    default: str | None = None


class SymbolDoc(BaseModel):
    """One declaration with lexical ownership and textual call targets."""

    name: str
    qualified_name: str
    parent: str | None = None
    kind: str
    path: str
    line: int
    col: int
    signature: str
    docstring: str | None = None
    decorators: list[str] = Field(default_factory=list)
    parameters: list[ParameterDoc] = Field(default_factory=list)
    calls: list[str] = Field(default_factory=list)


class FileActivity(BaseModel):
    """File-level aggregation over the bounded local commit window."""

    path: str
    commits: int = 0
    additions: int = 0
    deletions: int = 0
    binary_changes: int = 0
    latest_commit: str | None = None
    latest_date: str | None = None
    summaries: list[str] = Field(default_factory=list)


class GitHistory(BaseModel):
    """History availability and explicit scope warnings."""

    available: bool
    commit_count: int = 0
    files: list[FileActivity] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class ModuleDoc(BaseModel):
    """Static module description; no inspected code is ever loaded."""

    path: str
    module: str
    language: str
    package: str | None = None
    docstring: str | None = None
    symbols: list[SymbolDoc] = Field(default_factory=list)
    calls: list[str] = Field(default_factory=list)
    parse_errors: bool = False


class DocArtifact(BaseModel):
    """A fresh output or a no-write up-to-date result plus a human merge diff."""

    path: str
    content: str
    source_paths: list[str]
    base_path: str
    diff: str | None = None
    write_required: bool = True


class DocPlan(BaseModel):
    """Complete preview of generated artifacts and source analysis."""

    root: str
    requested_from: str
    effective_from: str
    split: str
    modules: list[ModuleDoc]
    history: GitHistory
    artifacts: list[DocArtifact]
    skipped: list[SkippedItem]
    warnings: list[str]
    errors: list[SkippedItem] = Field(default_factory=list)
