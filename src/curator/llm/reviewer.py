"""Budgeted finding-only review. Model output is never executed or applied."""

import asyncio
import json
import logging
import re
from importlib.resources import files
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from curator.core.config import Config
from curator.extract import extract_file
from curator.llm.provider import Completion, Provider
from curator.models.findings import Finding, ReviewReport


class Decision(BaseModel):
    """Strict, bounded review response; strings cannot masquerade as booleans."""

    model_config = ConfigDict(extra="forbid", strict=True)
    confirmed: bool
    reason: str = Field(min_length=1, max_length=2000)
    suggestion: str = Field(min_length=1, max_length=4000)
    patch: str | None = Field(max_length=16000)


def validate_decision(content: str) -> None:
    """Reject schema-invalid completions before they can enter the disk cache."""
    Decision.model_validate_json(content)


def safe_patch(patch: str, path: str) -> bool:
    """Accept only a structurally complete unified diff scoped to one known file."""
    lines = patch.splitlines()
    if lines and lines[0] == f"diff --git a/{path} b/{path}":
        lines = lines[1:]
    if len(lines) < 3 or lines[:2] != [f"--- a/{path}", f"+++ b/{path}"]:
        return False
    old = new = expected_old = expected_new = 0
    hunks = 0
    for line in lines[2:]:
        if line.startswith(("--- ", "+++ ")):
            return False
        if line.startswith("@@"):
            if hunks and (old != expected_old or new != expected_new):
                return False
            header = re.fullmatch(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@.*", line)
            if header is None:
                return False
            expected_old, expected_new = int(header[2] or 1), int(header[4] or 1)
            old = new = 0
            hunks += 1
        elif line == "\\ No newline at end of file":
            continue
        elif not hunks or not line or line[0] not in " +-":
            return False
        else:
            old += line[0] in " -"
            new += line[0] in " +"
    return bool(hunks and old == expected_old and new == expected_new)


def build_prompt(
    finding: Finding, text: str, language: str, system: str, budget: int
) -> str | None:
    """Select at most five context lines per side and never send a complete file."""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    index = finding.line - 1
    if index < 0 or index >= len(lines) or lines[index] != finding.evidence:
        return None
    meaningful = [i for i, line in enumerate(lines) if line.strip()]
    if len(meaningful) <= 1:
        return None
    chosen = list(range(max(0, index - 5), min(len(lines), index + 6)))
    if all(i in chosen for i in meaningful):
        exclude = max((i for i in meaningful if i != index), key=lambda i: abs(i - index))
        chosen = [i for i in chosen if i != exclude]

    def payload(snippet: str) -> str:
        return json.dumps(
            {
                "path": finding.path[:180],
                "language": language,
                "rule": finding.rule[:200],
                "line": finding.line,
                "col": finding.col,
                "partial_excerpt": True,
                "snippet": snippet,
            },
            ensure_ascii=False,
        )

    def fits(value: str) -> bool:
        return len(system.encode()) + len(value.encode()) + 128 <= budget

    while True:
        snippet = "\n".join(f"{i + 1}: {lines[i]}" for i in chosen)
        value = payload(snippet)
        if fits(value):
            return value
        if len(chosen) > 1:
            chosen.remove(max((i for i in chosen if i != index), key=lambda i: abs(i - index)))
            continue
        target = lines[index]
        width = len(target)
        while width > 8:
            width //= 2
            center = max(0, finding.col - 1)
            start = max(0, min(center - width // 2, len(target) - width))
            value = payload(f"{index + 1}: [trimmed] {target[start : start + width]} [trimmed]")
            if fits(value):
                return value
        return None


async def review_findings(report: ReviewReport, provider: Provider, config: Config) -> ReviewReport:
    """Review bounded slices concurrently, preserving every deterministic finding."""
    result = report.model_copy(deep=True)
    reason = provider.unavailable()
    if reason:
        result.warnings.append(reason)
        return result
    system = files("curator.llm").joinpath("prompts/review.txt").read_text(encoding="utf-8")
    semaphore = asyncio.Semaphore(provider.config.concurrency)
    selected = result.findings[: provider.config.max_tasks]
    stats = {
        "tasks": len(selected),
        "requests": 0,
        "confirmed": 0,
        "unconfirmed": 0,
        "failed": 0,
        "skipped": len(result.findings) - len(selected),
        "cache_hits": 0,
    }
    if stats["skipped"]:
        result.warnings.append(
            f"LLM task budget exceeded: {stats['skipped']} findings retain rule results"
        )
    # Retain only bounded prompts, not full source bodies for every selected file.
    grouped: dict[str, list[Finding]] = {}
    for finding in selected:
        grouped.setdefault(finding.path, []).append(finding)
    prompts: dict[str, str | None] = {}
    for path, findings in grouped.items():
        extracted = extract_file(Path(report.root), path, config.max_size)
        for finding in findings:
            prompts[finding.id] = (
                build_prompt(
                    finding,
                    extracted.document.body,
                    extracted.document.language or "unknown",
                    system,
                    provider.config.input_tokens,
                )
                if extracted.document
                else None
            )
        del extracted

    async def one(finding: Finding) -> tuple[Completion | None, str | None]:
        try:
            prompt = prompts.get(finding.id)
            if prompt is None:
                return (
                    None,
                    "Context unavailable, changed, too short or over budget; full files are never sent",
                )
            async with semaphore:
                return await provider.complete(system, prompt, validate_decision), None
        except Exception as error:
            logging.getLogger(__name__).debug("Review task failure", exc_info=True)
            return None, f"Isolated review failure: {type(error).__name__}"

    completions = await asyncio.gather(*(one(finding) for finding in selected))
    for finding, (completion, skipped) in zip(selected, completions, strict=True):
        if skipped or completion is None:
            stats["skipped"] += 1
            result.warnings.append(f"{finding.id}: {skipped}")
            continue
        stats["requests"] += completion.attempts
        stats["cache_hits"] += int(completion.cache_hit)
        result.warnings.extend(f"{finding.id}: {w}" for w in completion.warnings)
        if completion.error or completion.content is None:
            stats["failed"] += 1
            result.warnings.append(f"{finding.id}: {completion.error}; retaining rule result")
            continue
        try:
            decision = Decision.model_validate_json(completion.content)
        except ValueError:
            stats["failed"] += 1
            result.warnings.append(f"{finding.id}: invalid review schema; retaining rule result")
            continue
        finding.llm_confirmed, finding.llm_reason = decision.confirmed, decision.reason
        stats["confirmed" if decision.confirmed else "unconfirmed"] += 1
        if decision.confirmed:
            finding.source = "llm"
            finding.suggestion = decision.suggestion
            if decision.patch:
                if safe_patch(decision.patch, finding.path):
                    finding.patch = decision.patch
                else:
                    result.warnings.append(f"{finding.id}: unsafe or malformed patch discarded")
    result.llm_stats = stats
    return result
