from pathlib import Path

from curator.core.config import Config, load_config
from curator.core.scan import scan


def test_stable_read_only(tmp_path: Path) -> None:
    (tmp_path / "a.md").write_text("# Hello", encoding="utf-8")
    before = list(tmp_path.iterdir())
    first = scan(tmp_path)
    assert first.model_dump_json() == scan(tmp_path).model_dump_json()
    assert first.files[0].kind == "markdown"
    assert list(tmp_path.iterdir()) == before


def test_budgets(tmp_path: Path) -> None:
    (tmp_path / "big").write_bytes(b"123")
    assert scan(tmp_path, Config(max_size=1)).skipped
    (tmp_path / ".git").mkdir()
    assert len(scan(tmp_path, Config(max_size=1)).skipped) == 2


def test_config(tmp_path: Path) -> None:
    user = tmp_path / "user.toml"
    user.write_text("max_files = 2")
    (tmp_path / ".curator.toml").write_text("max_files = 3")
    assert load_config(tmp_path, {"max_files": 4}, user).max_files == 4


def test_filters_and_limits(tmp_path: Path) -> None:
    (tmp_path / "a.md").write_text("text")
    (tmp_path / "b.txt").write_text("text")
    (tmp_path / "nested").mkdir()
    assert len(scan(tmp_path, Config(include=["*.md"])).files) == 1
    assert len(scan(tmp_path, Config(exclude=["*.txt"], max_depth=0)).skipped) == 2
    assert len(scan(tmp_path, Config(max_files=1)).files) == 1


def test_errors_and_binary(tmp_path: Path) -> None:
    import pytest

    with pytest.raises((ValueError, FileNotFoundError)):
        scan(tmp_path / "missing")
    (tmp_path / "bytes.bin").write_bytes(b"a\x00b")
    assert scan(tmp_path).files[0].kind == "binary"


def test_cli(tmp_path: Path) -> None:
    import json

    from typer.testing import CliRunner

    from curator.cli import app, parse_size

    (tmp_path / "a.md").write_text("Hello")
    result = CliRunner().invoke(app, ["scan", str(tmp_path), "--json"])
    assert result.exit_code == 0, result.output
    assert len(json.loads(result.stdout)["files"]) == 1
    assert parse_size("1KB") == 1024
    assert parse_size("12") == 12


def test_symlink_outside_root(tmp_path: Path) -> None:
    import pytest

    outside = tmp_path.parent / (tmp_path.name + "-outside")
    outside.mkdir(exist_ok=True)
    try:
        (tmp_path / "escape").symlink_to(outside, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"Host cannot create symlinks: {error}")
    inventory = scan(tmp_path)
    assert len(inventory.skipped) == 1
    assert inventory.files == []


def test_thousand_files(tmp_path: Path) -> None:
    import json
    import os
    import subprocess
    import sys

    for index in range(1000):
        (tmp_path / f"{index}.txt").write_text(str(index))
    code = """
import json, sys, time, tracemalloc
from pathlib import Path
from curator.core.scan import scan
tracemalloc.start()
start = time.perf_counter()
result = scan(Path(sys.argv[1]))
elapsed = time.perf_counter() - start
_, peak = tracemalloc.get_traced_memory()
sys.stdout.write(json.dumps([len(result.files), elapsed, peak]))
"""
    env = dict(os.environ)
    env.pop("COVERAGE_PROCESS_START", None)
    result = subprocess.run(
        [sys.executable, "-c", code, str(tmp_path)],
        capture_output=True,
        text=True,
        check=True,
        env=env,
    )
    count, elapsed, peak = json.loads(result.stdout)
    assert count == 1000
    assert elapsed < 10
    assert peak < 300 * 1024 * 1024


def test_cli_errors_and_summary(tmp_path: Path) -> None:
    from typer.testing import CliRunner

    from curator.cli import app

    runner = CliRunner()
    assert runner.invoke(app, ["scan", str(tmp_path)]).exit_code == 0
    result = runner.invoke(app, ["scan", str(tmp_path / "absent"), "--json"])
    assert result.exit_code == 1
    assert "error" in result.stdout
    assert runner.invoke(app, ["scan", str(tmp_path), "--max-size", "bad"]).exit_code == 1
    assert runner.invoke(app, ["scan", str(tmp_path), "--follow-symlinks"]).exit_code == 1


def test_kinds(tmp_path: Path) -> None:
    for name in ("a.py", "b.jpg", "c.pdf", "d.txt", "e.other"):
        (tmp_path / name).write_text("sample")
    assert {f.kind for f in scan(tmp_path).files} == {"code", "image", "pdf", "text", "unknown"}
