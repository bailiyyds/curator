"""Documentation preview, merge diff and explicit batch publication."""

import logging
from enum import Enum
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from curator.analyze.docgen import generate_docs
from curator.core.artifacts import write_artifacts
from curator.core.config import load_config
from curator.safety.paths import SafetyError


class DocSource(str, Enum):
    """Available documentation evidence sources."""

    AST = "ast"
    GIT = "git"
    BOTH = "both"


class DocSplit(str, Enum):
    """Output grouping mode."""

    MODULE = "module"
    FILE = "file"


def register_doc(app: typer.Typer, console: Console) -> None:
    """Register documentation generation with default no-write preview."""

    @app.command("doc")
    def doc_command(
        path: Path,
        out: str = "docs",
        source: Annotated[DocSource, typer.Option("--from")] = DocSource.BOTH,
        split: DocSplit = DocSplit.MODULE,
        apply: bool = typer.Option(False, "--apply/--dry-run"),
        yes: bool = False,
        json: bool = False,
        git_max_commits: Annotated[int | None, typer.Option(min=1, max=10000)] = None,
    ) -> None:
        """Preview Markdown API docs and Git activity without executing source code."""
        try:
            overrides = (
                {"doc_git_max_commits": git_max_commits} if git_max_commits is not None else {}
            )
            plan = generate_docs(
                path,
                out=out,
                source=source.value,
                split=split.value,
                config=load_config(path.resolve(), overrides),
            )
            if not json:
                table = Table("Output", "Sources", "Action")
                for artifact in plan.artifacts:
                    table.add_row(
                        artifact.path,
                        ", ".join(artifact.source_paths),
                        "create" if artifact.write_required else "up to date",
                    )
                console.print(table)
                for artifact in plan.artifacts:
                    if artifact.diff:
                        console.print(artifact.diff, markup=False)
                for item in plan.skipped:
                    console.print(f"Skipped {item.path}: {item.reason}")
                for warning in plan.warnings:
                    console.print(warning, style="yellow")
            report = None
            if apply:
                if (
                    not yes
                    and typer.prompt("Type yes to write generated documentation", default="no")
                    != "yes"
                ):
                    raise typer.Exit(0)
                payloads = {
                    a.path: a.content.encode("utf-8") for a in plan.artifacts if a.write_required
                }
                report = write_artifacts(Path(plan.root), payloads, apply=True)
            if json:
                console.print_json(
                    data={
                        "plan": plan.model_dump(mode="json"),
                        "dry_run": not apply,
                        "result": report.model_dump(mode="json") if report else None,
                    }
                )
            elif report:
                console.print(f"Journal: {report.journal or '(nothing to write)'}")
                for entry in report.entries:
                    console.print(f"{entry.src or entry.dst}: {entry.result}; {entry.error or ''}")
            if plan.errors or report and any(e.result != "ok" for e in report.entries):
                raise typer.Exit(4)
        except (typer.Exit, typer.BadParameter):
            raise
        except (OSError, ValueError) as error:
            if json:
                console.print_json(data={"error": str(error)})
            else:
                console.print(str(error), style="red")
            raise typer.Exit(3 if isinstance(error, SafetyError) else 1) from error
        except Exception as error:
            logging.getLogger(__name__).debug("Documentation failure", exc_info=True)
            if json:
                console.print_json(data={"error": type(error).__name__})
            else:
                console.print(f"Documentation failed: {type(error).__name__}", style="red")
            raise typer.Exit(1) from error
