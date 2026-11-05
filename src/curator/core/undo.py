"""Conservative reverse execution including interrupted prepare records."""

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from curator.core.journal import Journal, move_no_replace, read_journal, root_lock, sha256
from curator.models.changes import JournalEntry, OperationReport
from curator.safety.paths import SafetyError, collision, safe_path


def journal_path(root: Path, path: Path) -> Path:
    """Validate an original journal as a regular file in this root's journal directory."""
    absolute = path.absolute()
    try:
        rel = absolute.relative_to(root).as_posix()
    except ValueError as error:
        raise SafetyError("Journal must be inside the root") from error
    target = safe_path(root, rel, internal=True)
    if target.parent != root / ".curator/journal" or not target.is_file():
        raise SafetyError("Expected a journal file directly inside .curator/journal")
    return target


def latest_journals(root: Path, last: int = 1) -> list[Path]:
    """Select original transactions by timestamp without selecting undo journals."""
    if last < 1:
        raise ValueError("Last must be positive")
    directory = safe_path(root, ".curator/journal", internal=True)
    if not directory.exists():
        return []
    candidates: list[tuple[str, Path]] = []
    completed: set[str] = set()
    for path in directory.glob("*.jsonl"):
        valid = journal_path(root, path)
        header, events, _ = read_journal(valid)
        if header.get("kind") in {"cache", "index"}:
            continue
        if header.get("undo_of") and any(
            e.op == "undo_complete" and e.result == "ok" and e.phase == "commit" for e in events
        ):
            completed.add(str(header["undo_of"]))
        if not header.get("undo_of"):
            candidates.append((str(header.get("created_at", "")), valid))
    return [p for _, p in sorted(candidates, reverse=True) if p.name not in completed][:last]


def undo_journal(path: Path, *, apply: bool = False) -> OperationReport:
    """Reverse only matching completed or recoverable operations; never overwrite."""
    header, _, _ = read_journal(path)
    root = Path(header["root"])
    if not root.is_absolute() or root.resolve() != root or not root.is_dir():
        raise SafetyError("Journal root is invalid")
    path = journal_path(root, path)
    if header.get("kind") in {"cache", "index"}:
        raise SafetyError("Cache/index metadata is not a user-file undo transaction")
    if header.get("undo_of"):
        raise SafetyError("Undo journals cannot themselves be undone")
    report = OperationReport(dry_run=not apply)
    if apply:
        with root_lock(root), Journal(root, undo_of=path.name) as journal:
            report.journal = str(journal.path)
            _reverse(root, path, report, journal)
            if all(e.result != "failed" for e in report.entries):
                journal.append(
                    JournalEntry(
                        ts=datetime.now(UTC),
                        change_id="undo_complete",
                        op="undo_complete",
                        src=path.name,
                        dst=None,
                        result="ok",
                    )
                )
    else:
        _reverse(root, path, report, None)
    return report


def _reverse(root: Path, path: Path, report: OperationReport, journal: Journal | None) -> None:
    header, events, warnings = read_journal(path)
    if Path(header["root"]) != root:
        raise SafetyError("Journal root changed")
    report.warnings.extend(warnings)
    # Prepared records survive a crash between rename and outcome flush.
    intents: dict[str, JournalEntry] = {}
    for event in events:
        if event.phase == "prepare":
            if event.change_id in intents:
                raise SafetyError("Duplicate intent in journal")
            intents[event.change_id] = event
    committed_dirs = {
        e.change_id: e.file_id
        for e in events
        if e.op == "mkdir" and e.phase == "commit" and e.result == "ok"
    }
    for intent in reversed(list(intents.values())):
        if intent.op == "mkdir" and intent.dst:
            directory = safe_path(root, intent.dst)
            event = JournalEntry(
                ts=datetime.now(UTC),
                change_id=intent.change_id,
                op="undo_mkdir",
                src=intent.dst,
                dst=None,
                result="skipped",
            )
            if not directory.exists():
                event.error = "Directory is already absent"
            elif intent.change_id not in committed_dirs:
                event.error = (
                    "Unconfirmed directory ownership after interruption; retained for review"
                )
            elif (
                committed_dirs[intent.change_id]
                != f"{directory.stat().st_dev}:{directory.stat().st_ino}"
            ):
                event.result, event.error = (
                    "failed",
                    "Directory identity changed; retained without modification",
                )
            elif not directory.is_dir() or any(directory.iterdir()):
                event.result, event.error = (
                    "failed",
                    "Directory is not empty; retained without modification",
                )
            elif journal:
                target_rel = f".curator/trash/{uuid4().hex}/{directory.name}"
                target = safe_path(root, target_rel, internal=True)
                event.dst, event.result = target_rel, "ok"
                journal.append(event.model_copy(update={"phase": "prepare"}))
                try:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    directory = safe_path(root, intent.dst)
                    if (
                        committed_dirs[intent.change_id]
                        != f"{directory.stat().st_dev}:{directory.stat().st_ino}"
                    ):
                        raise SafetyError("Directory identity changed before undo")
                    if any(directory.iterdir()):
                        raise SafetyError("Directory became nonempty before undo")
                    move_no_replace(directory, target)
                except (OSError, SafetyError) as error:
                    event.result, event.error = "failed", str(error)
            else:
                event.result = "ok"
            if journal:
                journal.append(event)
            report.entries.append(event)
            continue
        if intent.op in {"rename", "move"} and intent.dst:
            src = safe_path(root, intent.dst)
            dst = safe_path(root, intent.src)
            expected = intent.pre_sha256
        elif intent.op == "write":
            src = safe_path(root, intent.src)
            trash_rel = f".curator/trash/{uuid4().hex}/{src.name}"
            dst = safe_path(root, trash_rel, internal=True)
            expected = intent.post_sha256
        else:
            raise SafetyError("Unsupported journal operation")
        event = JournalEntry(
            ts=datetime.now(UTC),
            change_id=intent.change_id,
            op="undo_" + intent.op,
            src=src.relative_to(root).as_posix(),
            dst=dst.relative_to(root).as_posix(),
            result="ok",
            pre_sha256=expected,
            post_sha256=expected,
        )
        if not src.is_file():
            event = event.model_copy(
                update={"result": "skipped", "error": "Nothing to restore; source is absent"}
            )
        elif collision(dst):
            event = event.model_copy(
                update={"result": "failed", "error": "Restore target exists; nothing overwritten"}
            )
        elif not expected or sha256(src) != expected:
            event = event.model_copy(
                update={"result": "failed", "error": "Content changed; refusing undo"}
            )
        if event.result == "ok" and journal:
            journal.append(event.model_copy(update={"phase": "prepare"}))
            try:
                if intent.op == "write":
                    safe_path(root, dst.parent.relative_to(root).as_posix(), internal=True)
                    dst.parent.mkdir(parents=True, exist_ok=True)
                src = safe_path(root, event.src)
                if event.dst is None:
                    raise SafetyError("Missing undo target")
                dst = safe_path(root, event.dst, internal=intent.op == "write")
                if sha256(src) != expected:
                    raise SafetyError("Content changed before undo")
                move_no_replace(src, dst)
            except (OSError, SafetyError) as error:
                event = event.model_copy(update={"result": "failed", "error": str(error)})
        if journal:
            journal.append(event)
        report.entries.append(event)
