"""Optional title suggestions feed the existing deterministic safe planner."""

import asyncio
import json
import logging
from importlib.resources import files
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from curator.core.config import Config
from curator.core.scan import scan
from curator.extract import extract_file
from curator.llm.provider import Provider


class TitleDecision(BaseModel):
    """Only title text and confidence can enter a filename proposal."""

    model_config = ConfigDict(extra="forbid", strict=True)
    title: str = Field(min_length=1, max_length=120)
    confidence: float = Field(ge=0, le=1)


def validate_title(content: str) -> None:
    """Reject unexpected fields and invalid title types before caching."""
    TitleDecision.model_validate_json(content)


async def suggest_titles(
    root: Path, provider: Provider, config: Config, *, languages: list[str] | None = None
) -> tuple[dict[str, tuple[str, float]], list[str]]:
    """Send bounded partial document excerpts only when a reliable local title is absent."""
    unavailable = provider.unavailable()
    if unavailable:
        return {}, [unavailable]
    system = files("curator.llm").joinpath("prompts/title.txt").read_text(encoding="utf-8")
    inventory = scan(root, config)
    tasks: list[tuple[str, str]] = []
    warnings: list[str] = []
    for entry in inventory.files:
        if entry.error or entry.kind not in {"markdown", "text", "pdf"}:
            continue
        extracted = extract_file(root, entry.path, config.max_size)
        doc = extracted.document
        if doc is None:
            warnings.append(f"{entry.path}: title extraction failed")
            continue
        if doc.title or any(h.level == 1 for h in doc.headings):
            continue
        if len(tasks) >= provider.config.max_tasks:
            warnings.append(f"{entry.path}: LLM title task budget exceeded")
            continue
        if len(doc.body.strip()) < 8:
            warnings.append(f"{entry.path}: insufficient text for partial title suggestion")
            continue
        # Always withhold at least half of short documents, even when the whole file would fit.
        length = min(len(doc.body) // 2, 1600)
        prompt = ""
        while length >= 4:
            prompt = json.dumps(
                {
                    "path": entry.path[:180],
                    "partial_excerpt": True,
                    "text": doc.body[:length],
                    "preferred_languages": languages or ["zh", "en"],
                },
                ensure_ascii=False,
            )
            if len(system.encode()) + len(prompt.encode()) + 128 <= provider.config.input_tokens:
                break
            length //= 2
        if length < 4:
            warnings.append(f"{entry.path}: title prompt exceeds budget")
            continue
        tasks.append((entry.path, prompt))
    semaphore = asyncio.Semaphore(provider.config.concurrency)

    async def one(path: str, prompt: str) -> tuple[str, TitleDecision | None, str | None]:
        try:
            async with semaphore:
                completion = await provider.complete(system, prompt, validate_title)
                if completion.content is None:
                    return path, None, completion.error or "No title completion"
                try:
                    decision = TitleDecision.model_validate_json(completion.content)
                    return path, decision, "; ".join(completion.warnings) or None
                except ValueError:
                    return path, None, "Invalid title schema"
        except Exception as error:
            logging.getLogger(__name__).debug("Title task failure", exc_info=True)
            return path, None, f"Isolated title failure: {type(error).__name__}"

    titles: dict[str, tuple[str, float]] = {}
    for path, decision, error in await asyncio.gather(*(one(*task) for task in tasks)):
        if error:
            warnings.append(f"{path}: {error}")
        if decision:
            titles[path] = (decision.title, decision.confidence)
    return titles, warnings
