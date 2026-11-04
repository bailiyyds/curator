"""Read-only organization plans using type, UTC date or local text topics."""

import hashlib
from datetime import UTC, datetime
from pathlib import Path

from curator.analyze.docs import topic_labels
from curator.core.config import Config
from curator.core.scan import scan
from curator.extract import extract_file
from curator.models.changes import Change, CleanupProposal, Plan
from curator.safety.paths import collision, safe_path

ARCHIVES = {".zip", ".tar", ".gz", ".bz2", ".xz", ".7z", ".rar", ".tgz"}


def organize_plan(
    root: Path, *, by: str = "type", into: str = "organized", config: Config | None = None
) -> Plan:
    """Propose directories and non-overwriting file moves without touching disk."""
    if by not in {"type", "date", "topic"}:
        raise ValueError("Organization strategy must be type, date or topic")
    root = root.resolve(strict=True)
    destination = safe_path(root, into)
    if destination.exists() and not destination.is_dir():
        raise ValueError("Output directory is an existing file")
    config = config or Config()
    inventory = scan(root, config)
    warnings = [f"{s.path}: {s.reason}" for s in inventory.skipped]
    files = [f for f in inventory.files if not (f.path == into or f.path.startswith(into + "/"))]
    texts: dict[str, str] = {}
    if by == "topic":
        for file in files:
            if file.error or file.kind not in {"markdown", "text", "pdf", "code"}:
                continue
            if len(texts) >= config.max_topic_documents:
                warnings.append(f"{file.path}: topic analysis budget exceeded; assigned to 其他")
                continue
            extracted = extract_file(root, file.path, config.max_size)
            if extracted.document:
                texts[file.path] = extracted.document.body[:20000]
            else:
                warnings.append(f"{file.path}: topic extraction failed: {extracted.error}")
    topics = topic_labels(texts) if by == "topic" else {}
    occupied: set[str] = set()
    directories: set[str] = set()
    moves: list[Change] = []
    for file in files:
        if file.error:
            warnings.append(f"{file.path}: {file.error}")
            continue
        if by == "type":
            category = (
                "archives"
                if Path(file.path).suffix.lower() in ARCHIVES
                else {
                    "pdf": "docs",
                    "markdown": "docs",
                    "text": "docs",
                    "code": "code",
                    "image": "images",
                }.get(file.kind, "other")
            )
        elif by == "date":
            category = datetime.fromtimestamp(file.mtime, UTC).strftime("%Y/%m")
        else:
            category = topics.get(file.path, "其他")
        target = f"{into}/{category}/{Path(file.path).name}"
        if target.casefold() in occupied or collision(safe_path(root, target)):
            original = Path(file.path)
            target = f"{into}/_conflicts/{original.name}"
            counter = 2
            while target.casefold() in occupied or collision(safe_path(root, target)):
                target = f"{into}/_conflicts/{original.stem}-{counter}{original.suffix}"
                counter += 1
            warnings.append(f"{file.path}: name collision; routed to {target}")
        path = safe_path(root, target)
        if path.exists() and path.is_dir():
            raise ValueError("Move destination is a directory")
        occupied.add(target.casefold())
        for parent in path.parents:
            if parent == root:
                break
            if not parent.exists():
                directories.add(parent.relative_to(root).as_posix())
        moves.append(
            Change(
                id="pending",
                op="move",
                src=file.path,
                dst=target,
                reason=f"organize by {by}: {category}",
                confidence=0.8 if by == "topic" else 1,
                risk="low",
                pre_sha256=file.sha256,
            )
        )
    changes = [
        Change(
            id="pending",
            op="mkdir",
            src="",
            dst=directory,
            reason="Create organization directory",
            confidence=1,
            risk="low",
            pre_sha256="",
        )
        for directory in sorted(directories, key=lambda d: (d.count("/"), d))
    ]
    changes.extend(moves)
    for i, change in enumerate(changes, 1):
        change.id = f"chg_{i:04d}"
    moved_paths = {c.src for c in moves}
    cleanup: list[CleanupProposal] = []
    stack = [(root, 0)]
    directories_seen: list[Path] = []
    children: dict[Path, list[Path]] = {}
    from curator.safety.paths import PROTECTED

    while stack and len(directories_seen) < config.max_files:
        directory, depth = stack.pop()
        if depth >= config.max_depth:
            warnings.append(
                f"{directory.relative_to(root).as_posix()}: cleanup depth budget exceeded"
            )
            continue
        try:
            children[directory] = sorted(directory.iterdir())
        except OSError as error:
            warnings.append(
                f"{directory.relative_to(root).as_posix()}: cleanup inspection failed: {error}"
            )
            continue
        directories_seen.append(directory)
        for item in children[directory]:
            if (
                item.name.casefold() not in PROTECTED
                and not item.is_symlink()
                and not item.is_junction()
                and item.is_dir()
                and not item.is_relative_to(destination)
            ):
                stack.append((item, depth + 1))
    if stack:
        warnings.append(
            "Cleanup proposal traversal budget exceeded; remaining directories not inspected"
        )
    empty_after: set[Path] = set()
    for directory in sorted(directories_seen, key=lambda p: len(p.parts), reverse=True):
        if directory != root and all(
            item in empty_after or item.relative_to(root).as_posix() in moved_paths
            for item in children[directory]
        ):
            empty_after.add(directory)
            cleanup.append(
                CleanupProposal(
                    path=directory.relative_to(root).as_posix(),
                    reason="Already empty or will be empty after proposed moves; separate confirmation required",
                )
            )
    payload = "\n".join(c.model_dump_json() for c in changes)
    plan = Plan(
        plan_id="plan_" + hashlib.sha256(payload.encode()).hexdigest()[:16],
        root=str(root),
        created_at=inventory.created_at,
        changes=changes,
        warnings=warnings,
        cleanup=sorted(cleanup, key=lambda c: c.path),
    )

    from curator.core.apply import validate_plan

    validate_plan(plan)
    return plan
