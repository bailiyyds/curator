"""Review rendering, explicit artifact export and parameter validation."""

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
                f"{finding.path}:{finding.line}:{finding.col}: {finding.severity} [{finding.rule}] {finding.message}",
                f"  {finding.evidence}",
                f"  Suggestion: {finding.suggestion}",
            ]
        )
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
        fence = "`" * (
            max([len(m.group()) for m in re.finditer(r"`+", finding.evidence)] + [2]) + 1
        )
        lines.extend(
            [
                f"## {finding.path}:{finding.line}:{finding.col}",
                "",
                f"{finding.severity} · {finding.rule}",
                "",
                finding.message,
                "",
                fence + "text",
                finding.evidence,
                fence,
                "",
                f"Suggestion: {finding.suggestion}",
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
    ) -> None:
        """Review source files offline, without executing or rewriting inspected code."""
        try:
            if apply and output is None:
                raise typer.BadParameter("--apply requires -o for report export")
            report = review_directory(
                path,
                languages=lang,
                rules=rules,
                severity=severity,
                config=load_config(path.resolve()),
            )
            if llm:
                report.warnings.append(
                    "LLM review is not implemented until stage six; all rule findings are retained"
                )
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
                if not yes and typer.prompt("Type yes to export review", default="no") != "yes":
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
