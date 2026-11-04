"""Controlled defects, clean counterexamples, lexical scope and CLI review."""

import json
import socket
from pathlib import Path

import pytest
from typer.testing import CliRunner

from curator.analyze.code import Analyzer, merge_findings, review_directory
from curator.cli import app
from curator.core.undo import latest_journals, undo_journal
from curator.models.findings import Finding

DEFECTS = {
    "mutable.py": ("def f(values=[]):\n    return values\n", {"mutable-default"}),
    "none.py": ("def f(value):\n    return value == None\n", {"none-comparison"}),
    "imports.py": ("import os\n", {"unused-import"}),
    "swallow.py": (
        "def f():\n    try:\n        work()\n    except:\n        pass\n",
        {"bare-except", "swallowed-exception"},
    ),
    "logging.py": (
        "def f():\n    try:\n        work()\n    except ValueError:\n        logger.error('failed')\n",
        {"log-only-except"},
    ),
    "loop.py": (
        "async def f(items):\n    for item in items:\n        await service(item)\n",
        {"loop-io"},
    ),
    "resource.py": (
        "def f(path):\n    handle = open(path)\n    return handle.read()\n",
        {"unclosed-resource"},
    ),
    "format.py": ('def f(value):\n    return f"{value}" % value\n', {"mixed-formatting"}),
    "divide.py": ("def f(x, y):\n    return x / y\n", {"possible-zero-division"}),
    "complexity.py": (
        "def f(a,b,c,d,e,f,g):\n" + "    value = 1\n" * 81 + "    return value\n",
        {"long-function", "too-many-parameters"},
    ),
}


def analyze(source: str) -> list[Finding]:
    item = Analyzer(source, "sample.py", "py")
    item.common()
    item.python()
    return merge_findings(item.findings)


def test_ten_known_defect_categories_without_extra_hits(tmp_path: Path) -> None:
    for name, (source, _) in DEFECTS.items():
        (tmp_path / name).write_text(source, encoding="utf-8")
    report = review_directory(tmp_path, severity="info+")
    assert report.files_reviewed == 10 and not report.errors
    for name, (source, expected) in DEFECTS.items():
        findings = [f for f in report.findings if f.path == name]
        assert {rule for f in findings for rule in f.merged_rules} == expected, (name, findings)
        for finding in findings:
            assert finding.evidence == source.splitlines()[finding.line - 1]
            assert 1 <= finding.col <= len(finding.evidence) + 1
            assert finding.source == "rule" and finding.suggestion
    assert (
        report.model_dump_json() == review_directory(tmp_path, severity="info+").model_dump_json()
    )


def test_clean_code_has_no_hits() -> None:
    source = r"""from __future__ import annotations
from pathlib import Path
import json as js
import io as _io
__all__ = ["Path"]
def read(path: Path, count=1):
    with path.open() as handle:
        payload = handle.read()
    try:
        return js.loads(payload) / count
    except ZeroDivisionError:
        return 0
def guarded(a,b):
    if b == 0:
        return None
    return a / b
def close_resource(path):
    handle = open(path)
    try:
        return handle.read()
    finally:
        handle.close()
def factory(path):
    return open(path)
def identity(values=None):
    return [] if values is None else values
"""
    assert analyze(source) == []


@pytest.mark.parametrize(
    "source,unused",
    [
        ("import os\ndef f():\n    return os.getcwd()\n", False),
        ("import os\ndef f(os):\n    return os\n", True),
        ("import os\nvalue = other.os\n", True),
        ("from pathlib import Path as P\ndef f():\n    return P('x')\n", False),
        ("from pathlib import Path\n__all__=['Path']\n", False),
        ("from package import Thing as Thing\n", False),
        ("from pathlib import Path\ndef f(Path=Path()):\n    return Path\n", False),
    ],
)
def test_import_scope(source: str, unused: bool) -> None:
    findings = analyze(source)
    assert any("unused-import" in f.merged_rules for f in findings) == unused


@pytest.mark.parametrize("condition", ["b", "b != 0", "b > 0", "0 != b"])
def test_visible_divisor_guards(condition: str) -> None:
    assert not analyze(f"def f(a,b):\n    if {condition}:\n        return a / b\n")


def test_division_guards_and_mutation() -> None:
    assert not analyze(
        "def f(a,b):\n    if not b:\n        raise ValueError('zero')\n    return a/b\n"
    )
    assert not analyze("def f(a,b):\n    return b and a/b\n")
    assert any(
        "possible-zero-division" in f.merged_rules
        for f in analyze("def f(a,b):\n    if b:\n        b = 0\n        return a/b\n")
    )
    assert analyze("x = 1 / 0\n")[0].severity == "error"
    assert not analyze("x = 1 / 2\n")
    assert not analyze("x = 1 == 0 is None\n")


def test_comments_counts_and_unicode_positions(tmp_path: Path) -> None:
    source = '# 中文 TODO TODO FIXME XXX\ntext = "TODO not a comment"\n'
    (tmp_path / "markers.py").write_text(source, encoding="utf-8")
    report = review_directory(tmp_path, severity="info+")
    assert report.marker_counts == {"FIXME": 1, "TODO": 2, "XXX": 1}
    assert report.rule_counts["todo"] == 1
    assert report.findings[0].col == source.splitlines()[0].index("TODO") + 1
    assert len(report.findings) == 1
    assert review_directory(tmp_path).findings == []


@pytest.mark.parametrize(
    ("extension", "source"),
    [
        (
            "ts",
            "async function f(xs: string[]) { for (const x of xs) { await fetch(x); } } // TODO",
        ),
        (
            "tsx",
            "async function f(xs: string[]) { for (const x of xs) { await fetch(x); } } // TODO",
        ),
        ("js", "async function f(xs) { for (const x of xs) { await fetch(x); } } // TODO"),
        ("go", "package main\nfunc f() { for { reader.Read() } } // TODO"),
        ("rs", "fn f() { for x in xs { reader.read(); } } // TODO"),
        ("java", "class A { void f() { while (true) { reader.read(); } } } // TODO"),
    ],
)
def test_other_languages_common_rules(tmp_path: Path, extension: str, source: str) -> None:
    (tmp_path / f"sample.{extension}").write_text(source, encoding="utf-8")
    report = review_directory(tmp_path, severity="info+")
    rules = {r for f in report.findings for r in f.merged_rules}
    assert {"todo", "loop-io"} <= rules, report.model_dump_json()


def test_filters_bad_syntax_and_no_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "bad.py").write_text("raise RuntimeError('must not run')\ndef broken(:\n")
    (tmp_path / "readme.txt").write_text("text")

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("Network forbidden")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    report = review_directory(tmp_path, severity="error+")
    assert report.findings[0].rule == "syntax-error"
    assert report.skipped[0].path == "readme.txt"
    assert review_directory(tmp_path, languages="ts").files_reviewed == 0
    for kwargs in ({"rules": "unknown"}, {"languages": "cobol"}, {"severity": "warning++++"}):
        with pytest.raises(ValueError):
            review_directory(tmp_path, **kwargs)
    assert review_directory(tmp_path, rules="general", severity="info+")


def test_resource_close_and_nested_loop_scope() -> None:
    assert not analyze("def f(path):\n    handle = open(path)\n    handle.close()\n")
    assert any(
        "unclosed-resource" in f.merged_rules
        for f in analyze("def f(path):\n    return open(path).read()\n")
    )
    assert not analyze(
        "def f(items):\n    for item in items:\n        async def later():\n            await service()\n"
    )
    assert any(
        "swallowed-exception" in f.merged_rules
        for f in analyze("try:\n    work()\nexcept Exception:\n    pass\n")
    )
    assert any(
        "loop-io" in f.merged_rules for f in analyze("for item in items:\n    requests.get(item)\n")
    )


def test_cli_formats_exports_and_errors(tmp_path: Path) -> None:
    (tmp_path / "bad.py").write_text(DEFECTS["mutable.py"][0])
    runner = CliRunner()
    before = (tmp_path / "bad.py").read_bytes()
    for format in ("text", "json", "md"):
        result = runner.invoke(app, ["review", str(tmp_path), "--format", format, "--no-llm"])
        assert result.exit_code == 0, result.output
        assert "mutable-default" in result.stdout
    assert not (tmp_path / ".curator").exists()
    preview = runner.invoke(app, ["review", str(tmp_path), "-o", "findings.json", "--json"])
    assert preview.exit_code == 0 and not (tmp_path / "findings.json").exists()
    exported = runner.invoke(
        app,
        ["review", str(tmp_path), "-o", "findings.json", "--format", "json", "--apply", "--yes"],
    )
    assert exported.exit_code == 0, exported.output
    assert json.loads((tmp_path / "findings.json").read_text())["findings"]
    assert (tmp_path / "bad.py").read_bytes() == before
    undo_journal(latest_journals(tmp_path)[0], apply=True)
    assert not (tmp_path / "findings.json").exists()
    llm = runner.invoke(app, ["review", str(tmp_path), "--llm", "--json"])
    assert json.loads(llm.stdout)["warnings"]
    assert runner.invoke(app, ["review", str(tmp_path), "--rules", "unknown"]).exit_code == 2
    assert runner.invoke(app, ["review", str(tmp_path), "--apply"]).exit_code == 2
    assert runner.invoke(app, ["review", str(tmp_path / "missing"), "--json"]).exit_code == 1


def test_markdown_fence_and_highest_severity() -> None:
    findings = analyze("def f(x=[]): return 1/0 # TODO ```\n")
    assert len(findings) == 1 and findings[0].severity == "error"
    assert {"mutable-default", "possible-zero-division", "todo"} <= set(findings[0].merged_rules)


def test_compound_guards_connections_and_numeric_literals() -> None:
    assert not analyze("def f(a,b,enabled):\n    if b and enabled:\n        return a/b\n")
    assert not analyze(
        "def f(a,b):\n    try:\n        return a / get_number()\n    except ZeroDivisionError:\n        return 0\n"
    )
    assert not analyze("x = 1 / (2)\n")
    assert not analyze("x = 1 / -2\n")
    assert "mixed-formatting" not in {
        r for f in analyze('x = len(f"hello") % divisor\n') for r in f.merged_rules
    }
    assert any(
        "unclosed-resource" in f.merged_rules for f in analyze("connection = database.connect()\n")
    )
    assert any("unused-import" in f.merged_rules for f in analyze("import option\nf(option=1)\n"))


def test_report_errors_and_safe_markdown(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import chardet

    from curator.cli.review import render_markdown, render_text

    (tmp_path / "bad.py").write_bytes(b"\xff\xfe\xfd")
    monkeypatch.setattr(chardet, "detect", lambda data: {"encoding": None, "confidence": 0})
    report = review_directory(tmp_path)
    assert report.errors and report.skipped
    result = CliRunner().invoke(app, ["review", str(tmp_path), "--json"])
    assert result.exit_code == 4 and json.loads(result.stdout)["errors"]
    (tmp_path / "bad.py").write_text("x = 1/0 # TODO ```\n")
    report = review_directory(tmp_path, severity="info+")
    markdown = render_markdown(report)
    assert "````text" in markdown and "bad.py:1:" in markdown
    assert report.findings[0].evidence in render_text(report)
    wrong = CliRunner().invoke(app, ["review", str(tmp_path), "--rules", "unknown", "--json"])
    assert wrong.exit_code == 2 and json.loads(wrong.stdout)["error"]


def test_review_export_confirmation_and_nonoverwrite(tmp_path: Path) -> None:
    (tmp_path / "bad.py").write_text(DEFECTS["mutable.py"][0])
    runner = CliRunner()
    declined = runner.invoke(
        app, ["review", str(tmp_path), "-o", "report.md", "--format", "md", "--apply"], input="no\n"
    )
    assert declined.exit_code == 0 and not (tmp_path / "report.md").exists()
    exported = runner.invoke(
        app, ["review", str(tmp_path), "-o", "report.md", "--format", "md", "--apply", "--yes"]
    )
    assert exported.exit_code == 0 and (tmp_path / "report.md").read_text().startswith(
        "# Code review"
    )
    original = (tmp_path / "report.md").read_bytes()
    refused = runner.invoke(
        app, ["review", str(tmp_path), "-o", "report.md", "--apply", "--yes", "--json"]
    )
    assert refused.exit_code == 3 and (tmp_path / "report.md").read_bytes() == original


@pytest.mark.parametrize(
    ("lang", "source"),
    [
        ("js", "function f(a,b,c,d,e,f,g) {}"),
        ("ts", "function f(a:number,b:number,c:number,d:number,e:number,f:number,g:number) {}"),
        ("go", "package main\nfunc f(a,b,c,d,e,f,g int) {}"),
        ("rs", "fn f(a:i32,b:i32,c:i32,d:i32,e:i32,f:i32,g:i32) {}"),
        ("java", "class A { void f(int a,int b,int c,int d,int e,int f,int g) {} }"),
    ],
)
def test_common_parameter_limits(lang: str, source: str) -> None:
    item = Analyzer(source, "sample." + lang, lang)
    item.common()
    assert any(f.rule == "too-many-parameters" for f in item.findings)


def test_string_percent_formatting_is_not_numeric_division() -> None:
    assert not analyze('result = ("%s") % value\n')
    assert not analyze('result = "{}".format(name) % value\n')
    assert not analyze('result = ("%s" " done") % value\n')


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
def test_physical_lines_and_unicode_separator(newline: str) -> None:
    source = "label = '\u2028'" + newline + "answer = value == None" + newline
    findings = analyze(source)
    assert len(findings) == 1
    assert findings[0].line == 2
    assert findings[0].evidence == "answer = value == None"
