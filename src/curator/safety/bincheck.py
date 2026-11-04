"""Conservative binary classification over an 8 KiB prefix."""

MAGIC = (
    b"%PDF-",
    b"\x89PNG\r\n\x1a\n",
    b"\xff\xd8\xff",
    b"GIF87a",
    b"GIF89a",
    b"PK\x03\x04",
    b"\x7fELF",
    b"MZ",
    b"\x1f\x8b",
    b"Rar!",
    b"7z\xbc\xaf\x27\x1c",
)


def is_binary(data: bytes) -> bool:
    """Recognize NUL bytes and known binary headers without decoding."""
    head = data[:8192]
    return b"\x00" in head or head.startswith(MAGIC)
