"""CLI presentation layer."""
from pathlib import Path
import typer
from rich.console import Console
from curator.core.config import load_config
from curator.core.scan import scan as scan_directory

app = typer.Typer(no_args_is_help=True)
console = Console()

@app.command()
def scan(path: Path, json: bool = False) -> None:
    """Read a directory and display its inventory without writing files."""
    try:
        inventory = scan_directory(path, load_config(path.resolve()))
        if json:
            console.print_json(inventory.model_dump_json())
        else:
            console.print(f"Files: {len(inventory.files)}; skipped: {len(inventory.skipped)}")
            for item in inventory.skipped:
                console.print(f"{item.path}: {item.reason}")
    except (OSError, ValueError) as error:
        console.print(str(error), style="red")
        raise typer.Exit(1) from error
