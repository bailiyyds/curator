"""Bounded local inverted-index search with explicit offline fallback."""

import hashlib
import logging
import re
from pathlib import Path

from pydantic import BaseModel, Field

from curator.core.config import Config
from curator.core.scan import scan
from curator.core.search_index import VectorIndex
from curator.extract import extract_file
from curator.models import SkippedItem
from curator.safety.limits import SEARCH_CHARACTERS, SEARCH_DOCUMENTS, SEMANTIC_CHUNKS


class SearchHit(BaseModel):
    """A matching extracted-text line, with its location basis explicit."""

    path: str
    line: int
    snippet: str
    score: float
    line_basis: str = "extracted-text"


class SearchReport(BaseModel):
    """Read-only results and all extraction/limit warnings."""

    hits: list[SearchHit]
    skipped: list[SkippedItem]
    warnings: list[str]
    documents: int
    fingerprint: str
    postings: dict[str, dict[str, list[int]]] = Field(default_factory=dict, exclude=True)
    texts: dict[str, list[str]] = Field(default_factory=dict, exclude=True)
    vectors: VectorIndex | None = Field(default=None, exclude=True)


def tokens(text: str) -> set[str]:
    """Tokenize Latin words and Chinese characters/bigrams without model downloads."""
    result = set(re.findall(r"[a-z0-9_]+", text.casefold()))
    for run in re.findall(r"[\u3400-\u9fff]+", text):
        result.update(run)
        result.update(run[i : i + 2] for i in range(len(run) - 1))
    return result


def search(
    query: str,
    root: Path,
    *,
    top: int = 10,
    semantic: bool = False,
    model_path: Path | None = None,
    config: Config | None = None,
) -> SearchReport:
    """Search an in-memory inverted index; never write or download model assets."""
    if not query.strip() or top < 1:
        raise ValueError("Query must be nonempty and top must be positive")
    config = config or Config()
    inventory = scan(root, config)
    fingerprint = hashlib.sha256(
        (
            str(Path(inventory.root))
            + str(config.max_size)
            + "\n".join(f.path + f.sha256 for f in inventory.files)
        ).encode()
    ).hexdigest()
    skipped = inventory.skipped.copy()
    warnings = []
    index: dict[str, dict[str, list[int]]] = {}
    lines: dict[str, list[str]] = {}
    characters = 0
    cached = None
    try:
        from curator.core.search_index import read_index

        cached = read_index(Path(inventory.root), fingerprint)
    except (OSError, ValueError) as error:
        warnings.append(f"Index ignored: {type(error).__name__}")
    if cached:
        lines, index = cached.texts, cached.postings
        skipped = list(
            {(item.path, item.reason): item for item in skipped + cached.skipped}.values()
        )
        warnings.extend(cached.warnings)
    for entry in [] if cached else inventory.files:
        if entry.error or entry.kind in {"image", "binary", "unknown"}:
            skipped.append(
                SkippedItem(path=entry.path, reason=entry.error or "Not an extractable document")
            )
            continue
        if len(lines) >= SEARCH_DOCUMENTS or characters >= SEARCH_CHARACTERS:
            skipped.append(
                SkippedItem(path=entry.path, reason="Search document/character budget exceeded")
            )
            continue
        result = extract_file(Path(inventory.root), entry.path, config.max_size)
        if not result.document:
            skipped.append(SkippedItem(path=entry.path, reason=result.error or "Extraction failed"))
            continue
        body = result.document.body[: max(0, SEARCH_CHARACTERS - characters)]
        if len(body) < len(result.document.body):
            warnings.append(f"{entry.path}: text truncated by search budget")
        characters += len(body)
        values = body.splitlines()
        lines[entry.path] = values
        for number, line in enumerate(values):
            for term in tokens(line):
                index.setdefault(term, {}).setdefault(entry.path, []).append(number)
    terms = tokens(query)
    candidates = set(lines) if terms else set()
    for term in terms:
        candidates.intersection_update(index.get(term, {}))
    hits = []
    for path in candidates:
        scores: dict[int, int] = {}
        for term in terms:
            for number in index[term][path]:
                scores[number] = scores.get(number, 0) + 1
        number = min(scores, key=lambda n: (-scores[n], n))
        hits.append(
            SearchHit(
                path=path,
                line=number + 1,
                snippet=lines[path][number][:400],
                score=float(sum(scores.values())),
            )
        )
    vectors = None
    if semantic:
        try:
            from curator.analyze.semantic import semantic_search

            if (
                sum(bool(line.strip()) for values in lines.values() for line in values)
                > SEMANTIC_CHUNKS
            ):
                warnings.append(
                    "Semantic analysis limited to the first 512 nonempty extracted-text lines"
                )
            hits, vectors = semantic_search(
                root.resolve(),
                model_path,
                query,
                lines,
                top,
                cached.vectors if cached else None,
                source_fingerprint=fingerprint,
            )
        except Exception as error:
            logging.getLogger(__name__).debug("Optional semantic backend failed", exc_info=True)
            reason = (
                str(error)[:300]
                if isinstance(error, (ImportError, ValueError))
                else type(error).__name__
            )
            warnings.append(
                f"Semantic search unavailable ({reason}); falling back to offline keyword search"
            )
    return SearchReport(
        hits=sorted(hits, key=lambda h: (-h.score, h.path, h.line))[:top],
        skipped=skipped,
        warnings=warnings,
        documents=len(lines),
        fingerprint=fingerprint,
        postings=index,
        texts=lines,
        vectors=vectors,
    )
