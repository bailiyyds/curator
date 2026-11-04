"""PDF text, tables and coordinates; no OCR or external commands."""

from io import BytesIO

import pdfplumber

from curator.models.document import Document, Page, Word


def extract_pdf(data: bytes, path: str) -> Document:
    """Extract every page and explicitly flag pages without a text layer."""
    pages: list[Page] = []
    with pdfplumber.open(BytesIO(data)) as pdf:
        for number, page in enumerate(pdf.pages, 1):
            text = page.extract_text() or ""
            words = [
                Word(text=w["text"], x0=w["x0"], top=w["top"], x1=w["x1"], bottom=w["bottom"])
                for w in page.extract_words()
            ]
            pages.append(
                Page(
                    number=number,
                    width=page.width,
                    height=page.height,
                    text=text,
                    words=words,
                    tables=page.extract_tables(),
                    needs_ocr=not text.strip(),
                )
            )
            page.close()
    return Document(
        path=path,
        pages=pages,
        body="\n\n".join(p.text for p in pages),
        needs_ocr=any(p.needs_ocr for p in pages),
    )
