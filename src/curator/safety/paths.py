"""Strict portable path validation for user operations and private state."""

import os
import re
from pathlib import Path, PurePosixPath

PROTECTED = {".git", "node_modules", ".venv", "venv", "__pycache__", ".curator"}
RESERVED = {
    "con",
    "prn",
    "aux",
    "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}


class SafetyError(ValueError):
    """A requested operation violates the root or no-overwrite policy."""


def safe_path(root: Path, relative: str, *, internal: bool = False) -> Path:
    """Reject traversal, links, alternate streams, device names and protected paths."""
    if any(part.casefold() in PROTECTED for part in root.resolve().parts):
        raise SafetyError("Root is inside a protected directory")
    if not relative or "\\" in relative or ":" in relative:
        raise SafetyError("Expected a nonempty relative POSIX path")
    rel = PurePosixPath(relative)
    if rel.as_posix() != relative:
        raise SafetyError("Expected a normalized relative path")
    if rel.is_absolute() or any(p in {".", ".."} for p in relative.split("/")):
        raise SafetyError("Absolute paths and traversal are forbidden")
    if any(p.casefold() in PROTECTED for p in rel.parts) and not internal:
        raise SafetyError("Protected path")
    if internal and (not rel.parts or rel.parts[0] != ".curator"):
        raise SafetyError("Internal access is limited to .curator")
    cursor = root
    for part in rel.parts:
        if (
            part.endswith((".", " "))
            or part.split(".")[0].casefold() in RESERVED
            or re.search(r'[<>"|?*\x00-\x1f]', part)
        ):
            raise SafetyError("Unsafe portable path component")
        cursor = cursor / part
        if cursor.is_symlink() or cursor.is_junction():
            raise SafetyError("Links and junctions are forbidden")
    if not cursor.resolve().is_relative_to(root.resolve()):
        raise SafetyError("Path escapes root")
    return cursor


def collision(path: Path) -> bool:
    """Check collisions case-insensitively even on case-sensitive filesystems."""
    return (
        path.exists()
        or path.is_symlink()
        or (
            path.parent.exists()
            and any(p.name.casefold() == path.name.casefold() for p in path.parent.iterdir())
        )
    )


def sync_directory(directory: Path) -> None:
    """Flush directory entries where the OS supports directory fsync."""
    if os.name != "nt":
        fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
