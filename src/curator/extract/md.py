"""Markdown extraction through markdown-it tokens and safe YAML."""

from typing import Any

import yaml
from markdown_it import MarkdownIt

from curator.extract.text import normalize
from curator.models.document import CodeBlock, Document, Heading


def extract_markdown(text: str, path: str) -> Document:
    """Preserve heading and code locations including frontmatter offsets."""
    text = normalize(text)
    lines = text.splitlines(keepends=True)
    metadata: dict[str, Any] = {}
    offset = 0
    if lines and lines[0].strip() == "---":
        end = next((i for i in range(1, len(lines)) if lines[i].strip() in {"---", "..."}), None)
        if end is None:
            raise ValueError("Unterminated YAML frontmatter")
        raw = yaml.safe_load("".join(lines[1:end]))
        if raw is not None and not isinstance(raw, dict):
            raise ValueError("Frontmatter must be a mapping")
        if raw is not None:
            metadata = {str(key): value for key, value in raw.items()}
        offset = end + 1
    tokens = MarkdownIt("commonmark").parse("".join(lines[offset:]))
    doc = Document(path=path, frontmatter=metadata)
    body: list[str] = []
    for i, token in enumerate(tokens):
        line = (token.map[0] if token.map else 0) + offset + 1
        if token.type == "heading_open":
            doc.headings.append(
                Heading(level=int(token.tag[1:]), text=tokens[i + 1].content, line=line)
            )
        if token.type in {"fence", "code_block"}:
            doc.code_blocks.append(
                CodeBlock(language=token.info.strip() or None, body=token.content, line=line)
            )
            body.append(token.content.rstrip("\n"))
        if token.type == "inline":
            words: list[str] = []
            for child in token.children or []:
                if child.type in {"text", "code_inline", "image"}:
                    words.append(child.content)
                elif child.type in {"softbreak", "hardbreak"}:
                    words.append("\n")
                elif child.type == "link_open":
                    href = child.attrGet("href")
                    if isinstance(href, str) and href.startswith(
                        ("https://", "http://", "mailto:")
                    ):
                        doc.links.append(href)
            body.append("".join(words))
    title = metadata.get("title")
    doc.title = (
        title
        if isinstance(title, str)
        else next((h.text for h in doc.headings if h.level == 1), None)
    )
    doc.body = "\n".join(body)
    doc.links = list(dict.fromkeys(doc.links))
    return doc
