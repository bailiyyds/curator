"""CLI parameter parsing and presentation."""

from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console

from curator.core.config import load_config
from curator.core.scan import scan as scan_directory

app = typer.Typer(no_args_is_help=True)
console = Console()


@app.callback()
def main() -> None:
    """Curate local files with explicit, safe commands."""


def parse_size(value: str) -> int:
    """Convert a byte budget with an optional KB, MB, or GB suffix."""
    text = value.upper().strip()
    for suffix, factor in (("GB", 1024**3), ("MB", 1024**2), ("KB", 1024)):
        if text.endswith(suffix):
            return int(float(text[: -len(suffix)]) * factor)
    return int(text)


@app.command()
def scan(
    path: Path,
    json: bool = False,
    include: Annotated[list[str] | None, typer.Option()] = None,
    exclude: Annotated[list[str] | None, typer.Option()] = None,
    max_size: Annotated[str | None, typer.Option()] = None,
    max_files: Annotated[int | None, typer.Option(min=1)] = None,
    max_depth: Annotated[int | None, typer.Option(min=0)] = None,
    follow_symlinks: Annotated[bool | None, typer.Option()] = None,
    output: Annotated[str | None, typer.Option("--output", "-o")] = None,
    apply: bool = typer.Option(False, "--apply/--dry-run"),
    yes: bool = False,
) -> None:
    """Read a directory; optionally export with -o, --apply and confirmation."""
    try:
        overrides: dict[str, Any] = {
            key: value
            for key, value in {
                "include": include,
                "exclude": exclude,
                "max_files": max_files,
                "max_depth": max_depth,
                "follow_symlinks": follow_symlinks,
            }.items()
            if value is not None
        }
        if max_size is not None:
            overrides["max_size"] = parse_size(max_size)
        inventory = scan_directory(path, load_config(path.resolve(), overrides))
        export = None
        if apply and output is None:
            raise ValueError("scan --apply requires an explicit output path")
        if output and apply:
            from curator.core.apply import write_artifact

            if not yes and typer.prompt("Type yes to export inventory", default="no") != "yes":
                raise typer.Exit(0)
            export = write_artifact(
                Path(inventory.root),
                output,
                inventory.model_dump_json(indent=2).encode(),
                apply=True,
            )
        if json:
            if export:
                console.print_json(
                    data={
                        "inventory": inventory.model_dump(mode="json"),
                        "export": export.model_dump(mode="json"),
                    }
                )
            else:
                console.print_json(inventory.model_dump_json())
        else:
            console.print(f"Files: {len(inventory.files)}; skipped: {len(inventory.skipped)}")
            for item in inventory.skipped:
                console.print(f"{item.path}: {item.reason}")
            for entry in inventory.files:
                if entry.error:
                    console.print(f"{entry.path}: {entry.error}", style="red")
        if output and not json:
            console.print(f"Output: {output}; written={bool(export)}")
        if export and any(e.result != "ok" for e in export.entries):
            raise typer.Exit(4)
        if any(entry.error for entry in inventory.files):
            raise typer.Exit(4)
    except (OSError, ValueError) as error:
        if json:
            console.print_json(data={"error": str(error)})
        else:
            console.print(str(error), style="red")
        from curator.safety.paths import SafetyError

        raise typer.Exit(3 if isinstance(error, SafetyError) else 1) from error


@app.command("extract")
def extract_command(path: Path, json: bool = False) -> None:
    """Extract readable documents to the terminal without writing files."""
    from curator.extract import extract_directory

    try:
        report = extract_directory(path, load_config(path.resolve()))
        if json:
            console.print_json(report.model_dump_json())
        else:
            good = sum(r.document is not None for r in report.results)
            console.print(
                f"Documents: {good}; errors: {len(report.results) - good}; "
                f"skipped: {len(report.skipped)}"
            )
            console.print(f"{report.scanned_pdfs} 个 PDF 含无文本层页面，需要 OCR")
            for item in report.results:
                console.print(f"{item.path}: {item.error or 'ok'}")
                for warning in item.warnings:
                    console.print(warning, style="yellow")
            for skipped in report.skipped:
                console.print(f"{skipped.path}: {skipped.reason}")
        if any(item.error for item in report.results):
            raise typer.Exit(4)
    except (OSError, ValueError) as error:
        if json:
            console.print_json(data={"error": str(error)})
        else:
            console.print(str(error), style="red")
        raise typer.Exit(1) from error


from curator.cli.changes import register

register(app, console)

from curator.cli.review import register_review

register_review(app, console)

from curator.cli.doc import register_doc

register_doc(app, console)
