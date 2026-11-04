"""CLI parameter parsing and presentation."""

from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console

from curator.core.config import load_config
from curator.core.scan import scan as scan_directory

app = typer.Typer(no_args_is_help=True)
console = Console()


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
) -> None:
    """Read a directory without writing any files."""
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
        if json:
            console.print_json(inventory.model_dump_json())
        else:
            console.print(f"Files: {len(inventory.files)}; skipped: {len(inventory.skipped)}")
            for item in inventory.skipped:
                console.print(f"{item.path}: {item.reason}")
            for entry in inventory.files:
                if entry.error:
                    console.print(f"{entry.path}: {entry.error}", style="red")
        if any(entry.error for entry in inventory.files):
            raise typer.Exit(4)
    except (OSError, ValueError) as error:
        if json:
            console.print_json(data={"error": str(error)})
        else:
            console.print(str(error), style="red")
        raise typer.Exit(1) from error
