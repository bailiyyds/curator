"""Explicit, locked, journaled execution of approved rename plans."""

import hashlib
import re
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from curator.core.journal import Journal, move_no_replace, root_lock, sha256
from curator.models.changes import Change, JournalEntry, OperationReport, Plan
from curator.safety.paths import SafetyError, collision, safe_path


def validate_plan(plan: Plan) -> Path:
    """Validate the entire plan before any state files or user files are written."""
    root = Path(plan.root)
    if not root.is_absolute() or not root.is_dir() or root.resolve() != root:
        raise SafetyError("Plan root must be an existing canonical absolute directory")
    sources: set[str] = set()
    targets: set[str] = set()
    ids: set[str] = set()
    for change in plan.changes:
        if change.op != "rename":
            raise SafetyError("Only rename is supported by this stage")
        src, dst = safe_path(root, change.src), safe_path(root, change.dst)
        if src.parent != dst.parent:
            raise SafetyError("Rename must stay in the same directory")
        if not re.fullmatch(r"[a-f0-9]{64}", change.pre_sha256):
            raise SafetyError("Invalid pre-change SHA-256")
        if change.id in ids or change.src.casefold() in sources or change.dst.casefold() in targets:
            raise SafetyError("Duplicate change ID, source or target")
        sources.add(change.src.casefold())
        targets.add(change.dst.casefold())
        ids.add(change.id)
    if sources & targets:
        raise SafetyError("Rename chains, cycles and case-only renames are forbidden")
    return root


def entry_for(
    change: Change, *, phase: str = "commit", result: str = "ok", error: str | None = None
) -> JournalEntry:
    """Construct a schema-validated event for a rename."""
    return JournalEntry.model_validate(
        {
            "ts": datetime.now(UTC),
            "change_id": change.id,
            "op": change.op,
            "src": change.src,
            "dst": change.dst,
            "result": result,
            "phase": phase,
            "error": error,
            "pre_sha256": change.pre_sha256,
            "post_sha256": change.pre_sha256 if result == "ok" else None,
        }
    )


def apply_plan(
    plan: Plan, *, apply: bool = False, allow_high_risk: bool = False
) -> OperationReport:
    """Preview by default; perform validated renames only with explicit opt-in."""
    root = validate_plan(plan)
    report = OperationReport(dry_run=not apply, warnings=plan.warnings.copy())
    if not apply:
        return report
    with root_lock(root), Journal(root, plan) as journal:
        report.journal = str(journal.path)
        for change in plan.changes:
            src, dst = safe_path(root, change.src), safe_path(root, change.dst)
            reason: str | None = None
            if change.risk == "high" and not allow_high_risk:
                reason = "High-risk change skipped; explicit --allow-high-risk is required"
            elif not src.is_file():
                reason = "Source is missing or is not a regular file"
            elif collision(dst):
                reason = "Destination exists; overwriting is forbidden"
            elif sha256(src) != change.pre_sha256:
                reason = "Source SHA-256 changed since plan creation"
            if reason:
                event = entry_for(change, result="skipped", error=reason)
                journal.append(event)
                report.entries.append(event)
                continue
            journal.append(entry_for(change, phase="prepare"))
            try:
                # Revalidate links and content after the durable intent.
                src, dst = safe_path(root, change.src), safe_path(root, change.dst)
                if not src.is_file() or sha256(src) != change.pre_sha256:
                    raise SafetyError("Source changed immediately before rename")
                move_no_replace(src, dst)
                if sha256(dst) != change.pre_sha256:
                    raise SafetyError("Destination content changed during rename")
                event = entry_for(change)
            except (OSError, SafetyError) as error:
                event = entry_for(change, result="failed", error=str(error))
            journal.append(event)
            report.entries.append(event)
    return report


def write_artifact(
    root: Path, relative: str, payload: bytes, *, apply: bool = False
) -> OperationReport:
    """Export a new artifact through a journal and staged no-overwrite atomic rename."""
    root = root.resolve(strict=True)
    target = safe_path(root, relative)
    if not target.parent.is_dir() or collision(target):
        raise SafetyError("Output parent must exist and output must not already exist")
    report = OperationReport(dry_run=not apply)
    if not apply:
        return report
    digest = hashlib.sha256(payload).hexdigest()
    with root_lock(root), Journal(root) as journal:
        report.journal = str(journal.path)
        event = JournalEntry(
            ts=datetime.now(UTC),
            change_id="export_" + uuid4().hex,
            op="write",
            src=relative,
            dst=None,
            result="ok",
            phase="prepare",
            post_sha256=digest,
        )
        journal.append(event)
        staging = safe_path(root, ".curator/staging", internal=True)
        staging.mkdir(exist_ok=True)
        temporary = safe_path(root, ".curator/staging/" + uuid4().hex, internal=True)
        try:
            with temporary.open("xb") as stream:
                stream.write(payload)
                stream.flush()
                import os

                os.fsync(stream.fileno())
            target = safe_path(root, relative)
            move_no_replace(temporary, target)
            event = event.model_copy(update={"ts": datetime.now(UTC), "phase": "commit"})
        except (OSError, SafetyError) as error:
            event = event.model_copy(
                update={
                    "ts": datetime.now(UTC),
                    "phase": "commit",
                    "result": "failed",
                    "error": str(error),
                }
            )
        journal.append(event)
        report.entries.append(event)
    return report
