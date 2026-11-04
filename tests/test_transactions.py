"""Real file transaction tests, including abrupt process termination."""

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from curator.cli import app
from curator.core.apply import apply_plan, write_artifact
from curator.core.journal import read_journal, root_lock, sha256
from curator.core.plan import rename_plan, slugify
from curator.core.undo import latest_journals, undo_journal
from curator.models.changes import Plan
from curator.safety.paths import SafetyError, safe_path


def prepare(root: Path, count: int = 1) -> Plan:
    """Create source documents with reliable titles."""
    for i in range(count):
        (root / f"old-{i}.md").write_text(f"# Title {i}\nBody {i}", encoding="utf-8")
    return rename_plan(root)


def test_fifty_roundtrip(tmp_path: Path) -> None:
    plan = prepare(tmp_path, 50)
    before = {p.name: sha256(p) for p in tmp_path.glob("*.md")}
    assert len(plan.changes) == 50
    assert rename_plan(tmp_path).model_dump_json() == plan.model_dump_json()
    assert apply_plan(plan).dry_run
    assert not (tmp_path / ".curator").exists()
    report = apply_plan(plan, apply=True)
    assert all(e.result == "ok" for e in report.entries)
    assert report.journal
    journal = Path(report.journal)
    _, events, _ = read_journal(journal)
    assert len(events) == 100
    assert undo_journal(journal).dry_run
    assert not (tmp_path / "old-0.md").exists()
    undone = undo_journal(journal, apply=True)
    assert all(e.result == "ok" for e in undone.entries)
    assert {p.name: sha256(p) for p in tmp_path.glob("*.md")} == before
    assert all(e.result == "skipped" for e in undo_journal(journal, apply=True).entries)
    assert latest_journals(tmp_path) == []
    assert not list(tmp_path.glob("*.tmp"))


def test_collision_hash_and_highrisk(tmp_path: Path) -> None:
    plan = prepare(tmp_path, 3)
    (tmp_path / plan.changes[0].src).write_text("edited")
    (tmp_path / plan.changes[1].dst).write_text("unrelated")
    plan.changes[2].risk = "high"
    report = apply_plan(plan, apply=True)
    assert [e.result for e in report.entries] == ["skipped"] * 3
    assert "SHA-256" in (report.entries[0].error or "")
    assert (tmp_path / plan.changes[1].dst).read_text() == "unrelated"
    assert (tmp_path / plan.changes[2].src).exists()
    solo = plan.model_copy(update={"changes": [plan.changes[2]]})
    assert apply_plan(solo, apply=True, allow_high_risk=True).entries[0].result == "ok"


def test_undo_refuses_changed_content_and_collisions(tmp_path: Path) -> None:
    plan = prepare(tmp_path, 2)
    report = apply_plan(plan, apply=True)
    assert report.journal
    (tmp_path / plan.changes[0].dst).write_text("edited")
    (tmp_path / plan.changes[1].src).write_text("new file")
    result = undo_journal(Path(report.journal), apply=True)
    assert all(e.result == "failed" for e in result.entries)
    assert (tmp_path / plan.changes[0].dst).read_text() == "edited"
    assert (tmp_path / plan.changes[1].src).read_text() == "new file"


@pytest.mark.parametrize(
    "relative",
    [
        "../escape",
        "/absolute",
        "x/../a",
        ".git/config",
        ".GiT/config",
        "NUL.txt",
        "stream:secret",
        "a\\b",
        "x.",
        "",
        "a//../x",
    ],
)
def test_unsafe_paths(tmp_path: Path, relative: str) -> None:
    with pytest.raises(SafetyError):
        safe_path(tmp_path, relative)
    assert not (tmp_path / ".curator").exists()


def test_invalid_plan_no_writes(tmp_path: Path) -> None:
    plan = prepare(tmp_path)
    change = plan.changes[0]
    for updates in (
        {"dst": "../outside.md"},
        {"op": "move"},
        {"pre_sha256": "bad"},
        {"dst": change.src},
        {"dst": "other/target.md"},
    ):
        invalid = plan.model_copy(update={"changes": [change.model_copy(update=updates)]})
        with pytest.raises(SafetyError):
            apply_plan(invalid, apply=True)
    with pytest.raises(SafetyError):
        apply_plan(plan.model_copy(update={"changes": [change, change]}), apply=True)
    assert not (tmp_path / ".curator").exists()


def test_lock_and_internal_link(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    plan = prepare(tmp_path)
    with root_lock(tmp_path), pytest.raises(SafetyError, match="lock"):
        apply_plan(plan, apply=True)
    original = Path.is_symlink
    monkeypatch.setattr(Path, "is_symlink", lambda p: p.name == ".curator" or original(p))
    with pytest.raises(SafetyError, match="Links"):
        apply_plan(plan, apply=True)
    assert (tmp_path / plan.changes[0].src).exists()


def test_slug_and_naming_conflicts(tmp_path: Path) -> None:
    assert slugify(" 你好，World !! ") == "你好-World"
    assert len(slugify("a" * 100)) == 80
    assert slugify("...") == "untitled"
    assert slugify("NUL") == "file-NUL"
    (tmp_path / "a.MD").write_text("---\ntitle: SAME\n---\n# Other", encoding="utf-8")
    (tmp_path / "b.md").write_text("# SAME", encoding="utf-8")
    (tmp_path / "same.md").write_text("# SAME", encoding="utf-8")
    plan = rename_plan(tmp_path)
    assert len({c.dst.casefold() for c in plan.changes}) == len(plan.changes)
    assert all(c.dst.endswith(".md") for c in plan.changes)
    assert plan.changes[0].reason == "frontmatter.title"
    assert rename_plan(tmp_path, source="llm").warnings
    assert rename_plan(tmp_path, template="{date}-{topic}-{slug}").changes
    for template in ("{bad}", "{slug.__class__}", "{slug:80}", "{slug!r}"):
        with pytest.raises(ValueError):
            rename_plan(tmp_path, template=template)
    with pytest.raises(ValueError):
        rename_plan(tmp_path, source="invalid")
    with pytest.raises(ValueError):
        rename_plan(tmp_path, threshold=2)


def test_export_and_archive_on_undo(tmp_path: Path) -> None:
    payload = b'{"plan": "preview"}'
    assert write_artifact(tmp_path, "plan.json", payload).dry_run
    assert not (tmp_path / ".curator").exists()
    report = write_artifact(tmp_path, "plan.json", payload, apply=True)
    assert report.journal and (tmp_path / "plan.json").read_bytes() == payload
    with pytest.raises(SafetyError):
        write_artifact(tmp_path, "plan.json", b"overwrite", apply=True)
    undone = undo_journal(Path(report.journal), apply=True)
    assert undone.entries[0].result == "ok"
    assert not (tmp_path / "plan.json").exists()
    assert next(iter((tmp_path / ".curator/trash").rglob("plan.json"))).read_bytes() == payload


def test_torn_record_and_bad_journal(tmp_path: Path) -> None:
    plan = prepare(tmp_path)
    report = apply_plan(plan, apply=True)
    assert report.journal
    journal = Path(report.journal)
    with journal.open("ab") as stream:
        stream.write(b'{"incomplete":')
    assert read_journal(journal)[2]
    assert undo_journal(journal, apply=True).entries[0].result == "ok"
    undo_path = next(
        p
        for p in (tmp_path / ".curator/journal").glob("*.jsonl")
        if read_journal(p)[0].get("undo_of")
    )
    with pytest.raises(SafetyError):
        undo_journal(undo_path, apply=True)
    bad = tmp_path / "bad.jsonl"
    bad.write_text("")
    with pytest.raises(SafetyError):
        read_journal(bad)
    bad.write_text("{}\n")
    with pytest.raises(SafetyError):
        read_journal(bad)


def test_cli_plan_apply_undo(tmp_path: Path) -> None:
    prepare(tmp_path)
    runner = CliRunner()
    preview = runner.invoke(app, ["rename", str(tmp_path), "--json", "-o", "plan.json"])
    assert preview.exit_code == 0 and json.loads(preview.stdout)["dry_run"]
    assert not (tmp_path / "plan.json").exists()
    assert runner.invoke(app, ["rename", str(tmp_path)]).exit_code == 0
    denied = runner.invoke(app, ["rename", str(tmp_path), "--apply"], input="no\n")
    assert denied.exit_code == 0 and (tmp_path / "old-0.md").exists()
    export = runner.invoke(app, ["plan", str(tmp_path), "--apply", "--yes", "--json"])
    assert export.exit_code == 0, export.output
    planfile = tmp_path / "plan.json"
    inspected = runner.invoke(app, ["apply", str(planfile), "--json"])
    assert inspected.exit_code == 0 and (tmp_path / "old-0.md").exists()
    applied = runner.invoke(app, ["apply", str(planfile), "--apply", "--yes", "--json"])
    assert applied.exit_code == 0, applied.output
    journal = json.loads(applied.stdout)["journal"]
    undone = runner.invoke(app, ["undo", "--journal", journal, "--apply", "--yes", "--json"])
    assert undone.exit_code == 0, undone.output
    assert (tmp_path / "old-0.md").exists()
    assert runner.invoke(app, ["undo", str(tmp_path), "--last", "1"]).exit_code == 0
    assert runner.invoke(app, ["plan", str(tmp_path), "--json"]).exit_code == 0
    assert runner.invoke(app, ["plan", str(tmp_path)]).exit_code == 0


def test_abrupt_kill_after_rename_before_commit(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    plan = prepare(root, 5)
    before = {p.name: sha256(p) for p in root.glob("*.md")}
    ready = tmp_path / "ready"
    code = r"""
import sys, time
from pathlib import Path
from curator.core.plan import rename_plan
import curator.core.apply as module
original = module.move_no_replace
count = 0
def interrupted(src, dst):
    global count
    original(src, dst)
    count += 1
    if count == 3:
        Path(sys.argv[2]).write_text('renamed')
        time.sleep(60)
module.move_no_replace = interrupted
module.apply_plan(rename_plan(Path(sys.argv[1])), apply=True)
"""
    process = subprocess.Popen([sys.executable, "-c", code, str(root), str(ready)])
    try:
        deadline = time.monotonic() + 15
        while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        assert ready.exists(), "Worker did not reach the crash window"
    finally:
        process.kill()
        process.wait(timeout=10)
    assert not (root / plan.changes[0].src).exists()
    journals = latest_journals(root)
    assert len(journals) == 1
    _, events, _ = read_journal(journals[0])
    assert len(events) == 5 and events[-1].phase == "prepare"
    restored = undo_journal(journals[0], apply=True)
    assert len(restored.entries) == 3
    assert all(e.result == "ok" for e in restored.entries)
    assert {p.name: sha256(p) for p in root.glob("*.md")} == before


def test_scan_export_and_protected_root(tmp_path: Path) -> None:
    prepare(tmp_path)
    runner = CliRunner()
    result = runner.invoke(app, ["scan", str(tmp_path), "-o", "inventory.json", "--json"])
    assert result.exit_code == 0 and not (tmp_path / "inventory.json").exists()
    result = runner.invoke(
        app, ["scan", str(tmp_path), "-o", "inventory.json", "--apply", "--yes", "--json"]
    )
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    assert data["export"]["journal"] and (tmp_path / "inventory.json").is_file()
    assert runner.invoke(app, ["scan", str(tmp_path), "--apply", "--yes"]).exit_code == 1
    protected = tmp_path / ".git"
    protected.mkdir()
    with pytest.raises(SafetyError):
        safe_path(protected, "config")


def test_binary_filename_fallback_is_highrisk(tmp_path: Path) -> None:
    (tmp_path / "messy name.PNG").write_bytes(b"\x89PNG\r\n\x1a\n")
    plan = rename_plan(tmp_path)
    assert plan.changes[0].dst == "messy-name.png"
    assert plan.changes[0].risk == "high"
    assert apply_plan(plan, apply=True).entries[0].result == "skipped"


def test_failure_does_not_stop_other_operations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import curator.core.apply as module

    plan = prepare(tmp_path, 2)
    original = module.move_no_replace

    def fail_one(src: Path, dst: Path) -> None:
        if src.name == "old-0.md":
            raise OSError("simulated failure")
        original(src, dst)

    monkeypatch.setattr(module, "move_no_replace", fail_one)
    result = apply_plan(plan, apply=True)
    assert [e.result for e in result.entries] == ["failed", "ok"]
    assert result.journal
    restored = undo_journal(Path(result.journal), apply=True)
    assert [e.result for e in restored.entries] == ["ok", "skipped"]
    assert (tmp_path / "old-0.md").exists() and (tmp_path / "old-1.md").exists()


def test_cli_rename_direct_and_safety_errors(tmp_path: Path) -> None:
    prepare(tmp_path)
    runner = CliRunner()
    refused = runner.invoke(
        app, ["rename", str(tmp_path), "--apply", "--yes", "--json", "-o", "../escape"]
    )
    assert refused.exit_code == 3 and json.loads(refused.stdout)["error"]
    assert (tmp_path / "old-0.md").exists()
    renamed = runner.invoke(
        app, ["rename", str(tmp_path), "--apply", "--yes", "--json", "-o", "reviewed-plan.json"]
    )
    assert renamed.exit_code == 0, renamed.output
    assert (tmp_path / "reviewed-plan.json").is_file()
    assert (tmp_path / "Title-0.md").is_file()
    bad = runner.invoke(app, ["apply", str(tmp_path / "missing.json"), "--json"])
    assert bad.exit_code == 1 and json.loads(bad.stdout)["error"]


def test_normalized_paths_and_header(tmp_path: Path) -> None:
    for path in ("a//b", "a/", "./a"):
        with pytest.raises(SafetyError):
            safe_path(tmp_path, path)
    bad = tmp_path / "bad.jsonl"
    bad.write_text('{"type":"header","root":3}\n')
    with pytest.raises(SafetyError):
        read_journal(bad)
