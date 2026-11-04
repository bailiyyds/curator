"""Source documentation, safe local history, non-overwrite and batch recovery."""

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from curator.analyze.docast import extract_module
from curator.analyze.docgen import generate_docs
from curator.analyze.git_history import parse_history, read_history
from curator.cli import app
from curator.core.artifacts import write_artifacts
from curator.core.undo import latest_journals, undo_journal
from curator.safety.paths import SafetyError

PYTHON_SOURCE = r'''"""模块说明\nSecond line."""
@decorate(flag=True)
class Worker:
    """Worker documentation."""
    @staticmethod
    def run(value: int = 2, *args, **kwargs) -> str:
        """Run work.

        Args:
            value: An input value.
        """
        return helper(value)
def helper(value):
    return str(value)
'''


def git(root: Path, *args: str) -> bytes:
    """Run trusted system Git only for constructing the local test repository."""
    executable = shutil.which("git")
    if executable is None:
        pytest.skip("Git is unavailable on this host")
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("GIT_")}
    env.update(GIT_TERMINAL_PROMPT="0", GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
    return subprocess.run(
        [
            executable,
            "-c",
            "user.name=Curator Test",
            "-c",
            "user.email=curator@example.invalid",
            "-c",
            "commit.gpgsign=false",
            "-c",
            "core.hooksPath=",
            "-C",
            str(root),
            *args,
        ],
        env=env,
        capture_output=True,
        check=True,
    ).stdout


def test_python_hierarchy_signature_docstrings_and_calls() -> None:
    module = extract_module(PYTHON_SOURCE, "pkg/worker.py", "py")
    assert module.module == "pkg.worker" and module.docstring == "模块说明\nSecond line."
    symbols = {s.qualified_name: s for s in module.symbols}
    method = symbols["Worker.run"]
    assert method.parent == "Worker" and method.docstring and "Args:" in method.docstring
    assert method.signature == "def run(value: int = 2, *args, **kwargs) -> str:"
    assert method.decorators == ["@staticmethod"]
    assert [(p.name, p.annotation, p.default) for p in method.parameters] == [
        ("value", "int", "2"),
        ("*args", None, None),
        ("**kwargs", None, None),
    ]
    assert method.calls == ["helper"] and symbols["helper"].calls == ["str"]
    assert symbols["Worker"].decorators == ["@decorate(flag=True)"]
    assert "decorate" in module.calls


@pytest.mark.parametrize(
    ("lang", "source", "name", "parameter", "annotation"),
    [
        (
            "ts",
            "/** Run work */\nfunction run(x: number=2): number { return helper(x); }",
            "run",
            "x",
            "number",
        ),
        ("js", "// Run work\nfunction run(x=2) { return helper(x); }", "run", "x", None),
        (
            "go",
            "package main\n// Run work\nfunc Run(a,b int) string { return helper(a) }",
            "Run",
            "a",
            "int",
        ),
        ("rs", "/// Run work\nfn run(x: i32) -> i32 { helper(x) }", "run", "x", "i32"),
        (
            "java",
            "class A { /** Run work */\nString run(int x) { return helper(x); } }",
            "A.run",
            "x",
            "int",
        ),
    ],
)
def test_other_language_declarations(
    lang: str, source: str, name: str, parameter: str, annotation: str | None
) -> None:
    module = extract_module(source, "sample." + lang, lang)
    symbol = next(s for s in module.symbols if s.qualified_name == name)
    assert symbol.docstring and "Run work" in symbol.docstring
    assert symbol.parameters[0].name == parameter and symbol.parameters[0].annotation == annotation
    assert symbol.calls == ["helper"]


def test_static_source_never_runs_and_docstrings_are_data(tmp_path: Path) -> None:
    source = 'raise RuntimeError("never run")\n"not a module docstring"\ndef f():\n    r"raw \\n text"\n    return 1\n'
    (tmp_path / "sample.py").write_text(source)
    plan = generate_docs(tmp_path, source="ast")
    assert plan.modules[0].docstring is None
    assert plan.modules[0].symbols[0].docstring == r"raw \n text"
    assert not (tmp_path / "docs").exists() and not (tmp_path / ".curator").exists()


def test_ast_preview_publish_repeat_and_undo(tmp_path: Path) -> None:
    (tmp_path / "sample.py").write_text(PYTHON_SOURCE, encoding="utf-8")
    before = (tmp_path / "sample.py").read_bytes()
    plan = generate_docs(tmp_path, source="ast")
    assert len(plan.artifacts) == 2
    page = next(a for a in plan.artifacts if "modules" in a.path)
    assert "## Contents" in page.content and "Parameter | Annotation | Default" in page.content
    assert "Worker.run" in page.content and "Direct textual calls" in page.content
    payloads = {a.path: a.content.encode() for a in plan.artifacts if a.write_required}
    assert write_artifacts(tmp_path, payloads).dry_run and not (tmp_path / "docs").exists()
    result = write_artifacts(tmp_path, payloads, apply=True)
    assert result.journal and all(e.result == "ok" for e in result.entries)
    repeat = generate_docs(tmp_path, source="ast")
    assert all(not a.write_required for a in repeat.artifacts)
    assert (tmp_path / "sample.py").read_bytes() == before
    undone = undo_journal(Path(result.journal), apply=True)
    assert all(e.result == "ok" for e in undone.entries)
    assert not (tmp_path / "docs").exists() and (tmp_path / "sample.py").read_bytes() == before


def test_existing_documents_generated_names_and_diffs(tmp_path: Path) -> None:
    (tmp_path / "sample.py").write_text("def greet(name):\n    return name\n")
    docs = tmp_path / "docs/modules"
    docs.mkdir(parents=True)
    original = docs / "sample.md"
    original.write_text("# Human authored documentation\nKeep this.\n")
    plan = generate_docs(tmp_path, source="ast")
    page = next(
        a for a in plan.artifacts if a.source_paths == ["sample.py"] and "modules" in a.path
    )
    assert (
        page.path == "docs/modules/sample.generated.md" and page.diff and "-Keep this." in page.diff
    )
    (docs / "sample.generated.md").write_text("Earlier generated version")
    next_plan = generate_docs(tmp_path, source="ast")
    assert any(a.path.endswith("sample.generated-2.md") for a in next_plan.artifacts)
    result = write_artifacts(
        tmp_path,
        {a.path: a.content.encode() for a in next_plan.artifacts if a.write_required},
        apply=True,
    )
    assert all(e.result == "ok" for e in result.entries)
    assert original.read_text() == "# Human authored documentation\nKeep this.\n"


def test_go_package_grouping_and_file_split(tmp_path: Path) -> None:
    for name, source in {
        "a.go": "package main\nfunc A() {}",
        "b.go": "package main\nfunc B() {}",
        "test.go": "package main_test\nfunc Test() {}",
    }.items():
        (tmp_path / name).write_text(source)
    modules = generate_docs(tmp_path, source="ast", split="module")
    files = generate_docs(tmp_path, source="ast", split="file")
    assert len(modules.artifacts) == 3 and len(files.artifacts) == 4
    page = next(a for a in modules.artifacts if len(a.source_paths) == 2)
    assert "A" in page.content and "B" in page.content


def test_local_git_activity_and_no_worktree_writes(tmp_path: Path) -> None:
    git(tmp_path, "init", "-b", "main")
    (tmp_path / "sample.py").write_text("def first():\n    return 1\n")
    git(tmp_path, "add", "sample.py")
    git(tmp_path, "commit", "-m", "Initial API")
    (tmp_path / "sample.py").write_text("def first():\n    return 2\n")
    (tmp_path / "README.md").write_text("Notes")
    git(tmp_path, "add", "sample.py", "README.md")
    git(tmp_path, "commit", "-m", "Change result")
    before = {
        p.relative_to(tmp_path).as_posix(): p.read_bytes()
        for p in tmp_path.rglob("*")
        if p.is_file()
    }
    history = read_history(tmp_path)
    assert history.available and history.commit_count == 2
    activity = next(f for f in history.files if f.path == "sample.py")
    assert activity.commits == 2 and activity.additions == 3 and activity.deletions == 1
    assert activity.summaries == ["Change result", "Initial API"]
    plan = generate_docs(tmp_path, source="both")
    assert plan.effective_from == "both"
    assert any("Change result" in a.content for a in plan.artifacts)
    pure_git = generate_docs(tmp_path, source="git")
    assert pure_git.effective_from == "git" and all(not m.symbols for m in pure_git.modules)
    assert {
        p.relative_to(tmp_path).as_posix(): p.read_bytes()
        for p in tmp_path.rglob("*")
        if p.is_file()
    } == before
    assert read_history(tmp_path, max_commits=1).warnings
    assert not read_history(tmp_path, max_bytes=20).available


def test_git_fallback_and_untrusted_executable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "sample.py").write_text("def f():\n    return 1\n")
    import curator.analyze.git_history as module

    monkeypatch.setattr(module.shutil, "which", lambda command: None)
    plan = generate_docs(tmp_path, source="git")
    assert plan.effective_from == "ast" and plan.warnings and plan.modules[0].symbols
    fake = tmp_path / "git.exe"
    fake.write_bytes(b"not executed")
    monkeypatch.setattr(module.shutil, "which", lambda command: str(fake))
    assert not read_history(tmp_path).available


def test_git_helpers_are_disabled(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    marker = tmp_path / "helper-ran"
    helper = tmp_path / "helper.py"
    helper.write_text(f"from pathlib import Path\nPath({str(marker)!r}).write_text('ran')\n")
    git(repo, "init", "-b", "main")
    git(repo, "config", "diff.evil.textconv", f'"{sys.executable}" "{helper}"')
    git(repo, "config", "diff.external", f'"{sys.executable}" "{helper}"')
    (repo / ".gitattributes").write_text("*.py diff=evil\n")
    (repo / "sample.py").write_text("value=1\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "One")
    (repo / "sample.py").write_text("value=2\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "Two")
    assert read_history(repo).available
    assert not marker.exists()


def test_history_parser_binary_and_bad_paths(tmp_path: Path) -> None:
    header = b"CURATOR_LOG_V1\x00" + b"a" * 40 + b"\x002024-01-01T00:00:00Z\x00Subject\x00"
    history = parse_history(
        header + b"\n-\t-\timage.png\x001\t0\t../escape.py\x00garbage\x00", tmp_path
    )
    assert history.files[0].binary_changes == 1 and history.warnings
    for kwargs in ({"max_commits": 0}, {"timeout": 0}, {"max_bytes": 0}):
        with pytest.raises(ValueError):
            read_history(tmp_path, **kwargs)


def test_batch_refuses_collisions_and_partial_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "existing.md").write_text("Preserve")
    with pytest.raises(SafetyError):
        write_artifacts(tmp_path, {"new.md": b"new", "existing.md": b"bad"}, apply=True)
    assert not (tmp_path / "new.md").exists() and not (tmp_path / ".curator").exists()
    with pytest.raises(SafetyError):
        write_artifacts(tmp_path, {"../escape.md": b"bad"}, apply=True)
    import curator.core.artifacts as module

    original = module.move_no_replace

    def fail_one(src: Path, dst: Path) -> None:
        if dst.name == "bad.md":
            raise OSError("injected output failure")
        original(src, dst)

    monkeypatch.setattr(module, "move_no_replace", fail_one)
    result = write_artifacts(tmp_path, {"docs/good.md": b"good", "docs/bad.md": b"bad"}, apply=True)
    assert any(e.result == "failed" for e in result.entries)
    assert result.journal
    undo_journal(Path(result.journal), apply=True)
    assert not (tmp_path / "docs").exists() and (tmp_path / "existing.md").read_text() == "Preserve"


def test_cli_preview_write_undo_and_no_overwrite(tmp_path: Path) -> None:
    (tmp_path / "sample.py").write_text(PYTHON_SOURCE, encoding="utf-8")
    runner = CliRunner()
    preview = runner.invoke(app, ["doc", str(tmp_path), "--from", "ast", "--json"])
    assert preview.exit_code == 0 and json.loads(preview.stdout)["dry_run"]
    assert not (tmp_path / "docs").exists()
    assert runner.invoke(app, ["doc", str(tmp_path), "--from", "ast"]).exit_code == 0
    declined = runner.invoke(app, ["doc", str(tmp_path), "--from", "ast", "--apply"], input="no\n")
    assert declined.exit_code == 0 and not (tmp_path / "docs").exists()
    result = runner.invoke(
        app, ["doc", str(tmp_path), "--from", "ast", "--apply", "--yes", "--json"]
    )
    assert result.exit_code == 0, result.output
    journal = json.loads(result.stdout)["result"]["journal"]
    restored = runner.invoke(app, ["undo", "--journal", journal, "--apply", "--yes", "--json"])
    assert restored.exit_code == 0 and not (tmp_path / "docs").exists()
    assert runner.invoke(app, ["doc", str(tmp_path), "--out", "../escape", "--json"]).exit_code == 3
    assert runner.invoke(app, ["doc", str(tmp_path), "--from", "bad"]).exit_code == 2


def test_force_kill_after_artifact_publish(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    ready = tmp_path / "ready"
    code = r"""
import sys,time
from pathlib import Path
import curator.core.artifacts as module
original=module.move_no_replace
def interrupted(src,dst):
    original(src,dst)
    Path(sys.argv[2]).write_text('published')
    time.sleep(60)
module.move_no_replace=interrupted
module.write_artifacts(Path(sys.argv[1]),{'docs/one.md':b'one','docs/two.md':b'two'},apply=True)
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
    assert (root / "docs/one.md").read_bytes() == b"one" and not (root / "docs/two.md").exists()
    result = undo_journal(latest_journals(root)[0], apply=True)
    assert all(e.result == "ok" for e in result.entries)
    assert not (root / "docs").exists()


def test_docstring_literals_annotations_and_receivers() -> None:
    python = extract_module(
        '"first" " second"\ndef f():\n    "line\\nnext"\n    return 1\n', "a.py", "py"
    )
    assert python.docstring == "first second"
    assert python.symbols[0].docstring == "line\nnext"
    assert extract_module('f"not a literal doc"\n', "a.py", "py").docstring is None
    java = extract_module("class A { @Deprecated int run(int x) { return x; } }", "A.java", "java")
    assert next(s for s in java.symbols if s.name == "run").decorators == ["@Deprecated"]
    go = extract_module(
        "package main\ntype Server struct {}\nfunc (s *Server) Run(x int) int { return helper(x) }",
        "server.go",
        "go",
    )
    assert any(s.qualified_name == "Server.Run" for s in go.symbols)
    rust = extract_module(
        "#[derive(Clone)]\nstruct Server {}\nimpl Server { fn run(&self,x:i32)->i32 { helper(x) } }",
        "a.rs",
        "rs",
    )
    assert next(s for s in rust.symbols if s.name == "Server").decorators == ["#[derive(Clone)]"]
    assert any(s.qualified_name == "Server.run" for s in rust.symbols)
    js = extract_module("const run = (x=2) => helper(x);", "a.js", "js")
    assert js.symbols[0].name == "run" and js.symbols[0].parameters[0].default == "2"


def test_safe_markdown_and_encoded_links(tmp_path: Path) -> None:
    from markdown_it import MarkdownIt

    (tmp_path / "space name.ts").write_text('class A { "<img src=evil>"() { return 1; } }')
    plan = generate_docs(tmp_path, source="ast")
    index = next(a for a in plan.artifacts if a.path == "docs/index.md")
    assert "space%20name.md" in index.content
    page = next(a for a in plan.artifacts if "/modules/" in a.path)
    tokens = MarkdownIt().parse(page.content)
    assert not any(t.type == "html_inline" and "<img" in t.content for t in tokens)
    assert "&lt;img" in page.content


def test_decode_failures_and_existing_directory_targets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import chardet

    (tmp_path / "bad.py").write_bytes(b"\xff\xfe\xfd")
    monkeypatch.setattr(chardet, "detect", lambda data: {"encoding": None, "confidence": 0})
    plan = generate_docs(tmp_path, source="ast")
    assert plan.errors and not plan.artifacts
    result = CliRunner().invoke(app, ["doc", str(tmp_path), "--from", "ast", "--json"])
    assert result.exit_code == 4 and json.loads(result.stdout)["plan"]["errors"]
    (tmp_path / "bad.py").write_text("def f():\n    return 1\n")
    (tmp_path / "docs/modules/bad.md").mkdir(parents=True)
    with pytest.raises(SafetyError):
        generate_docs(tmp_path, source="ast")


def test_git_subdirectory_and_noncode_only_history(tmp_path: Path) -> None:
    git(tmp_path, "init", "-b", "main")
    (tmp_path / "outer.py").write_text("x=1\n")
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "inner.py").write_text("x=2\n")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-m", "Scoped files")
    history = read_history(sub)
    assert history.available and [f.path for f in history.files] == ["inner.py"]
    docs_only = tmp_path / "docs-only"
    docs_only.mkdir()
    git(docs_only, "init", "-b", "main")
    (docs_only / "README.md").write_text("Only documentation")
    git(docs_only, "add", ".")
    git(docs_only, "commit", "-m", "Readme only")
    plan = generate_docs(docs_only, source="git")
    assert len(plan.artifacts) == 1 and plan.artifacts[0].content.count("## Git activity") == 1


def test_git_deadline_fallback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import io

    import curator.analyze.git_history as module

    class Process:
        stdout = io.BytesIO(b"")
        waits = 0
        killed = False

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def wait(self, timeout=None):
            self.waits += 1
            if self.waits == 1:
                raise subprocess.TimeoutExpired("git", timeout)
            return 0

        def kill(self):
            self.killed = True

    process = Process()
    monkeypatch.setattr(module.shutil, "which", lambda command: sys.executable)
    monkeypatch.setattr(module.subprocess, "Popen", lambda *args, **kwargs: process)
    result = read_history(tmp_path, timeout=0.01)
    assert not result.available and process.killed and "deadline" in result.warnings[0]
