"""Text decoding without replacement characters or silent loss."""

import unicodedata

import chardet

from curator.models.document import Document


def decode(data: bytes) -> str:
    """Prefer UTF-8, then use a sufficiently confident chardet result."""
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        result = chardet.detect(data)
        encoding = result.get("encoding")
        if not encoding or result.get("confidence", 0.0) < 0.6:
            raise ValueError("Cannot reliably detect text encoding")
        return data.decode(encoding, errors="strict")


def normalize(text: str) -> str:
    """Normalize Unicode and newlines while retaining source line counts."""
    return unicodedata.normalize("NFC", text.replace("\r\n", "\n").replace("\r", "\n"))


def extract_text(text: str, path: str) -> Document:
    """Build a plain-text document from decoded input."""
    return Document(path=path, body=normalize(text))
