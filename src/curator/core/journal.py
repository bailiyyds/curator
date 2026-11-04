"""OS lock, append-only fsync journal, and atomic no-overwrite moves."""

import ctypes
import hashlib
import json
import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Any, Self
from uuid import uuid4

import portalocker

from curator.models.changes import JournalEntry, Plan
from curator.safety.paths import SafetyError, collision, safe_path, sync_directory


def sha256(path: Path) -> str:
    """Hash a file in bounded chunks."""
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@contextmanager
def root_lock(root: Path) -> Iterator[None]:
    """Acquire an OS-owned lock released automatically on process death."""
    state = safe_path(root, ".curator", internal=True)
    state.mkdir(exist_ok=True)
    lock = safe_path(root, ".curator/lock", internal=True)
    try:
        with portalocker.Lock(str(lock), mode="a", timeout=0):
            yield
    except portalocker.exceptions.LockException as error:
        raise SafetyError("Another curator writer holds the root lock") from error


def move_no_replace(src: Path, dst: Path) -> None:
    """Move atomically without overwriting an existing destination."""
    if collision(dst):
        raise SafetyError(f"Destination already exists: {dst.name}")
    if os.name == "nt":
        os.rename(src, dst)
    elif sys.platform.startswith("linux"):
        libc = ctypes.CDLL(None, use_errno=True)
        rename = libc.renameat2
        rename.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        rename.restype = ctypes.c_int
        if rename(-100, os.fsencode(src), -100, os.fsencode(dst), 1) != 0:
            errno = ctypes.get_errno()
            raise OSError(errno, os.strerror(errno))
    elif sys.platform == "darwin":
        libc = ctypes.CDLL(None, use_errno=True)
        rename = libc.renamex_np
        rename.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        rename.restype = ctypes.c_int
        if rename(os.fsencode(src), os.fsencode(dst), 4) != 0:
            errno = ctypes.get_errno()
            raise OSError(errno, os.strerror(errno))
    else:
        raise SafetyError("This platform has no supported atomic no-overwrite rename")
    sync_directory(src.parent)
    if src.parent != dst.parent:
        sync_directory(dst.parent)


class Journal:
    """Append every intent before mutation and every outcome after mutation."""

    def __init__(self, root: Path, plan: Plan | None = None, undo_of: str | None = None) -> None:
        """Create an exclusive journal inside a validated private state directory."""
        folder = safe_path(root, ".curator/journal", internal=True)
        folder.mkdir(exist_ok=True)
        self.path = safe_path(root, f".curator/journal/{uuid4().hex}.jsonl", internal=True)
        self.stream: IO[str] = self.path.open("x", encoding="utf-8", newline="\n")
        self._append(
            {
                "type": "header",
                "root": str(root),
                "created_at": datetime.now(UTC).isoformat(),
                "plan": plan.model_dump(mode="json") if plan else None,
                "undo_of": undo_of,
            }
        )
        sync_directory(folder)

    def _append(self, value: dict[str, Any]) -> None:
        self.stream.write(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n")
        self.stream.flush()
        os.fsync(self.stream.fileno())

    def append(self, entry: JournalEntry) -> None:
        """Persist one full JSONL record before returning."""
        self._append({"type": "entry", **entry.model_dump(mode="json")})

    def close(self) -> None:
        """Close the already-flushed journal."""
        self.stream.close()

    def __enter__(self) -> Self:
        """Return the active journal."""
        return self

    def __exit__(self, *args: object) -> None:
        """Close on success, failure or interruption."""
        self.close()


def read_journal(path: Path) -> tuple[dict[str, Any], list[JournalEntry], list[str]]:
    """Read complete records, tolerating only a torn final line after a crash."""
    lines = path.read_bytes().splitlines(keepends=True)
    if not lines:
        raise SafetyError("Empty journal")
    header = json.loads(lines[0])
    if (
        not isinstance(header, dict)
        or header.get("type") != "header"
        or not isinstance(header.get("root"), str)
    ):
        raise SafetyError("Invalid journal header")
    entries: list[JournalEntry] = []
    warnings: list[str] = []
    for index, raw in enumerate(lines[1:], 1):
        if not raw.endswith(b"\n") and index == len(lines) - 1:
            warnings.append("Ignored incomplete final journal record")
            break
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise SafetyError("Invalid journal entry")
        if value.pop("type", None) != "entry":
            raise SafetyError("Unexpected journal record type")
        entries.append(JournalEntry.model_validate(value))
    return header, entries, warnings
