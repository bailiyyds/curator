"""Publish a batch of new artifacts as one journaled, undoable transaction."""

import hashlib
import os
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from curator.core.journal import Journal, move_no_replace, root_lock
from curator.models.changes import JournalEntry, OperationReport
from curator.safety.paths import SafetyError, collision, safe_path, sync_directory


def write_artifacts(
    root: Path, payloads: dict[str, bytes], *, apply: bool = False
) -> OperationReport:
    """Validate every target first; publish new files only after explicit opt-in."""
    root = root.resolve(strict=True)
    targets: set[str] = set()
    directories: set[Path] = set()
    for relative in payloads:
        target = safe_path(root, relative)
        if relative.casefold() in targets or collision(target):
            raise SafetyError("Output collision; existing files are never overwritten")
        targets.add(relative.casefold())
        for directory in target.parents:
            if directory == root:
                break
            if collision(directory) and not directory.is_dir():
                raise SafetyError("Output parent conflicts with an existing path")
            if not directory.exists():
                directories.add(directory)
    if any(p.relative_to(root).as_posix().casefold() in targets for p in directories):
        raise SafetyError("An output file cannot also be a directory")
    directory_cases: dict[str, str] = {}
    for directory in directories:
        relative = directory.relative_to(root).as_posix()
        previous = directory_cases.setdefault(relative.casefold(), relative)
        if previous != relative:
            raise SafetyError("Case-insensitive output directory collision")
    report = OperationReport(dry_run=not apply)
    if not apply or not payloads:
        return report
    with root_lock(root), Journal(root) as journal:
        report.journal = str(journal.path)
        for directory in sorted(directories, key=lambda p: (len(p.parts), str(p))):
            relative = directory.relative_to(root).as_posix()
            directory = safe_path(root, relative)
            event = JournalEntry(
                ts=datetime.now(UTC),
                change_id="dir_" + uuid4().hex,
                op="mkdir",
                src="",
                dst=relative,
                result="ok",
                phase="prepare",
            )
            if collision(directory):
                event.phase, event.result, event.error = (
                    "commit",
                    "skipped",
                    "Directory appeared after planning; not owned by this transaction",
                )
            else:
                journal.append(event)
                try:
                    directory.mkdir(exist_ok=False)
                    identity = directory.stat()
                    sync_directory(directory.parent)
                    event = event.model_copy(
                        update={
                            "phase": "commit",
                            "file_id": f"{identity.st_dev}:{identity.st_ino}",
                        }
                    )
                except (OSError, SafetyError) as error:
                    event = event.model_copy(
                        update={"phase": "commit", "result": "failed", "error": str(error)}
                    )
            journal.append(event)
            report.entries.append(event)
        staging = safe_path(root, ".curator/staging", internal=True)
        staging.mkdir(exist_ok=True)
        for relative, payload in payloads.items():
            event = JournalEntry(
                ts=datetime.now(UTC),
                change_id="doc_" + uuid4().hex,
                op="write",
                src=relative,
                dst=None,
                result="ok",
                phase="prepare",
                post_sha256=hashlib.sha256(payload).hexdigest(),
            )
            journal.append(event)
            try:
                temporary = safe_path(root, ".curator/staging/" + uuid4().hex, internal=True)
                with temporary.open("xb") as stream:
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
                move_no_replace(temporary, safe_path(root, relative))
                event = event.model_copy(update={"phase": "commit", "ts": datetime.now(UTC)})
            except (OSError, SafetyError) as error:
                event = event.model_copy(
                    update={"phase": "commit", "result": "failed", "error": str(error)}
                )
            journal.append(event)
            report.entries.append(event)
    return report
