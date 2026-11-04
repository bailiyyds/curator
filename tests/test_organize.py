"""Organization strategies and cross-directory recovery."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from curator.analyze.docs import terms, topic_labels
from curator.cli import app
from curator.core.apply import apply_plan
from curator.core.journal import sha256
from curator.core.organize import organize_plan
from curator.core.undo import latest_journals, undo_journal
from curator.safety.paths import SafetyError


def fixture(root: Path) -> dict[str, str]:
    samples = {
        "nested/a.md": "# Apples\nApples orchard fruit harvest",
        "b.py": "def hello():\n    return 1\n",
        "c.txt": "apple fruit orchard",
        "picture.png": "image",
        "archive.zip": "archive",
        "other.dat": "other",
    }
    for name, body in samples.items():
        file = root / name
        file.parent.mkdir(exist_ok=True)
        file.write_text(body)
        os.utime(file, (1704067200, 1704067200))
    return {name: sha256(root / name) for name in samples}


@pytest.mark.parametrize("by", ["type", "date", "topic"])
def test_organize_roundtrip(tmp_path: Path, by: str) -> None:
    before = fixture(tmp_path)
    plan = organize_plan(tmp_path, by=by)
    assert len([c for c in plan.changes if c.op == "move"]) == len(before)
    assert plan.model_dump_json() == organize_plan(tmp_path, by=by).model_dump_json()
    assert apply_plan(plan).dry_run and not (tmp_path / ".curator").exists()
    assert any(c.path == "nested" for c in plan.cleanup)
    result = apply_plan(plan, apply=True)
    assert all(e.result == "ok" for e in result.entries)
    assert not [c for c in organize_plan(tmp_path, by=by).changes if c.op == "move"]
    assert result.journal
    undone = undo_journal(Path(result.journal), apply=True)
    assert all(e.result == "ok" for e in undone.entries), undone.model_dump_json()
    assert {name: sha256(tmp_path / name) for name in before} == before
    assert not (tmp_path / "organized").exists()
    assert (tmp_path / "nested").exists()


def test_categories_dates_conflicts(tmp_path: Path) -> None:
    fixture(tmp_path)
    (tmp_path / "nested/c.txt").write_text("different")
    plan = organize_plan(tmp_path)
    moves = {c.src: c.dst for c in plan.changes if c.op == "move"}
    assert moves["archive.zip"] == "organized/archives/archive.zip"
    assert moves["picture.png"] == "organized/images/picture.png"
    assert moves["b.py"] == "organized/code/b.py"
    assert "/_conflicts/" in moves["nested/c.txt"]
    assert len(set(moves.values())) == len(moves)
    dated = organize_plan(tmp_path, by="date")
    assert any("/2024/01/" in c.dst for c in dated.changes if c.op == "move")
    (tmp_path / "organized").mkdir()
    (tmp_path / "organized/docs").mkdir()
    (tmp_path / "organized/docs/c.txt").write_text("existing")
    (tmp_path / "organized/_conflicts").mkdir()
    (tmp_path / "organized/_conflicts/c.txt").write_text("reserved")
    new = organize_plan(tmp_path)
    assert any("_conflicts/c-2.txt" in c.dst for c in new.changes)
    result = apply_plan(new, apply=True)
    assert (tmp_path / "organized/docs/c.txt").read_text() == "existing"
    assert (tmp_path / "organized/_conflicts/c.txt").read_text() == "reserved"
    assert all(e.result == "ok" for e in result.entries)


def test_topic_clustering_and_budget() -> None:
    docs = {
        "a": "apple orchard fruit harvest",
        "b": "apple orchard fruit harvest",
        "c": "python compiler software program",
        "d": "python compiler software program",
        "blank": "",
    }
    labels = topic_labels(docs)
    assert labels["a"] == labels["b"] and labels["c"] == labels["d"]
    assert labels["a"] != labels["c"] and labels["blank"] == "其他"
    assert "苹果" in terms("苹果收获")
    assert topic_labels({}) == {}
    large = {str(i): "unique" + chr(97 + i // 26) + chr(97 + i % 26) for i in range(80)}
    assert len(set(topic_labels(large).values())) <= 12
    with pytest.raises(ValueError):
        topic_labels(docs, 13)


def test_cleanup_and_nonempty_created_directory(tmp_path: Path) -> None:
    fixture(tmp_path)
    (tmp_path / "empty").mkdir()
    plan = organize_plan(tmp_path)
    assert "empty" in {c.path for c in plan.cleanup}
    assert all(c.requires_confirmation for c in plan.cleanup)
    result = apply_plan(plan, apply=True)
    assert (tmp_path / "empty").exists() and (tmp_path / "nested").exists()
    extra = tmp_path / "organized/docs/extra.txt"
    extra.write_text("Added after organization")
    assert result.journal
    undone = undo_journal(Path(result.journal), apply=True)
    assert any(e.result == "failed" for e in undone.entries)
    assert extra.read_text() == "Added after organization"
    assert (tmp_path / "nested/a.md").exists()


def test_unsafe_output_and_directory_conflict(tmp_path: Path) -> None:
    fixture(tmp_path)
    with pytest.raises(SafetyError):
        organize_plan(tmp_path, into="../escape")
    with pytest.raises(ValueError):
        organize_plan(tmp_path, by="invalid")
    (tmp_path / "organized").write_text("existing file")
    with pytest.raises(ValueError):
        organize_plan(tmp_path)
    assert not (tmp_path / ".curator").exists()


def test_cli_preview_apply_and_undo(tmp_path: Path) -> None:
    before = fixture(tmp_path)
    runner = CliRunner()
    preview = runner.invoke(app, ["organize", str(tmp_path), "--by", "type", "--json"])
    assert preview.exit_code == 0 and json.loads(preview.stdout)["dry_run"]
    assert not (tmp_path / "organized").exists()
    assert runner.invoke(app, ["organize", str(tmp_path)]).exit_code == 0
    declined = runner.invoke(app, ["organize", str(tmp_path), "--apply"], input="no\n")
    assert declined.exit_code == 0 and not (tmp_path / "organized").exists()
    applied = runner.invoke(app, ["organize", str(tmp_path), "--apply", "--yes", "--json"])
    assert applied.exit_code == 0, applied.output
    journal = json.loads(applied.stdout)["journal"]
    restored = runner.invoke(app, ["undo", "--journal", journal, "--apply", "--yes", "--json"])
    assert restored.exit_code == 0, restored.output
    assert {name: sha256(tmp_path / name) for name in before} == before


def test_force_kill_during_cross_directory_move(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    before = fixture(root)
    ready = tmp_path / "ready"
    code = r"""
import sys, time
from pathlib import Path
from curator.core.organize import organize_plan
import curator.core.apply as module
original=module.move_no_replace
count=0
def interrupted(src,dst):
    global count
    original(src,dst)
    count+=1
    if count==2:
        Path(sys.argv[2]).write_text('ready')
        time.sleep(60)
module.move_no_replace=interrupted
module.apply_plan(organize_plan(Path(sys.argv[1])),apply=True)
"""
    child = subprocess.Popen([sys.executable, "-c", code, str(root), str(ready)])
    try:
        deadline = time.monotonic() + 15
        while not ready.exists() and child.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        assert ready.exists()
    finally:
        child.kill()
        child.wait(timeout=10)
    result = undo_journal(latest_journals(root)[0], apply=True)
    assert all(e.result == "ok" for e in result.entries), result.model_dump_json()
    assert {name: sha256(root / name) for name in before} == before


def test_topic_budget_and_directory_identity(tmp_path: Path) -> None:
    from curator.core.config import Config

    fixture(tmp_path)
    plan = organize_plan(tmp_path, by="topic", config=Config(max_topic_documents=1))
    assert any("topic analysis budget" in warning for warning in plan.warnings)
    assert any("其他" in c.dst for c in plan.changes)
    directories_only = plan.model_copy(
        update={"changes": [c for c in plan.changes if c.op == "mkdir"]}
    )
    result = apply_plan(directories_only, apply=True)
    assert result.journal
    (tmp_path / "organized").rename(tmp_path / "kept-original-directories")
    (tmp_path / "organized").mkdir()
    undone = undo_journal(Path(result.journal), apply=True)
    assert any(e.result == "failed" and "identity" in (e.error or "") for e in undone.entries)
    assert (tmp_path / "organized").is_dir() and (tmp_path / "kept-original-directories").is_dir()


def test_export_organization_plan_without_moving(tmp_path: Path) -> None:
    fixture(tmp_path)
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "plan",
            str(tmp_path),
            "--operation",
            "organize",
            "--by",
            "type",
            "--apply",
            "--yes",
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    assert (tmp_path / "plan.json").exists() and (tmp_path / "nested/a.md").exists()
    assert not (tmp_path / "organized").exists()
    saved = json.loads((tmp_path / "plan.json").read_text(encoding="utf-8"))
    assert any(c["op"] == "move" for c in saved["changes"])
    assert runner.invoke(app, ["organize", str(tmp_path), "--by", "invalid"]).exit_code == 2
