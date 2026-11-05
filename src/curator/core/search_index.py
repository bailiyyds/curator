"""Immutable, journaled search snapshots in the private local state directory."""

import hashlib
import os
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from pydantic import BaseModel, Field

from curator.core.journal import Journal, move_no_replace, root_lock
from curator.models import SkippedItem
from curator.models.changes import JournalEntry
from curator.safety.limits import INDEX_BYTES, SEARCH_CHARACTERS, SEARCH_DOCUMENTS
from curator.safety.paths import SafetyError, safe_path


class VectorIndex(BaseModel):
    """Model-identified embeddings for bounded extracted-text chunks."""

    model_fingerprint: str
    chunks: list[tuple[str, int, str]]
    vectors: list[list[float]]


class Snapshot(BaseModel):
    """Versioned derived text and postings, keyed by source hashes and limits."""

    skipped: list[SkippedItem] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    vectors: VectorIndex | None = None
    version: int = 1
    root: str
    fingerprint: str
    texts: dict[str, list[str]]
    postings: dict[str, dict[str, list[int]]]


def index_path(root: Path, fingerprint: str) -> Path:
    """Resolve only a SHA-256 filename under the protected index directory."""
    if len(fingerprint) != 64 or any(c not in "0123456789abcdef" for c in fingerprint):
        raise ValueError("Invalid index fingerprint")
    return safe_path(root, f".curator/index/{fingerprint}.json", internal=True)


def read_index(root: Path, fingerprint: str) -> Snapshot | None:
    """Ignore stale, corrupt or oversized snapshots without modifying them."""
    path = index_path(root, fingerprint)
    if not path.is_file():
        return None
    if path.stat().st_size > INDEX_BYTES:
        raise ValueError("Index exceeds read budget")
    snapshot = Snapshot.model_validate_json(path.read_bytes())
    if snapshot.version != 1 or snapshot.root != str(root) or snapshot.fingerprint != fingerprint:
        raise ValueError("Index identity mismatch")
    if (
        len(snapshot.texts) > SEARCH_DOCUMENTS
        or sum(len(s) for ls in snapshot.texts.values() for s in ls) > SEARCH_CHARACTERS
    ):
        raise ValueError("Index content budget exceeded")
    for relative in snapshot.texts:
        safe_path(root, relative)
    for paths in snapshot.postings.values():
        for relative, numbers in paths.items():
            if relative not in snapshot.texts or any(
                n < 0 or n >= len(snapshot.texts[relative]) for n in numbers
            ):
                raise ValueError("Invalid posting location")
    return snapshot


def save_index(snapshot: Snapshot) -> Path:
    """Publish a new snapshot without overwriting any existing state."""
    root = Path(snapshot.root).resolve(strict=True)
    target = index_path(root, snapshot.fingerprint)
    payload = snapshot.model_dump_json().encode()
    if len(payload) > INDEX_BYTES:
        raise ValueError("Index exceeds persistence budget")
    with root_lock(root), Journal(root, kind="index") as journal:
        if target.exists():
            existing = read_index(root, snapshot.fingerprint)
            if (
                existing is None
                or existing.texts != snapshot.texts
                or existing.postings != snapshot.postings
                or existing.vectors != snapshot.vectors
            ):
                raise SafetyError("Existing index differs; refusing overwrite")
            return target
        target.parent.mkdir(exist_ok=True)
        event = JournalEntry(
            ts=datetime.now(UTC),
            change_id="index_" + uuid4().hex,
            op="index_write",
            src=target.relative_to(root).as_posix(),
            dst=None,
            result="ok",
            phase="prepare",
            post_sha256=hashlib.sha256(payload).hexdigest(),
        )
        journal.append(event)
        try:
            staging = safe_path(root, ".curator/staging", internal=True)
            staging.mkdir(exist_ok=True)
            temporary = safe_path(root, ".curator/staging/" + uuid4().hex, internal=True)
            with temporary.open("xb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            move_no_replace(temporary, index_path(root, snapshot.fingerprint))
        except (OSError, ValueError) as error:
            journal.append(
                event.model_copy(
                    update={"phase": "commit", "result": "failed", "error": type(error).__name__}
                )
            )
            raise
        journal.append(event.model_copy(update={"phase": "commit"}))
    return target
