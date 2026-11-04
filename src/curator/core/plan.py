"""Deterministic rename proposals; no writes and no LLM network calls."""

import hashlib
import re
from datetime import UTC, datetime
from pathlib import Path
from string import Formatter

from curator.core.config import Config
from curator.core.scan import scan
from curator.extract import extract_file
from curator.models.changes import Change, Plan
from curator.safety.paths import RESERVED, safe_path


def slugify(value: str) -> str:
    """Keep Chinese, Latin letters and digits, collapsing punctuation to hyphens."""
    slug = re.sub(r"[^a-zA-Z0-9\u3400-\u9fff]+", "-", value).strip("-.")[:80].rstrip("-.")
    if not slug:
        slug = "untitled"
    if slug.casefold() in RESERVED:
        slug = "file-" + slug
    return slug


def rename_plan(
    root: Path,
    *,
    template: str = "{slug}",
    source: str = "title",
    threshold: float = 0.7,
    config: Config | None = None,
    llm_titles: dict[str, tuple[str, float]] | None = None,
) -> Plan:
    """Choose titles in priority order and reserve casefolded destination names."""
    if source not in {"title", "frontmatter", "llm"}:
        raise ValueError("Source must be title, frontmatter or llm")
    if not 0 <= threshold <= 1:
        raise ValueError("Confidence threshold must be between zero and one")
    for _, field, spec, conversion in Formatter().parse(template):
        if field is not None and (field not in {"date", "topic", "slug"} or spec or conversion):
            raise ValueError("Template accepts only plain {date}, {topic}, {slug} fields")
    root = root.resolve(strict=True)
    config = config or Config()
    inventory = scan(root, config)
    parents = {(root / entry.path).parent for entry in inventory.files}
    occupied = {
        p.relative_to(root).as_posix().casefold() for parent in parents for p in parent.iterdir()
    }
    changes: list[Change] = []
    warnings = [f"{s.path}: {s.reason}" for s in inventory.skipped]
    if source == "llm" and llm_titles is None:
        warnings.append("LLM titles were not supplied; using deterministic fallbacks")
    for entry in inventory.files:
        if entry.error:
            warnings.append(f"{entry.path}: {entry.error}")
            continue
        title: object = None
        confidence, reason = 0.5, "cleaned original filename; no reliable title"
        if entry.kind in {"markdown", "text", "pdf"}:
            result = extract_file(root, entry.path, config.max_size)
            if result.error or result.document is None:
                warnings.append(f"{entry.path}: {result.error}")
                continue
            doc = result.document
            title = doc.frontmatter.get("title")
            confidence, reason = 0.95, "frontmatter.title"
            if not isinstance(title, str) or not title.strip():
                title = next((h.text for h in doc.headings if h.level == 1), None)
                confidence, reason = 0.9, "first level-one heading"
        if (not isinstance(title, str) or not title) and llm_titles and entry.path in llm_titles:
            title, confidence = llm_titles[entry.path]
            reason = "LLM title suggestion; review before applying"
        if not isinstance(title, str) or not title:
            title = Path(entry.path).stem
            confidence, reason = 0.5, "cleaned original filename; no reliable title"
        name = slugify(
            template.format(
                slug=slugify(title),
                topic=slugify(title),
                date=datetime.fromtimestamp(entry.mtime, UTC).strftime("%Y-%m-%d"),
            )
        )
        old = Path(entry.path)
        suffix = old.suffix.lower()
        dst = old.with_name(name + suffix).as_posix()
        if dst == entry.path:
            continue
        # Avoid case-only renames to keep undo unambiguous across platforms.
        counter = 2
        while dst.casefold() in occupied:
            dst = old.with_name(
                name[: 80 - len(str(counter)) - 1].rstrip("-") + f"-{counter}" + suffix
            ).as_posix()
            counter += 1
        safe_path(root, dst)
        occupied.add(dst.casefold())
        changes.append(
            Change(
                id=f"chg_{len(changes) + 1:04d}",
                op="rename",
                src=entry.path,
                dst=dst,
                reason=reason,
                confidence=confidence,
                risk="high"
                if confidence < threshold
                else "medium"
                if reason.startswith("LLM")
                else "low",
                pre_sha256=entry.sha256,
            )
        )
    payload = "\n".join(c.model_dump_json() for c in changes)
    return Plan(
        plan_id="plan_" + hashlib.sha256(payload.encode()).hexdigest()[:16],
        root=str(root),
        created_at=inventory.created_at,
        changes=changes,
        warnings=warnings,
    )
