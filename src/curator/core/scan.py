"""Bounded, read-only filesystem inventory."""

import hashlib
import logging
from datetime import UTC, datetime
from fnmatch import fnmatch
from pathlib import Path

from curator.core.config import Config
from curator.models import FileEntry, Inventory, SkippedItem
from curator.safety.bincheck import is_binary
from curator.safety.paths import PROTECTED


def scan(root: Path, config: Config | None = None) -> Inventory:
    """Scan without following links or executing inspected content.

    Snapshot time is the newest observed mtime, keeping unchanged scans stable.
    """
    root = root.resolve(strict=True)
    if not root.is_dir():
        raise ValueError("Root must be a directory")
    config = config or Config()
    if config.follow_symlinks:
        raise ValueError("Following links is disabled for safe inventory scans")
    files: list[FileEntry] = []
    skipped: list[SkippedItem] = []

    logger = logging.getLogger(__name__)

    def skip(path: Path, reason: str) -> None:
        logger.debug("Skipped %s: %s", path.relative_to(root).as_posix(), reason)
        skipped.append(SkippedItem(path=path.relative_to(root).as_posix(), reason=reason))

    stack = [(root, 0)]
    while stack:
        directory, depth = stack.pop()
        try:
            entries = sorted(directory.iterdir())
        except OSError as error:
            skip(directory, str(error))
            continue
        for path in entries:
            rel = path.relative_to(root).as_posix()
            try:
                if path.is_symlink() or path.is_junction():
                    skip(path, "Symbolic links and junctions are not followed")
                    continue
                if path.name.casefold() in PROTECTED or any(
                    fnmatch(rel, g) for g in config.exclude
                ):
                    skip(path, "Protected or excluded path")
                    continue
                if path.is_dir():
                    if depth >= config.max_depth:
                        skip(path, "Depth budget exceeded")
                    else:
                        stack.append((path, depth + 1))
                    continue
                if not path.is_file():
                    skip(path, "Not a regular file")
                    continue
                if config.include and not any(fnmatch(rel, g) for g in config.include):
                    skip(path, "Does not match include filters")
                    continue
                stat = path.stat()
                if stat.st_size > config.max_size or len(files) >= config.max_files:
                    skip(path, "Size or file count budget exceeded")
                    continue
                digest = hashlib.sha256()
                with path.open("rb") as stream:
                    head = stream.read(8192)
                    digest.update(head)
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        digest.update(chunk)
                after = path.stat()
                if (stat.st_size, stat.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                    skip(path, "File changed during scan")
                    continue
                item = FileEntry(
                    path=rel,
                    size=stat.st_size,
                    mtime=stat.st_mtime,
                    sha256=digest.hexdigest(),
                    kind="unknown",
                )
                ext = path.suffix.lower()
                if ext == ".pdf":
                    item.kind = "pdf"
                elif head.startswith((b"\x89PNG", b"\xff\xd8\xff", b"GIF87a", b"GIF89a")):
                    item.kind = "image"
                elif is_binary(head):
                    item.kind = "binary"
                elif ext in {".md", ".markdown"}:
                    item.kind = "markdown"
                elif ext in {".py", ".ts", ".tsx", ".js", ".jsx", ".go", ".rs", ".java"}:
                    item.kind, item.language = "code", ext[1:]
                elif ext in {".png", ".jpg", ".jpeg", ".gif"}:
                    item.kind = "image"
                elif ext in {".txt", ".csv", ".json", ".toml", ".yaml"}:
                    item.kind = "text"
                logger.debug("Scanned %s (%d bytes)", rel, item.size)
                files.append(item)
            except OSError as error:
                logger.error("Failed reading %s: %s", rel, error)
                files.append(
                    FileEntry(
                        path=rel, size=0, mtime=0, sha256="", kind="unknown", error=str(error)
                    )
                )
    logger.info("Scanned %d files; skipped %d", len(files), len(skipped))
    if skipped:
        logger.warning("Skipped %d paths; reasons are included in the inventory", len(skipped))
    return Inventory(
        root=str(root),
        created_at=datetime.fromtimestamp(max((f.mtime for f in files), default=0), UTC),
        files=sorted(files, key=lambda f: f.path),
        skipped=sorted(skipped, key=lambda s: s.path),
    )
