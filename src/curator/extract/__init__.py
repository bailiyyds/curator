"""Read-only extraction boundary with explicit per-file failures."""

from pathlib import Path

import yaml
from pdfminer.pdfdocument import PDFPasswordIncorrect
from pdfminer.pdfparser import PDFSyntaxError
from pdfminer.psparser import PSEOF
from pdfplumber.utils.exceptions import PdfminerException

from curator.core.config import Config
from curator.extract.code import extract_code
from curator.extract.detect import detect_kind
from curator.extract.md import extract_markdown
from curator.extract.pdf import extract_pdf
from curator.extract.text import decode, extract_text
from curator.models.document import ExtractionReport, ExtractionResult
from curator.safety.bincheck import is_binary
from curator.safety.paths import PROTECTED


def extract_file(root: Path, relative: str, max_size: int = 50 * 1024 * 1024) -> ExtractionResult:
    """Read a bounded regular file strictly within root without following links."""
    result = ExtractionResult(path=relative)
    try:
        if max_size < 1:
            raise ValueError("Size budget must be positive")
        root = root.resolve(strict=True)
        rel = Path(relative)
        if rel.is_absolute() or ".." in rel.parts or not rel.parts:
            raise ValueError("Expected a relative path within root")
        if any(part.casefold() in PROTECTED for part in rel.parts):
            raise ValueError("Protected path")
        candidate = root
        for part in rel.parts:
            candidate = candidate / part
            if candidate.is_symlink() or candidate.is_junction():
                raise ValueError("Links and junctions are not followed")
        path = candidate.resolve(strict=True)
        if not path.is_relative_to(root) or not path.is_file():
            raise ValueError("Not a regular file within root")
        if path.stat().st_size > max_size:
            raise ValueError("Size budget exceeded")
        with path.open("rb") as stream:
            data = stream.read(max_size + 1)
        if len(data) > max_size:
            raise ValueError("Size budget exceeded during read")
        kind, language = detect_kind(path.suffix, data[:8192])
        if kind == "pdf":
            result.document = extract_pdf(data, rel.as_posix())
        elif kind in {"image", "binary"} or is_binary(data):
            raise ValueError(f"Binary or image file skipped ({kind})")
        else:
            text = decode(data)
            if kind == "markdown":
                result.document = extract_markdown(text, rel.as_posix())
            elif language:
                result.document = extract_code(text, rel.as_posix(), language)
            else:
                result.document = extract_text(text, rel.as_posix())
        if result.document.needs_ocr:
            result.warnings.append("PDF contains pages without a text layer; OCR is required")
        if result.document.parse_errors:
            result.warnings.append("Code contains syntax errors; AST recovery may be incomplete")
    except (
        OSError,
        ValueError,
        UnicodeError,
        yaml.YAMLError,
        PDFSyntaxError,
        PSEOF,
        PDFPasswordIncorrect,
        PdfminerException,
    ) as error:
        result.error = str(error)
    return result


def extract_directory(root: Path, config: Config | None = None) -> ExtractionReport:
    """Scan then extract each eligible file, preserving every skip reason."""
    from curator.core.scan import scan

    config = config or Config()
    inventory = scan(root, config)
    results = [
        ExtractionResult(path=f.path, error=f.error)
        if f.error
        else extract_file(root, f.path, config.max_size)
        for f in inventory.files
    ]
    return ExtractionReport(
        results=results,
        skipped=inventory.skipped,
        scanned_pdfs=sum(bool(r.document and r.document.needs_ocr) for r in results),
    )
