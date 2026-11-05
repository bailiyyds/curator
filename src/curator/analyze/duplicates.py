"""SHA-256 exact duplicates followed by bounded simhash near-duplicate suggestions."""

import hashlib
from pathlib import Path

from pydantic import BaseModel

from curator.core.config import Config
from curator.core.scan import scan
from curator.core.search import tokens
from curator.extract import extract_file
from curator.models import SkippedItem
from curator.safety.limits import NEAR_DUPLICATE_CHARACTERS, NEAR_DUPLICATE_DOCUMENTS


class DuplicateReport(BaseModel):
    """Suggestions only; no duplicate is removed or moved."""

    exact: list[list[str]]
    near: list[tuple[str, str, int]]
    skipped: list[SkippedItem]
    warnings: list[str]


def simhash(text: str) -> int:
    """Compute a deterministic 64-bit token simhash."""
    weights = [0] * 64
    for token in tokens(text):
        value = int.from_bytes(hashlib.sha256(token.encode()).digest()[:8], "big")
        for bit in range(64):
            weights[bit] += 1 if value & (1 << bit) else -1
    return sum(1 << bit for bit, weight in enumerate(weights) if weight > 0)


def duplicates(root: Path, *, distance: int = 3, config: Config | None = None) -> DuplicateReport:
    """Group exact hashes and suggest close text fingerprints within a fixed budget."""
    if not 0 <= distance <= 10:
        raise ValueError("Simhash distance must be between zero and ten")
    config = config or Config()
    inventory = scan(root, config)
    groups: dict[str, list[str]] = {}
    skipped = inventory.skipped.copy()
    fingerprints: list[tuple[str, int]] = []
    warnings = []
    for entry in inventory.files:
        if entry.error:
            skipped.append(SkippedItem(path=entry.path, reason=entry.error))
            continue
        groups.setdefault(entry.sha256, []).append(entry.path)
    exact = [paths for _, paths in sorted(groups.items()) if len(paths) > 1]
    entries = {entry.path: entry for entry in inventory.files}
    for paths in groups.values():
        path = paths[0]
        if len(fingerprints) >= NEAR_DUPLICATE_DOCUMENTS:
            warnings.append(
                "Near-duplicate analysis limited to 512 unique files; exact deduplication is complete"
            )
            break
        entry = entries[path]
        if entry.kind not in {"text", "markdown", "code", "pdf"}:
            continue
        result = extract_file(Path(inventory.root), path, config.max_size)
        if result.document and result.document.body.strip():
            fingerprints.append((path, simhash(result.document.body[:NEAR_DUPLICATE_CHARACTERS])))
        elif result.error:
            skipped.append(SkippedItem(path=path, reason=result.error))
    near = []
    for i, (first, a) in enumerate(fingerprints):
        for second, b in fingerprints[i + 1 :]:
            bits = (a ^ b).bit_count()
            if bits <= distance:
                near.append((first, second, bits))
    return DuplicateReport(exact=exact, near=sorted(near), skipped=skipped, warnings=warnings)
