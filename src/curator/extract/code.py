"""Official tree-sitter grammars; inspected code is never imported."""

from typing import Any

import tree_sitter_go
import tree_sitter_java
import tree_sitter_javascript
import tree_sitter_python
import tree_sitter_rust
import tree_sitter_typescript
from tree_sitter import Language, Node, Parser, Tree

from curator.models.document import Document, Symbol

LANGUAGES = {
    "py": "python",
    "ts": "typescript",
    "tsx": "tsx",
    "js": "javascript",
    "jsx": "javascript",
    "go": "go",
    "rs": "rust",
    "java": "java",
}


def parse_code(text: str, language: str) -> Tree:
    """Parse a supported language without evaluating its content."""
    name = LANGUAGES.get(language, language)
    capsule: Any
    if name == "python":
        capsule = tree_sitter_python.language()
    elif name in {"typescript", "tsx"}:
        capsule = (
            tree_sitter_typescript.language_tsx()
            if name == "tsx"
            else tree_sitter_typescript.language_typescript()
        )
    elif name == "javascript":
        capsule = tree_sitter_javascript.language()
    elif name == "go":
        capsule = tree_sitter_go.language()
    elif name == "rust":
        capsule = tree_sitter_rust.language()
    elif name == "java":
        capsule = tree_sitter_java.language()
    else:
        raise ValueError(f"Unsupported code language: {language}")
    return Parser(Language(capsule)).parse(text.encode("utf-8"))


def walk(node: Node) -> list[Node]:
    """Collect named nodes iteratively to avoid Python recursion limits."""
    nodes: list[Node] = []
    stack = [node]
    while stack:
        item = stack.pop()
        nodes.append(item)
        stack.extend(reversed(item.named_children))
    return nodes


def extract_code(text: str, path: str, language: str) -> Document:
    """Record AST declarations and syntax errors with byte-based columns."""
    tree = parse_code(text, language)
    data = text.encode("utf-8")
    kinds = {
        "function_definition",
        "class_definition",
        "function_declaration",
        "method_definition",
        "method_declaration",
        "class_declaration",
        "interface_declaration",
        "function_item",
        "struct_item",
        "enum_item",
        "trait_item",
        "type_declaration",
    }
    symbols: list[Symbol] = []
    for node in walk(tree.root_node):
        if node.type not in kinds:
            continue
        name = node.child_by_field_name("name")
        if name is None:
            continue
        body = node.child_by_field_name("body")
        end = body.start_byte if body else node.end_byte
        symbols.append(
            Symbol(
                kind=node.type,
                name=data[name.start_byte : name.end_byte].decode(),
                line=node.start_point.row + 1,
                col=node.start_point.column + 1,
                end_line=node.end_point.row + 1,
                signature=data[node.start_byte : end].decode().rstrip(),
            )
        )
    return Document(
        path=path,
        body=text,
        language=LANGUAGES.get(language, language),
        symbols=symbols,
        parse_errors=tree.root_node.has_error,
    )
