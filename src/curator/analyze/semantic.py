"""Optional local-only sentence-transformer search with conservative model validation."""

import hashlib
import importlib
import json
import math
from itertools import islice
from pathlib import Path
from typing import Any

from curator.core.journal import sha256
from curator.core.search import SearchHit
from curator.core.search_index import VectorIndex, read_index
from curator.safety.limits import SEMANTIC_CHARACTERS, SEMANTIC_CHUNKS


def semantic_hits(
    root: Path, model_path: Path | None, query: str, texts: dict[str, list[str]], top: int
) -> list[SearchHit]:
    """Return local semantic matches without writing a vector index."""
    return semantic_search(root, model_path, query, texts, top)[0]


def semantic_search(
    root: Path,
    model_path: Path | None,
    query: str,
    texts: dict[str, list[str]],
    top: int,
    cached: VectorIndex | None = None,
    source_fingerprint: str | None = None,
) -> tuple[list[SearchHit], VectorIndex | None]:
    """Load trusted local assets only, never model code or automatic downloads."""
    if model_path is None:
        raise ValueError("Provide --model-path pointing to a trusted local model")
    model_path = model_path.resolve(strict=True)
    if model_path.is_relative_to(root) or not model_path.is_dir():
        raise ValueError("The trusted model directory must be outside the scanned root")
    # Reject model links and executable/pickle payloads; custom modules are not supported.
    assets: list[Path] = []
    for asset in model_path.rglob("*"):
        if len(assets) >= 10000:
            raise ValueError("Model asset budget exceeded")
        if asset.is_symlink() or asset.is_junction():
            raise ValueError("Linked model assets are not supported")
        assets.append(asset)
    modules_path = model_path / "modules.json"
    if modules_path.stat().st_size > 65536:
        raise ValueError("Oversized model module manifest")
    modules = json.loads(modules_path.read_text(encoding="utf-8"))
    allowed = {
        "sentence_transformers.models." + name for name in ("Transformer", "Pooling", "Normalize")
    }
    if (
        not isinstance(modules, list)
        or not modules
        or any(
            not isinstance(module, dict)
            or module.get("type") not in allowed
            or not isinstance(module.get("path"), str)
            or not (model_path / module["path"]).resolve().is_relative_to(model_path)
            for module in modules
        )
    ):
        raise ValueError("Only built-in Transformer/Pooling/Normalize modules are supported")
    if not any(p.suffix == ".safetensors" for p in assets):
        raise ValueError("Safetensors model weights are required")
    if any(p.suffix in {".py", ".bin", ".pt", ".pth", ".pkl", ".pickle"} for p in assets):
        raise ValueError("Executable or pickle model assets are not supported")
    model_fingerprint = hashlib.sha256(
        "\n".join(
            asset.relative_to(model_path).as_posix() + sha256(asset)
            for asset in sorted(assets)
            if asset.is_file()
        ).encode()
    ).hexdigest()
    if source_fingerprint:
        key = hashlib.sha256((source_fingerprint + model_fingerprint).encode()).hexdigest()
        stored = read_index(root, key)
        if stored:
            cached = stored.vectors
    package = importlib.import_module("sentence_transformers")
    model: Any = package.SentenceTransformer(
        str(model_path),
        local_files_only=True,
        trust_remote_code=False,
        device="cpu",
        model_kwargs={"use_safetensors": True},
    )
    # Bound inference work independently from the keyword index.
    chunks = list(
        islice(
            (
                (path, n + 1, line[:SEMANTIC_CHARACTERS])
                for path, lines in texts.items()
                for n, line in enumerate(lines)
                if line.strip()
            ),
            SEMANTIC_CHUNKS,
        )
    )
    if not chunks:
        return [], None
    reusable = (
        cached is not None
        and cached.model_fingerprint == model_fingerprint
        and cached.chunks == chunks
    )
    inputs = (
        [query[:SEMANTIC_CHARACTERS]]
        if reusable
        else [query[:SEMANTIC_CHARACTERS]] + [v[2] for v in chunks]
    )
    vectors = model.encode(
        inputs,
        normalize_embeddings=True,
        show_progress_bar=False,
        batch_size=16,
    ).tolist()
    if reusable and cached:
        vectors = vectors + cached.vectors
    if len(vectors) != len(chunks) + 1:
        raise ValueError("Unexpected embedding batch size")
    query_vector = vectors[0]
    hits = []
    for (path, line, snippet), vector in zip(chunks, vectors[1:], strict=True):
        if len(query_vector) != len(vector) or not 0 < len(vector) <= 4096:
            raise ValueError("Unexpected embedding dimensions")
        score = sum(float(a) * float(b) for a, b in zip(query_vector, vector, strict=True))
        if not math.isfinite(score):
            raise ValueError("Nonfinite embedding score")
        hits.append(SearchHit(path=path, line=line, snippet=snippet[:400], score=score))
    return sorted(hits, key=lambda hit: (-hit.score, hit.path, hit.line))[:top], VectorIndex(
        model_fingerprint=model_fingerprint, chunks=chunks, vectors=vectors[1:]
    )
