"""Offline search, immutable snapshots, duplicate suggestions and config transactions."""

import json
import socket
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from curator.analyze.duplicates import duplicates, simhash
from curator.analyze.semantic import semantic_hits
from curator.cli import app
from curator.core.search import search, tokens
from curator.core.search_index import Snapshot, index_path, read_index, save_index
from curator.core.undo import latest_journals
from curator.safety.paths import SafetyError


def snapshot(root: Path, report):
    return Snapshot(
        root=str(root.resolve()),
        fingerprint=report.fingerprint,
        texts=report.texts,
        postings=report.postings,
        skipped=report.skipped,
        warnings=report.warnings,
    )


def test_keyword_chinese_positions_and_no_writes(tmp_path):
    (tmp_path / "a.txt").write_text("first\nChinese 文件策展 alpha\n", encoding="utf-8")
    (tmp_path / "bad.pdf").write_bytes(b"%PDF-invalid")
    (tmp_path / "image.png").write_bytes(b"\x89PNGbinary")
    before = sorted(p.name for p in tmp_path.iterdir())
    report = search("文件 alpha", tmp_path)
    assert [(h.path, h.line) for h in report.hits] == [("a.txt", 2)]
    assert report.hits[0].line_basis == "extracted-text"
    assert "postings" not in report.model_dump()
    assert len(report.skipped) == 2
    assert sorted(p.name for p in tmp_path.iterdir()) == before
    assert not search("missing", tmp_path).hits
    assert not search("!!!", tmp_path).hits
    assert "策展" in tokens("策展")
    with pytest.raises(ValueError):
        search(" ", tmp_path)


def test_persistent_index_reuse_invalidation_and_corruption(tmp_path, monkeypatch):
    import curator.core.search as module

    (tmp_path / "a.txt").write_text("needle", encoding="utf-8")
    report = search("needle", tmp_path)
    target = save_index(snapshot(tmp_path, report))
    assert target.parent.name == "index"
    assert not latest_journals(tmp_path)
    original = module.extract_file
    monkeypatch.setattr(
        module, "extract_file", lambda *args: pytest.fail("cache should avoid extraction")
    )
    assert search("needle", tmp_path).hits == report.hits
    assert save_index(snapshot(tmp_path, report)) == target
    monkeypatch.setattr(module, "extract_file", original)
    (tmp_path / "a.txt").write_text("changed", encoding="utf-8")
    changed = search("needle", tmp_path)
    assert not changed.hits and changed.fingerprint != report.fingerprint
    new_target = save_index(snapshot(tmp_path, changed))
    new_target.write_text("{broken", encoding="utf-8")
    recovered = search("changed", tmp_path)
    assert recovered.hits and recovered.warnings
    with pytest.raises(ValueError):
        save_index(snapshot(tmp_path, changed))
    assert new_target.read_text(encoding="utf-8") == "{broken"


def test_invalid_index_postings_and_paths(tmp_path):
    fingerprint = "a" * 64
    target = index_path(tmp_path, fingerprint)
    target.parent.mkdir(parents=True)
    bad = Snapshot(
        root=str(tmp_path),
        fingerprint=fingerprint,
        texts={"file.txt": ["x"]},
        postings={"x": {"file.txt": [9]}},
    )
    target.write_text(bad.model_dump_json(), encoding="utf-8")
    with pytest.raises(ValueError):
        read_index(tmp_path, fingerprint)
    with pytest.raises(ValueError):
        index_path(tmp_path, "../escape")
    bad.texts = {"../escape.txt": ["x"]}
    target.write_text(bad.model_dump_json(), encoding="utf-8")
    with pytest.raises(SafetyError):
        read_index(tmp_path, fingerprint)


def test_duplicates_are_suggestions_only(tmp_path):
    for name in ("a.txt", "b.txt"):
        (tmp_path / name).write_text("alpha beta gamma", encoding="utf-8")
    (tmp_path / "c.md").write_text("gamma beta alpha", encoding="utf-8")
    (tmp_path / "empty.txt").write_bytes(b"")
    report = duplicates(tmp_path, distance=0)
    assert ["a.txt", "b.txt"] in report.exact
    assert ("a.txt", "c.md", 0) in report.near
    assert len(list(tmp_path.iterdir())) == 4
    assert simhash("a b") == simhash("b a")
    with pytest.raises(ValueError):
        duplicates(tmp_path, distance=11)


def test_cli_dry_run_and_explicit_writes(tmp_path):
    runner = CliRunner()
    (tmp_path / "a.txt").write_text("needle", encoding="utf-8")
    args = ["search", "needle", str(tmp_path), "--build-index", "--json"]
    assert runner.invoke(app, args).exit_code == 0
    assert not (tmp_path / ".curator").exists()
    assert runner.invoke(app, args + ["--apply", "--yes"]).exit_code == 0
    assert len(list((tmp_path / ".curator/index").glob("*.json"))) == 1
    assert runner.invoke(app, ["duplicates", str(tmp_path), "--json"]).exit_code == 0
    config = ["config", "init", str(tmp_path), "--json"]
    assert runner.invoke(app, config).exit_code == 0
    assert not (tmp_path / ".curator.toml").exists()
    assert runner.invoke(app, config + ["--apply", "--yes"]).exit_code == 0
    assert runner.invoke(app, config + ["--apply", "--yes"]).exit_code == 3
    assert len(latest_journals(tmp_path)) == 1
    assert (
        runner.invoke(app, ["search", "needle", str(tmp_path), "--apply", "--yes"]).exit_code == 1
    )


def test_optional_semantic_fallback_is_offline(tmp_path, monkeypatch):
    (tmp_path / "a.txt").write_text("needle", encoding="utf-8")

    def denied(*args, **kwargs):
        pytest.fail("Network must not be used")

    monkeypatch.setattr(socket, "create_connection", denied)
    report = search("needle", tmp_path, semantic=True)
    assert report.hits and "falling back" in report.warnings[0]
    with pytest.raises(ValueError):
        semantic_hits(tmp_path, tmp_path, "q", {}, 1)


def test_local_semantic_backend_contract(tmp_path, monkeypatch):
    root = tmp_path / "documents"
    root.mkdir()
    model = tmp_path / "trusted-model"
    model.mkdir()
    (model / "modules.json").write_text(
        json.dumps([{"type": "sentence_transformers.models.Transformer", "path": ""}]),
        encoding="utf-8",
    )
    (model / "model.safetensors").write_bytes(b"test-only")

    class FakeModel:
        def __init__(self, path, **kwargs):
            assert kwargs["local_files_only"] and not kwargs["trust_remote_code"]
            assert kwargs["model_kwargs"]["use_safetensors"]

        def encode(self, values, **kwargs):
            assert kwargs["normalize_embeddings"]
            assert len(values) == 3
            return SimpleNamespace(tolist=lambda: [[1.0, 0.0], [0.0, 1.0], [1.0, 0.0]])

    monkeypatch.setitem(
        sys.modules, "sentence_transformers", SimpleNamespace(SentenceTransformer=FakeModel)
    )
    hits = semantic_hits(root, model, "query", {"a.txt": ["one", "two"]}, 1)
    assert hits[0].line == 2 and hits[0].score == 1
    (model / "unsafe.py").write_text("raise RuntimeError", encoding="utf-8")
    with pytest.raises(ValueError, match="Executable"):
        semantic_hits(root, model, "query", {}, 1)


def test_all_primary_commands_with_network_blocked(tmp_path, monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("Unexpected network connection")

    monkeypatch.setattr(socket.socket, "connect", denied)
    (tmp_path / "source.py").write_text(
        'def add(a, b):\n    """Add values."""\n    return a + b\n', encoding="utf-8"
    )
    runner = CliRunner()
    commands = [
        ["scan", str(tmp_path), "--json"],
        ["organize", str(tmp_path), "--json"],
        ["rename", str(tmp_path), "--json"],
        ["review", str(tmp_path), "--no-llm", "--json"],
        ["doc", str(tmp_path), "--from", "ast", "--json"],
        ["search", "return", str(tmp_path), "--json"],
        ["duplicates", str(tmp_path), "--json"],
    ]
    for args in commands:
        result = runner.invoke(app, args)
        assert result.exit_code == 0, (args, result.stdout, result.exception)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["source.py"]


def test_vector_index_persistence_and_reuse(tmp_path, monkeypatch):
    root = tmp_path / "docs"
    root.mkdir()
    (root / "a.txt").write_text("one\ntwo", encoding="utf-8")
    model = tmp_path / "model"
    model.mkdir()
    (model / "modules.json").write_text(
        json.dumps([{"type": "sentence_transformers.models.Transformer", "path": ""}]),
        encoding="utf-8",
    )
    (model / "model.safetensors").write_bytes(b"test-only")
    sizes = []

    class FakeModel:
        def __init__(self, *args, **kwargs):
            assert kwargs["local_files_only"]

        def encode(self, values, **kwargs):
            sizes.append(len(values))
            return SimpleNamespace(tolist=lambda: [[1.0, 0.0] for _ in values])

    monkeypatch.setitem(
        sys.modules, "sentence_transformers", SimpleNamespace(SentenceTransformer=FakeModel)
    )
    result = CliRunner().invoke(
        app,
        [
            "search",
            "query",
            str(root),
            "--semantic",
            "--model-path",
            str(model),
            "--build-index",
            "--apply",
            "--yes",
            "--json",
        ],
    )
    assert result.exit_code == 0, (result.stdout, result.exception)
    assert len(list((root / ".curator/index").glob("*.json"))) == 2
    report = search("query", root, semantic=True, model_path=model)
    assert report.hits and not report.warnings
    assert sizes == [3, 1], "Persisted vectors should avoid encoding document chunks again"
    assert not latest_journals(root)


def test_search_and_similarity_budgets_are_reported(tmp_path, monkeypatch):
    import curator.analyze.duplicates as duplicate_module
    import curator.core.search as search_module

    (tmp_path / "a.txt").write_text("long alpha text", encoding="utf-8")
    (tmp_path / "b.txt").write_text("different beta text", encoding="utf-8")
    monkeypatch.setattr(search_module, "SEARCH_DOCUMENTS", 1)
    monkeypatch.setattr(search_module, "SEARCH_CHARACTERS", 5)
    report = search("long", tmp_path)
    assert report.hits and report.warnings
    assert any(item.path == "b.txt" and "budget" in item.reason for item in report.skipped)
    monkeypatch.setattr(duplicate_module, "NEAR_DUPLICATE_DOCUMENTS", 1)
    assert duplicates(tmp_path).warnings


def test_index_write_failure_is_journaled(tmp_path, monkeypatch):
    import curator.core.search_index as module
    from curator.core.journal import read_journal

    (tmp_path / "a.txt").write_text("alpha", encoding="utf-8")
    report = search("alpha", tmp_path)

    def fail(*args):
        raise OSError("simulated storage failure")

    monkeypatch.setattr(module, "move_no_replace", fail)
    with pytest.raises(OSError):
        save_index(snapshot(tmp_path, report))
    assert not index_path(tmp_path, report.fingerprint).exists()
    journal = next((tmp_path / ".curator/journal").glob("*.jsonl"))
    header, events, _ = read_journal(journal)
    assert header["kind"] == "index"
    assert events[0].phase == "prepare"
    assert events[-1].result == "failed" and events[-1].phase == "commit"
    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "alpha"
