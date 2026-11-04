"""Documentation extraction directly from official tree-sitter ASTs."""

import codecs
import inspect
import re
from pathlib import PurePosixPath

from tree_sitter import Node

from curator.extract.code import LANGUAGES, parse_code, walk
from curator.models.docgen import ModuleDoc, ParameterDoc, SymbolDoc

FUNCTIONS = {
    "function_definition",
    "function_declaration",
    "method_definition",
    "method_declaration",
    "function_item",
    "constructor_declaration",
    "function_expression",
    "arrow_function",
    "lambda",
}
CLASSES = {
    "class_definition",
    "class_declaration",
    "interface_declaration",
    "struct_item",
    "enum_item",
    "trait_item",
    "type_spec",
}
COMMENTS = {"comment", "line_comment", "block_comment"}


def text(node: Node | None, data: bytes) -> str:
    """Return the exact textual span of one AST node."""
    return data[node.start_byte : node.end_byte].decode() if node else ""


def literal_docstring(node: Node, data: bytes) -> str | None:
    """Decode a Python string literal without eval, exec, import or compilation."""
    if node.type in {"parenthesized_expression", "concatenated_string"}:
        parts = [literal_docstring(c, data) for c in node.named_children]
        return (
            "".join(p or "" for p in parts) if parts and all(p is not None for p in parts) else None
        )
    if node.type != "string":
        return None
    start = next((c for c in node.children if c.type == "string_start"), None)
    end = next((c for c in node.children if c.type == "string_end"), None)
    prefix = text(start, data).lower()
    if start is None or end is None or "f" in prefix or "b" in prefix:
        return None
    value = data[start.end_byte : end.start_byte].decode()
    if "r" not in prefix:
        pattern = r"\\(?:[\\'\"abfnrtv]|x[0-9a-fA-F]{2}|u[0-9a-fA-F]{4}|U[0-9a-fA-F]{8}|N\{[^}]+\}|[0-7]{1,3}|\n)"

        def decode_escape(match: re.Match[str]) -> str:
            try:
                return str(codecs.decode(match.group().encode("ascii"), "unicode_escape"))
            except (UnicodeError, ValueError):
                return match.group()

        value = re.sub(pattern, decode_escape, value)
    return value


def docstring(node: Node, data: bytes, python: bool) -> str | None:
    """Read literal Python docstrings or adjacent source comments."""
    body = node if node.type == "module" else node.child_by_field_name("body")
    if python and body:
        first = next((c for c in body.named_children if c.type != "comment"), None)
        if first and first.type == "expression_statement" and first.named_children:
            value = literal_docstring(first.named_children[0], data)
            return inspect.cleandoc(value) if value is not None else None
    anchor = (
        node.parent
        if node.parent and node.parent.type in {"decorated_definition", "export_statement"}
        else node
    )
    comments: list[str] = []
    previous = anchor.prev_named_sibling
    row = anchor.start_point.row
    while previous and previous.type in COMMENTS and row - previous.end_point.row <= 1:
        comments.insert(0, text(previous, data))
        row = previous.start_point.row
        previous = previous.prev_named_sibling
    if node.type in {"module", "program", "source_file"} and not python:
        comments = []
        for child in node.named_children:
            if child.type not in COMMENTS:
                break
            comments.append(text(child, data))
    if not comments:
        return None
    cleaned = []
    for line in "\n".join(comments).splitlines():
        value = re.sub(r"^\s*(?://[/!]?|/\*+|\*+/|\*)\s?", "", line)
        cleaned.append(re.sub(r"\*/\s*$", "", value).rstrip())
    return inspect.cleandoc("\n".join(cleaned)).strip() or None


def parameters(node: Node, data: bytes) -> list[ParameterDoc]:
    """Extract names, annotations and defaults across the supported grammars."""
    group = node.child_by_field_name("parameters")
    if group is None:
        parameter = node.child_by_field_name("parameter")
        return [ParameterDoc(name=text(parameter, data))] if parameter else []
    if group.type == "identifier":
        return [ParameterDoc(name=text(group, data))]
    result: list[ParameterDoc] = []
    for item in group.named_children:
        if item.type in {"comment", "keyword_separator", "positional_separator"}:
            continue
        names = item.children_by_field_name("name")
        if not names:
            name = item.child_by_field_name("pattern") or item.child_by_field_name("left")
            if name is None and item.type == "typed_parameter" and item.named_children:
                name = item.named_children[0]
            names = [name or item]
        annotation_node = item.child_by_field_name("type")
        annotation = text(annotation_node, data)
        if annotation_node and annotation_node.type == "type_annotation":
            annotation = annotation.removeprefix(":").strip()
        default = (
            text(item.child_by_field_name("value") or item.child_by_field_name("right"), data)
            or None
        )
        for name in names:
            result.append(
                ParameterDoc(name=text(name, data), annotation=annotation or None, default=default)
            )
    return result


def declaration_name(node: Node, data: bytes) -> str | None:
    """Identify declared or assigned callable names without evaluating expressions."""
    name = node.child_by_field_name("name")
    if name:
        return text(name, data)
    parent = node.parent
    if (
        node.type in FUNCTIONS
        and parent
        and parent.type in {"variable_declarator", "assignment", "pair"}
    ):
        name = (
            parent.child_by_field_name("name")
            or parent.child_by_field_name("left")
            or parent.child_by_field_name("key")
        )
        if name:
            return text(name, data)
    return None


def decorators(node: Node, data: bytes) -> list[str]:
    """Collect Python decorators, TS decorators, Java annotations and Rust attributes."""
    found = [text(c, data) for c in node.named_children if c.type == "decorator"]
    if node.parent and node.parent.type == "decorated_definition":
        found.extend(text(c, data) for c in node.parent.named_children if c.type == "decorator")
    modifiers = next((c for c in node.named_children if c.type == "modifiers"), None)
    if modifiers:
        found.extend(
            text(c, data) for c in walk(modifiers) if c.type in {"annotation", "marker_annotation"}
        )
    previous = node.prev_named_sibling
    while previous and previous.type in {"attribute_item", "comment", "line_comment"}:
        if previous.type == "attribute_item":
            found.insert(0, text(previous, data))
        previous = previous.prev_named_sibling
    return list(dict.fromkeys(found))


def extract_module(source: str, path: str, language: str) -> ModuleDoc:
    """Reconstruct declarations, ownership and direct textual call relationships."""
    source = source.replace("\r\n", "\n").replace("\r", "\n")
    data = source.encode()
    source_lines = source.split("\n")
    tree = parse_code(source, language)
    nodes = walk(tree.root_node)
    module_name = path.rsplit(".", 1)[0].replace("/", ".")
    module_name = module_name.removesuffix(".__init__")
    module = ModuleDoc(
        path=path,
        module=module_name,
        language=LANGUAGES.get(language, language),
        docstring=docstring(tree.root_node, data, language in {"py", "python"}),
        parse_errors=tree.root_node.has_error,
    )
    if module.language == "go":
        clause = next(
            (n for n in tree.root_node.named_children if n.type == "package_clause"), None
        )
        package = (
            next(
                (
                    n
                    for n in clause.named_children
                    if n.type in {"package_identifier", "identifier"}
                ),
                None,
            )
            if clause
            else None
        )
        module.package = text(package, data) or None
        if module.package:
            parent_path = PurePosixPath(path).parent.as_posix().replace("/", ".")
            module.module = (
                f"{parent_path}.{module.package}" if parent_path != "." else module.package
            )
    by_node: dict[int, SymbolDoc] = {}
    for node in nodes:
        if node.type not in FUNCTIONS | CLASSES:
            continue
        name = declaration_name(node, data)
        if name is None:
            continue
        ancestor = node.parent
        parent: str | None = None
        while ancestor:
            if ancestor.id in by_node:
                parent = by_node[ancestor.id].qualified_name
                break
            if ancestor.type == "impl_item":
                parent = text(ancestor.child_by_field_name("type"), data) or None
                break
            ancestor = ancestor.parent
        receiver = node.child_by_field_name("receiver")
        if receiver:
            types = [n for n in walk(receiver) if n.type == "type_identifier"]
            parent = text(types[-1], data) if types else parent
        body = node.child_by_field_name("body")
        if body is None and node.type == "type_spec":
            type_node = node.child_by_field_name("type")
            if type_node:
                body = next(
                    (
                        c
                        for c in type_node.named_children
                        if c.type in {"field_declaration_list", "interface_type"}
                    ),
                    None,
                )
        end = body.start_byte if body else node.end_byte
        line_start = source_lines[node.start_point.row].encode()[: node.start_point.column]
        symbol = SymbolDoc(
            name=name,
            qualified_name=f"{parent}.{name}" if parent else name,
            parent=parent,
            kind="class" if node.type in CLASSES else "function",
            path=path,
            line=node.start_point.row + 1,
            col=len(line_start.decode()) + 1,
            signature=data[node.start_byte : end].decode().rstrip(),
            docstring=docstring(node, data, language in {"py", "python"}),
            decorators=decorators(node, data),
            parameters=parameters(node, data) if node.type in FUNCTIONS else [],
        )
        by_node[node.id] = symbol
        module.symbols.append(symbol)
    for node in nodes:
        if node.type not in {
            "call",
            "call_expression",
            "method_invocation",
            "object_creation_expression",
        }:
            continue
        function = (
            node.child_by_field_name("function")
            or node.child_by_field_name("name")
            or node.child_by_field_name("type")
        )
        target = text(function, data)
        obj = node.child_by_field_name("object")
        if obj:
            target = text(obj, data) + "." + target
        if not target:
            continue
        ancestor = node.parent
        owner: SymbolDoc | None = None
        while ancestor:
            if ancestor.id in by_node:
                body = ancestor.child_by_field_name("body")
                if body is None or node.start_byte >= body.start_byte:
                    owner = by_node[ancestor.id]
                    break
            ancestor = ancestor.parent
        if owner:
            owner.calls.append(target)
        else:
            module.calls.append(target)
    module.calls = sorted(set(module.calls))
    for symbol in module.symbols:
        symbol.calls = sorted(set(symbol.calls))
    return module
