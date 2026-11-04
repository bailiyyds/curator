"""Preview and confirmation adapters for stage-three operations."""

from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from curator.core.apply import apply_plan, write_artifact
from curator.core.config import load_config
from curator.core.plan import rename_plan
from curator.core.undo import latest_journals, undo_journal
from curator.models.changes import OperationReport, Plan
from curator.safety.paths import SafetyError


def register(app: typer.Typer, console: Console) -> None:
    """Attach mutation commands while keeping business logic in core modules."""

    def confirm(yes: bool) -> None:
        if not yes and typer.prompt("Type yes to write files", default="no") != "yes":
            raise typer.Exit(0)

    def preview(plan: Plan) -> None:
        console.print(f"Root: {plan.root}")
        table = Table("Old path", "New path", "Reason", "Confidence", "Risk")
        for change in plan.changes:
            table.add_row(
                change.src, change.dst, change.reason, f"{change.confidence:.0%}", change.risk
            )
        console.print(table)
        for warning in plan.warnings:
            console.print(warning, style="yellow")

    def outcome(report: OperationReport, json: bool) -> None:
        if json:
            console.print_json(report.model_dump_json())
        else:
            console.print(f"Journal: {report.journal or '(dry-run)'}")
            for entry in report.entries:
                console.print(f"{entry.change_id}: {entry.result}; {entry.src} -> {entry.dst}")
                if entry.error:
                    console.print(entry.error, style="yellow")
            for warning in report.warnings:
                console.print(warning, style="yellow")
        if not report.dry_run and any(e.result != "ok" for e in report.entries):
            raise typer.Exit(4)

    def fail(error: Exception, json: bool) -> None:
        if json:
            console.print_json(data={"error": str(error)})
        else:
            console.print(str(error), style="red")
        raise typer.Exit(3 if isinstance(error, SafetyError) else 1) from error

    @app.command("rename")
    def rename_command(
        path: Path,
        template: str = "{slug}",
        source: str = "title",
        threshold: Annotated[float, typer.Option(min=0, max=1)] = 0.7,
        apply: bool = typer.Option(False, "--apply/--dry-run"),
        yes: bool = False,
        allow_high_risk: bool = False,
        json: bool = False,
        output: Annotated[str | None, typer.Option("--output", "-o")] = None,
    ) -> None:
        """Preview renames; --apply requires confirmation unless --yes is supplied."""
        try:
            plan = rename_plan(
                path,
                template=template,
                source=source,
                threshold=threshold,
                config=load_config(path.resolve()),
            )
            if not json:
                preview(plan)
            if not apply:
                if json:
                    console.print_json(
                        data={
                            "plan": plan.model_dump(mode="json"),
                            "dry_run": True,
                            "output_preview": output,
                        }
                    )
                return
            confirm(yes)
            if output:
                exported = write_artifact(
                    Path(plan.root), output, plan.model_dump_json(indent=2).encode(), apply=True
                )
                if any(e.result != "ok" for e in exported.entries):
                    outcome(exported, json)
                    return
            outcome(apply_plan(plan, apply=True, allow_high_risk=allow_high_risk), json)
        except (OSError, ValueError) as error:
            fail(error, json)

    @app.command("plan")
    def plan_command(
        path: Path,
        output: Annotated[str, typer.Option("--output", "-o")] = "plan.json",
        template: str = "{slug}",
        apply: bool = typer.Option(False, "--apply/--dry-run"),
        yes: bool = False,
        json: bool = False,
    ) -> None:
        """Preview a rename plan; --apply exports only the plan, without renaming."""
        try:
            plan = rename_plan(path, template=template, config=load_config(path.resolve()))
            if not json:
                preview(plan)
                console.print(f"Plan export: {output}; apply={apply}")
            if apply:
                confirm(yes)
                outcome(
                    write_artifact(
                        Path(plan.root), output, plan.model_dump_json(indent=2).encode(), apply=True
                    ),
                    json,
                )
            elif json:
                console.print_json(plan.model_dump_json())
        except (OSError, ValueError) as error:
            fail(error, json)

    @app.command("apply")
    def apply_command(
        plan_file: Path,
        apply: bool = typer.Option(False, "--apply/--dry-run"),
        yes: bool = False,
        allow_high_risk: bool = False,
        json: bool = False,
    ) -> None:
        """Inspect an exported plan; mutate files only with --apply."""
        try:
            plan = Plan.model_validate_json(plan_file.read_text(encoding="utf-8"))
            if not json:
                preview(plan)
            if apply:
                confirm(yes)
            outcome(apply_plan(plan, apply=apply, allow_high_risk=allow_high_risk), json)
        except (OSError, ValueError) as error:
            fail(error, json)

    @app.command("undo")
    def undo_command(
        path: Annotated[Path, typer.Argument()] = Path("."),
        journal: Path | None = None,
        last: Annotated[int, typer.Option(min=1)] = 1,
        apply: bool = typer.Option(False, "--apply/--dry-run"),
        yes: bool = False,
        json: bool = False,
    ) -> None:
        """Preview or restore original transactions in reverse chronological order."""
        try:
            paths = [journal] if journal else latest_journals(path.resolve(), last)
            if apply:
                confirm(yes)
            reports = [undo_journal(p, apply=apply) for p in paths]
            combined = OperationReport(
                dry_run=not apply,
                entries=[e for r in reports for e in r.entries],
                warnings=[w for r in reports for w in r.warnings],
                journal=reports[-1].journal if reports else None,
            )
            outcome(combined, json)
        except (OSError, ValueError) as error:
            fail(error, json)
