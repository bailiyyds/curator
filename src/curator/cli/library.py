"""Read-only library utilities and explicit configuration/index export."""

import hashlib
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from curator.analyze.duplicates import duplicates
from curator.core.apply import write_artifact
from curator.core.config import load_config
from curator.core.search import search
from curator.core.search_index import Snapshot, save_index
from curator.safety.paths import SafetyError


def register_library(app: typer.Typer, console: Console) -> None:
    """Register search, duplicate suggestions and safe project configuration."""

    @app.command("search")
    def search_command(
        query: str,
        path: Path,
        semantic: bool = False,
        model_path: Path | None = None,
        build_index: bool = False,
        top: Annotated[int, typer.Option(min=1)] = 10,
        json: bool = False,
        output: Annotated[str | None, typer.Option("--output", "-o")] = None,
        apply: bool = typer.Option(False, "--apply/--dry-run"),
        yes: bool = False,
    ) -> None:
        """Search extracted document text without changing source files."""
        try:
            report = search(
                query,
                path,
                top=top,
                semantic=semantic,
                model_path=model_path,
                config=load_config(path.resolve()),
            )
            if apply:
                if not output and not build_index:
                    raise ValueError("Choose --build-index or an explicit -o report path")
                if not yes and typer.prompt("Type yes to export index", default="no") != "yes":
                    raise typer.Exit(0)
                if build_index:
                    save_index(
                        Snapshot(
                            root=str(path.resolve()),
                            fingerprint=report.fingerprint,
                            texts=report.texts,
                            postings=report.postings,
                            skipped=report.skipped,
                            warnings=report.warnings,
                        )
                    )
                if build_index and report.vectors:
                    vector_key = hashlib.sha256(
                        (report.fingerprint + report.vectors.model_fingerprint).encode()
                    ).hexdigest()
                    save_index(
                        Snapshot(
                            root=str(path.resolve()),
                            fingerprint=vector_key,
                            texts=report.texts,
                            postings=report.postings,
                            vectors=report.vectors,
                            skipped=report.skipped,
                        )
                    )
                if output:
                    outcome = write_artifact(
                        path, output, report.model_dump_json(indent=2).encode(), apply=True
                    )
                    if any(entry.result == "failed" for entry in outcome.entries):
                        raise ValueError("Report export failed; inspect the journal")
            if json:
                console.print_json(report.model_dump_json())
            else:
                for hit in report.hits:
                    console.print(
                        f"{hit.path}:{hit.line} ({hit.line_basis}): {hit.snippet}", markup=False
                    )
                for warning in report.warnings:
                    console.print(warning, style="yellow")
                for item in report.skipped:
                    console.print(f"Skipped {item.path}: {item.reason}", markup=False)
        except (OSError, ValueError) as error:
            console.print_json(data={"error": str(error)}) if json else console.print(
                str(error), style="red"
            )
            raise typer.Exit(3 if isinstance(error, SafetyError) else 1) from error

    @app.command("duplicates")
    def duplicates_command(
        path: Path, distance: Annotated[int, typer.Option(min=0, max=10)] = 3, json: bool = False
    ) -> None:
        """Suggest exact and near duplicates without modifying any file."""
        try:
            report = duplicates(path, distance=distance, config=load_config(path.resolve()))
            if json:
                console.print_json(report.model_dump_json())
            else:
                console.print(report.model_dump(), markup=False)
        except (OSError, ValueError) as error:
            console.print_json(data={"error": str(error)}) if json else console.print(
                str(error), style="red"
            )
            raise typer.Exit(1) from error

    config_app = typer.Typer(no_args_is_help=True)
    app.add_typer(config_app, name="config")

    @config_app.command("init")
    def config_init(
        path: Annotated[Path, typer.Argument()] = Path("."),
        apply: bool = typer.Option(False, "--apply/--dry-run"),
        yes: bool = False,
        json: bool = False,
    ) -> None:
        """Preview or create project configuration; never replace existing configuration."""
        content = (
            "max_size = 52428800\nmax_files = 100000\nmax_depth = 100\nfollow_symlinks = false\n"
        )
        try:
            if apply:
                if (
                    not yes
                    and typer.prompt("Type yes to create project config", default="no") != "yes"
                ):
                    raise typer.Exit(0)
                report = write_artifact(path, ".curator.toml", content.encode(), apply=True)
                if any(entry.result == "failed" for entry in report.entries):
                    raise ValueError("Configuration write failed; inspect the journal")
                if json:
                    console.print_json(report.model_dump_json())
                else:
                    console.print(f"Created .curator.toml; journal: {report.journal}")
            elif json:
                console.print_json(
                    data={"path": ".curator.toml", "content": content, "dry_run": True}
                )
            else:
                console.print(content, markup=False)
        except (OSError, ValueError) as error:
            console.print_json(data={"error": str(error)}) if json else console.print(
                str(error), style="red"
            )
            raise typer.Exit(3 if isinstance(error, SafetyError) else 1) from error
