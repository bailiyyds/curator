"""Extraction behavior verified with local, synthetic inputs."""

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from curator.cli import app
from curator.extract import extract_directory, extract_file
from curator.extract.code import extract_code, parse_code
from curator.extract.detect import detect_kind
from curator.extract.md import extract_markdown
from curator.extract.text import decode, normalize
from curator.safety.bincheck import is_binary


def pdf_bytes(text: bool = True) -> bytes:
    """Construct two PDF pages with text/table vectors and a blank page."""
    stream = (
        (
            b"BT /F1 12 Tf 30 150 Td (Hello PDF) Tj ET\n"
            b"20 20 m 180 20 l 180 80 l 20 80 l h S\n"
            b"100 20 m 100 80 l S 20 50 m 180 50 l S\n"
            b"BT /F1 10 Tf 30 60 Td (A) Tj 80 0 Td (B) Tj ET\n"
            b"BT /F1 10 Tf 30 30 Td (1) Tj 80 0 Td (2) Tj ET"
        )
        if text
        else b""
    )
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R 6 0 R] /Count 2 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] /Contents 7 0 R >>",
        b"<< /Length 0 >>\nstream\n\nendstream",
    ]
    data = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for i, obj in enumerate(objects, 1):
        offsets.append(len(data))
        data.extend(f"{i} 0 obj\n".encode() + obj + b"\nendobj\n")
    xref = len(data)
    data.extend(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode())
    for offset in offsets[1:]:
        data.extend(f"{offset:010d} 00000 n \n".encode())
    data.extend(
        f"trailer << /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF".encode()
    )
    return bytes(data)


def test_markdown() -> None:
    source = "---\ntitle: 标题\n---\n# Heading\n\n[Link](https://example.com) and `inline`\n\n```python\nx = 1\n```\n"
    doc = extract_markdown(source, "a.md")
    assert doc.title == "标题"
    assert doc.headings[0].line == 4
    assert doc.code_blocks[0].line == 8
    assert doc.code_blocks[0].language == "python"
    assert doc.links == ["https://example.com"]
    assert "Link and inline" in doc.body
    assert extract_markdown("# Title", "a.md").title == "Title"
    assert extract_markdown("---\n---\nText", "a.md").frontmatter == {}
    assert extract_markdown("    code\n", "a.md").code_blocks
    assert "\n" in extract_markdown("a\nb", "a.md").body


@pytest.mark.parametrize(
    "source",
    [
        "---\nmissing",
        "---\n- list\n---",
        "---\na: [bad\n---",
        "---\na: !!python/object:bad {}\n---",
    ],
)
def test_bad_frontmatter(tmp_path: Path, source: str) -> None:
    (tmp_path / "bad.md").write_text(source, encoding="utf-8")
    assert extract_file(tmp_path, "bad.md").error


@pytest.mark.parametrize(
    ("lang", "source", "name"),
    [
        ("py", "def hello(x):\n    return x\n", "hello"),
        ("ts", "function hello(x: number) { return x; }", "hello"),
        ("tsx", "function hello() { return <div/>; }", "hello"),
        ("js", "function hello(x) { return x; }", "hello"),
        ("go", "package main\nfunc hello() {}", "hello"),
        ("rs", "fn hello() {}", "hello"),
        ("java", "class Hello { void hello() {} }", "Hello"),
    ],
)
def test_code_grammars(lang: str, source: str, name: str) -> None:
    doc = extract_code(source, "sample." + lang, lang)
    assert not doc.parse_errors
    assert any(s.name == name for s in doc.symbols)
    assert all(s.line >= 1 and s.col >= 1 for s in doc.symbols)
    assert doc.body == source


def test_code_never_executes(tmp_path: Path) -> None:
    source = "raise RuntimeError('not executed')\ndef broken(:\n"
    (tmp_path / "a.py").write_text(source)
    result = extract_file(tmp_path, "a.py")
    assert result.document and result.document.parse_errors
    assert result.warnings
    with pytest.raises(ValueError):
        parse_code("hello", "unsupported")


def test_encoding_and_binary(tmp_path: Path) -> None:
    assert decode(b"\xef\xbb\xbfhello") == "hello"
    assert "français" in decode(("français déjà été " * 20).encode("cp1252"))
    assert normalize("a\r\nb\r") == "a\nb\n"
    (tmp_path / "binary.md").write_bytes(b"MZ" + b"x" * 10)
    assert extract_file(tmp_path, "binary.md").error
    assert is_binary(b"abc\x00") and not is_binary(b"text")
    assert detect_kind(".txt", b"PK\x03\x04")[0] == "binary"
    assert detect_kind(".jpg", b"\xff\xd8\xff")[0] == "image"


def test_pdf(tmp_path: Path) -> None:
    (tmp_path / "sample.pdf").write_bytes(pdf_bytes())
    result = extract_file(tmp_path, "sample.pdf")
    assert result.error is None
    doc = result.document
    assert doc and doc.pages and len(doc.pages) == 2
    assert "Hello PDF" in doc.body
    assert doc.pages[0].words[0].x0 > 0
    assert doc.pages[0].tables[0] == [["A", "B"], ["1", "2"]]
    assert doc.pages[1].needs_ocr and doc.needs_ocr
    assert result.warnings
    (tmp_path / "broken.pdf").write_bytes(b"%PDF-broken")
    assert extract_file(tmp_path, "broken.pdf").error


def test_boundaries_and_report(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("hello")
    (tmp_path / "blank.pdf").write_bytes(pdf_bytes(False))
    (tmp_path / "b.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    for relative in ("../escape", str(tmp_path.resolve()), ".git/config", "missing", "."):
        assert extract_file(tmp_path, relative).error
    assert extract_file(tmp_path, "a.txt", max_size=1).error
    report = extract_directory(tmp_path)
    assert report.scanned_pdfs == 1
    assert len(report.results) == 3
    assert extract_file(tmp_path, "a.txt").document.body == "hello"
    cli = CliRunner().invoke(app, ["extract", str(tmp_path), "--json"])
    assert cli.exit_code == 4
    assert json.loads(cli.stdout)["scanned_pdfs"] == 1
    summary = CliRunner().invoke(app, ["extract", str(tmp_path)])
    assert summary.exit_code == 4 and "OCR" in summary.stdout
    assert CliRunner().invoke(app, ["extract", str(tmp_path / "absent"), "--json"]).exit_code == 1


def test_no_network_or_writes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import socket

    def deny(*args: object, **kwargs: object) -> None:
        raise AssertionError("Network access is forbidden during extraction")

    monkeypatch.setattr(socket, "create_connection", deny)
    (tmp_path / "a.md").write_text("# Hello", encoding="utf-8")
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    assert extract_directory(tmp_path).results[0].document
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == before


def test_encoding_failure_is_reported(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import chardet

    monkeypatch.setattr(chardet, "detect", lambda data: {"encoding": None, "confidence": 0})
    (tmp_path / "bad.txt").write_bytes(b"\xff\xfe\xfd")
    assert "encoding" in (extract_file(tmp_path, "bad.txt").error or "")


def test_symbol_signature_and_detection() -> None:
    assert (
        extract_code("class Demo:\n    def method(self):\n        return 1\n", "x.py", "py")
        .symbols[1]
        .name
        == "method"
    )
    for suffix, kind in (
        (".md", "markdown"),
        (".py", "code"),
        (".txt", "text"),
        (".pdf", "pdf"),
        (".jpg", "image"),
        (".zzz", "unknown"),
    ):
        assert detect_kind(suffix, b"hello")[0] == kind


def test_invalid_budget_and_case_protected(tmp_path: Path) -> None:
    assert extract_file(tmp_path, "anything", max_size=-1).error
    assert extract_file(tmp_path, ".GiT/config").error == "Protected path"


def test_link_rejection_branch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "linked.txt").write_text("data")
    original = Path.is_symlink
    monkeypatch.setattr(Path, "is_symlink", lambda p: p.name == "linked.txt" or original(p))
    assert "Links" in (extract_file(tmp_path, "linked.txt").error or "")
