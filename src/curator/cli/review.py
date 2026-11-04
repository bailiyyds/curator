"""Review rendering, explicit artifact export and parameter validation."""

import asyncio
import logging
import re
from enum import Enum
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from curator.analyze.code import review_directory
from curator.core.apply import write_artifact
from curator.core.config import load_config
from curator.llm.provider import Provider
from curator.llm.reviewer import review_findings
from curator.models.findings import ReviewReport
from curator.safety.paths import SafetyError


class ReviewFormat(str, Enum):
    """Supported report representations."""

    TEXT = "text"
    JSON = "json"
    MARKDOWN = "md"


def render_text(report: ReviewReport) -> str:
    """Render findings with exact file:line:column and original evidence."""
    lines = [
        f"Reviewed {report.files_reviewed} files; {len(report.findings)} findings; {len(report.skipped)} skipped"
    ]
    for finding in report.findings:
        lines.extend(
            [
                f"{finding.path}:{finding.line}:{finding.col}: {finding.severity} [{finding.rule}; source={finding.source}] {finding.message}",
                f"  {finding.evidence}",
                f"  Suggestion: {finding.suggestion}",
            ]
        )
        if finding.llm_confirmed is not None:
            lines.append(f"  LLM confirmed={finding.llm_confirmed}: {finding.llm_reason}")
        if finding.patch:
            lines.extend(["  Suggested patch (not applied):", finding.patch])
    lines.append(f"Markers: {report.marker_counts}")
    lines.extend(f"Skipped {s.path}: {s.reason}" for s in report.skipped)
    lines.extend(f"Warning: {w}" for w in report.warnings)
    return "\n".join(lines)


def render_markdown(report: ReviewReport) -> str:
    """Render standalone Markdown while fencing arbitrary source evidence safely."""
    lines = [
        "# Code review",
        "",
        f"Files: {report.files_reviewed}; findings: {len(report.findings)}",
        "",
    ]
    for finding in report.findings:
        suggestion_fence = "`" * (
            max([len(m.group()) for m in re.finditer(r"`+", finding.suggestion or "")] + [2]) + 1
        )
        fence = "`" * (
            max([len(m.group()) for m in re.finditer(r"`+", finding.evidence)] + [2]) + 1
        )
        lines.extend(
            [
                f"## {finding.path}:{finding.line}:{finding.col}",
                "",
                f"{finding.severity} · {finding.rule} · source={finding.source}",
                "",
                finding.message,
                "",
                fence + "text",
                finding.evidence,
                fence,
                "",
                "Suggestion:",
                suggestion_fence + "text",
                finding.suggestion or "",
                suggestion_fence,
                "",
            ]
        )
        if finding.llm_confirmed is not None:
            reason = finding.llm_reason or ""
            reason_fence = "`" * (
                max([len(m.group()) for m in re.finditer(r"`+", reason)] + [2]) + 1
            )
            lines.extend(
                [
                    f"LLM confirmed: {finding.llm_confirmed}",
                    reason_fence + "text",
                    reason,
                    reason_fence,
                    "",
                ]
            )
        if finding.patch:
            patch_fence = "`" * (
                max([len(m.group()) for m in re.finditer(r"`+", finding.patch)] + [2]) + 1
            )
            lines.extend(
                [
                    "Suggested patch (not applied):",
                    patch_fence + "diff",
                    finding.patch,
                    patch_fence,
                    "",
                ]
            )
    lines.extend(
        [
            "## Counts",
            "",
            f"Rules: {report.rule_counts}",
            f"Markers: {report.marker_counts}",
            "",
            "## Skipped files",
            "",
        ]
    )
    lines.extend(f"- {s.path}: {s.reason}" for s in report.skipped)
    lines.extend(["", "## Warnings", "", *report.warnings])
    return "\n".join(lines)


def register_review(app: typer.Typer, console: Console) -> None:
    """Register a read-only review command; exports require explicit write opt-in."""

    @app.command("review")
    def review_command(
        path: Path,
        lang: str | None = None,
        rules: str = "all",
        severity: str = "warning+",
        llm: bool = typer.Option(False, "--llm/--no-llm"),
        json: bool = False,
        format: ReviewFormat = ReviewFormat.TEXT,
        output: Annotated[str | None, typer.Option("--output", "-o")] = None,
        apply: bool = typer.Option(False, "--apply/--dry-run"),
        yes: bool = False,
        base_url: str | None = None,
        model: str | None = None,
        timeout: Annotated[float | None, typer.Option(min=0.001, max=600)] = None,
        retries: Annotated[int | None, typer.Option(min=0, max=3)] = None,
        input_tokens: Annotated[int | None, typer.Option(min=256, max=32000)] = None,
        max_tokens: Annotated[int | None, typer.Option(min=1, max=8192)] = None,
        concurrency: Annotated[int | None, typer.Option(min=1, max=32)] = None,
        llm_max_tasks: Annotated[int | None, typer.Option(min=1, max=10000)] = None,
        no_cache: bool = typer.Option(False, "--no-cache"),
        write_cache: bool = False,
    ) -> None:
        """Review source files offline, without executing or rewriting inspected code."""
        try:
            if apply and output is None and not write_cache:
                raise typer.BadParameter("--apply requires -o for report export")
            if write_cache and (not llm or not apply or no_cache):
                raise typer.BadParameter(
                    "--write-cache requires --llm --apply and cannot combine with --no-cache"
                )
            overrides = {
                key: value
                for key, value in {
                    "base_url": base_url,
                    "model": model,
                    "timeout": timeout,
                    "retries": retries,
                    "input_tokens": input_tokens,
                    "max_tokens": max_tokens,
                    "concurrency": concurrency,
                    "max_tasks": llm_max_tasks,
                }.items()
                if value is not None
            }
            config = load_config(path.resolve(), {"llm": overrides})
            confirmed_write = False
            if write_cache:
                if (
                    not yes
                    and typer.prompt("Type yes to write LLM response cache", default="no") != "yes"
                ):
                    raise typer.Exit(0)
                confirmed_write = True
            report = review_directory(
                path,
                languages=lang,
                rules=rules,
                severity=severity,
                config=config,
            )
            if llm:
                settings = config.llm.model_copy(
                    update={
                        "use_cache": config.llm.use_cache and not no_cache,
                        "write_cache": write_cache and apply,
                    }
                )
                provider = Provider(settings, Path(report.root))
                report = asyncio.run(review_findings(report, provider, config))
            chosen = ReviewFormat.JSON if json else format
            payload = (
                report.model_dump_json(indent=2)
                if chosen == ReviewFormat.JSON
                else render_markdown(report)
                if chosen == ReviewFormat.MARKDOWN
                else render_text(report)
            )
            exported = None
            if output and apply:
                if (
                    not confirmed_write
                    and not yes
                    and typer.prompt("Type yes to export review", default="no") != "yes"
                ):
                    raise typer.Exit(0)
                exported = write_artifact(
                    Path(report.root), output, payload.encode("utf-8"), apply=True
                )
            if chosen == ReviewFormat.JSON:
                console.print_json(report.model_dump_json())
            else:
                console.print(payload, markup=False)
                if output:
                    console.print(f"Output: {output}; written={bool(exported)}")
                if exported:
                    console.print(f"Journal: {exported.journal}")
            if report.errors or exported and any(e.result != "ok" for e in exported.entries):
                raise typer.Exit(4)
        except (typer.Exit, typer.BadParameter):
            raise
        except SafetyError as error:
            if json or format == ReviewFormat.JSON:
                console.print_json(data={"error": str(error)})
            else:
                console.print(str(error), style="red")
            raise typer.Exit(3) from error
        except ValueError as error:
            if json or format == ReviewFormat.JSON:
                console.print_json(data={"error": str(error)})
                raise typer.Exit(2) from error
            raise typer.BadParameter(str(error)) from error
        except Exception as error:
            logging.getLogger(__name__).debug("Unexpected review failure", exc_info=True)
            if json or format == ReviewFormat.JSON:
                console.print_json(data={"error": str(error)})
            else:
                console.print(str(error), style="red")
            raise typer.Exit(1) from error
