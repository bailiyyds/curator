"""AST-based local rules. Potential-risk findings are explicitly heuristic."""

import hashlib
import re
from bisect import bisect_right
from collections import Counter
from pathlib import Path
from typing import Literal

from tree_sitter import Node

from curator.core.config import Config
from curator.core.scan import scan
from curator.extract import extract_file
from curator.extract.code import LANGUAGES, parse_code, walk
from curator.models import SkippedItem
from curator.models.findings import Finding, ReviewReport

RULES = {
    "bare-except",
    "swallowed-exception",
    "mutable-default",
    "none-comparison",
    "unused-import",
    "long-function",
    "too-many-parameters",
    "loop-io",
    "unclosed-resource",
    "mixed-formatting",
    "possible-zero-division",
    "log-only-except",
    "todo",
    "syntax-error",
}
COMMON = {"long-function", "too-many-parameters", "loop-io", "todo", "syntax-error"}
FUNCTIONS = {
    "function_definition",
    "function_declaration",
    "method_definition",
    "method_declaration",
    "function_item",
    "arrow_function",
    "function_expression",
    "lambda_expression",
    "lambda",
    "func_literal",
}
SCOPES = FUNCTIONS | {"class_definition", "module"}
LOOPS = {
    "for_statement",
    "while_statement",
    "for_in_statement",
    "for_expression",
    "while_expression",
    "do_statement",
    "enhanced_for_statement",
}
IO_METHODS = {
    "read",
    "write",
    "read_text",
    "write_text",
    "read_bytes",
    "write_bytes",
    "execute",
    "executemany",
    "fetchone",
    "fetchall",
    "send",
    "recv",
    "readfile",
    "writefile",
    "readline",
    "executequery",
    "executeupdate",
    "getinputstream",
    "getoutputstream",
}
SEVERITY = {"error": 0, "warning": 1, "info": 2}
Severity = Literal["error", "warning", "info"]


def node_text(node: Node | None, data: bytes) -> str:
    """Decode an AST node's exact UTF-8 source span."""
    return data[node.start_byte : node.end_byte].decode("utf-8") if node else ""


def scope(node: Node) -> Node:
    """Find the nearest enclosing lexical scope, excluding the node itself."""
    current = node.parent
    while current and current.type not in SCOPES:
        current = current.parent
    return current or node


def binding_names(node: Node | None, data: bytes) -> set[str]:
    """Extract assignment bindings without treating attribute/subscript operands as bindings."""
    if node is None:
        return set()
    if node.type == "identifier":
        return {node_text(node, data)}
    if node.type in {
        "tuple_pattern",
        "list_pattern",
        "pattern_list",
        "tuple",
        "list",
        "list_splat_pattern",
        "dictionary_splat_pattern",
    }:
        return {name for child in node.named_children for name in binding_names(child, data)}
    return set()


class Analyzer:
    """Inspect a single syntax tree without importing or executing source."""

    def __init__(self, text: str, path: str, language: str) -> None:
        """Parse once and build lexical information for all static rules."""
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        self.text, self.path, self.language = text, path, LANGUAGES.get(language, language)
        self.data = text.encode("utf-8")
        self.tree = parse_code(text, language)
        self.nodes = walk(self.tree.root_node)
        self.lines = text.split("\n")
        self.starts = [0] + [i + 1 for i, value in enumerate(self.data) if value == 10]
        self.findings: list[Finding] = []
        self.markers: Counter[str] = Counter()

    def add(
        self,
        node: Node,
        rule: str,
        message: str,
        suggestion: str,
        severity: Severity = "warning",
        offset: int | None = None,
    ) -> None:
        """Record a one-based character column and the exact original source line."""
        position = node.start_byte if offset is None else offset
        line = bisect_right(self.starts, position)
        col = len(self.data[self.starts[line - 1] : position].decode("utf-8")) + 1
        evidence = self.lines[line - 1] if line <= len(self.lines) else ""
        self.findings.append(
            Finding(
                id="pending",
                path=self.path,
                line=line,
                col=col,
                rule=rule,
                severity=severity,
                message=message,
                evidence=evidence,
                suggestion=suggestion,
            )
        )

    def call_name(self, node: Node) -> str:
        """Extract a call target from language-specific AST fields."""
        function = node.child_by_field_name("function") or node.child_by_field_name("name")
        return node_text(function, self.data)

    def inside_loop(self, node: Node) -> bool:
        """Exclude calls in nested functions from an enclosing function's loop."""
        parent = node.parent
        while parent:
            if parent.type in FUNCTIONS:
                return False
            if parent.type in LOOPS:
                return True
            parent = parent.parent
        return False

    def io_call(self, node: Node) -> bool:
        """Match explicit IO APIs and selected potentially synchronous IO methods."""
        name = self.call_name(node)
        tail = name.replace("::", ".").rsplit(".", 1)[-1].lower()
        return (
            name in {"open", "io.open", "input", "sqlite3.connect", "socket.socket"}
            or tail in IO_METHODS
            or (
                name.startswith(("requests.", "httpx.", "urllib.request."))
                and tail in {"get", "post", "put", "delete", "urlopen", "request"}
            )
        )

    def common(self) -> None:
        """Run marker, complexity, loop-IO and syntax recovery rules for all languages."""
        for node in self.nodes:
            if node.type in {"comment", "line_comment", "block_comment"}:
                value = node_text(node, self.data)
                for match in re.finditer(r"\b(TODO|FIXME|XXX)\b", value, re.IGNORECASE):
                    marker = match.group().upper()
                    self.markers[marker] += 1
                    self.add(
                        node,
                        "todo",
                        f"Unresolved {marker} marker",
                        "Resolve or explicitly track this work.",
                        "info",
                        node.start_byte + len(value[: match.start()].encode("utf-8")),
                    )
            if node.type in FUNCTIONS:
                if node.end_point.row - node.start_point.row + 1 > 80:
                    self.add(
                        node,
                        "long-function",
                        "Function spans more than 80 lines.",
                        "Split cohesive responsibilities into smaller functions.",
                    )
                parameters = node.child_by_field_name("parameters")
                if parameters:
                    count = 0
                    for param in parameters.named_children:
                        if param.type in {"keyword_separator", "positional_separator", "comment"}:
                            continue
                        names = param.children_by_field_name("name")
                        count += max(1, len(names))
                    if parameters.type == "identifier":
                        count = 1
                    if count > 6:
                        self.add(
                            node,
                            "too-many-parameters",
                            f"Function declares {count} parameters (>6).",
                            "Group related inputs or simplify the interface.",
                        )
            if node.type in {"await", "await_expression"} and self.inside_loop(node):
                self.add(
                    node,
                    "loop-io",
                    "Await inside a loop may serialize repeated IO (potential N+1).",
                    "Consider batching or bounded concurrency where ordering permits.",
                )
            if (
                node.type in {"call", "call_expression", "method_invocation"}
                and self.inside_loop(node)
                and self.io_call(node)
            ):
                self.add(
                    node,
                    "loop-io",
                    "Potential synchronous IO inside a loop (potential N+1).",
                    "Batch repeated IO or move invariant work outside the loop.",
                )
        errors = [n for n in self.nodes if n.type == "ERROR" or n.is_missing]
        errors.extend(
            child
            for node in self.nodes
            for child in node.children
            if child.is_missing and child not in errors
        )
        for node in errors[:20]:
            self.add(
                node,
                "syntax-error",
                "Parser recovered invalid or incomplete syntax.",
                "Fix syntax before relying on the remaining review.",
                "error",
            )

    def python(self) -> None:
        """Run Python-specific AST rules."""
        self.unused_imports()
        for node in self.nodes:
            if node.type in {"default_parameter", "typed_default_parameter"}:
                value = node.child_by_field_name("value")
                if value and (
                    value.type
                    in {
                        "list",
                        "dictionary",
                        "set",
                        "list_comprehension",
                        "dictionary_comprehension",
                        "set_comprehension",
                    }
                    or value.type == "call"
                    and self.call_name(value) in {"list", "dict", "set"}
                ):
                    self.add(
                        value,
                        "mutable-default",
                        "Mutable default is shared across calls.",
                        "Use None and create the collection inside the function.",
                    )
            elif node.type == "comparison_operator":
                operands = node.named_children
                operators = node.children_by_field_name("operators")
                if any(
                    op.type in {"==", "!="}
                    and (operands[i].type == "none" or operands[i + 1].type == "none")
                    for i, op in enumerate(operators)
                    if i + 1 < len(operands)
                ):
                    self.add(
                        node,
                        "none-comparison",
                        "Equality comparison with None.",
                        "Use is None or is not None.",
                    )
            elif node.type == "except_clause":
                self.exception_rules(node)
            elif node.type == "call" and self.is_resource(node) and not self.resource_managed(node):
                self.add(
                    node,
                    "unclosed-resource",
                    "Potential resource creation has no visible context manager or close in its scope.",
                    "Use with, try/finally close, or explicitly transfer ownership.",
                )
            elif node.type == "binary_operator":
                op = node_text(node.child_by_field_name("operator"), self.data)
                left, right = node.child_by_field_name("left"), node.child_by_field_name("right")
                if op == "%" and left and self.fstring(left):
                    self.add(
                        node,
                        "mixed-formatting",
                        "f-string is combined with percent formatting.",
                        "Use one formatting method consistently.",
                    )
                elif (
                    op in {"/", "//", "%"} and right and not (left and self.string_expression(left))
                ):
                    number = self.number(right)
                    if number == 0:
                        self.add(
                            right,
                            "possible-zero-division",
                            "Divisor is the constant zero.",
                            "Replace the zero divisor or handle the invalid input.",
                            "error",
                        )
                    elif number is None and not self.division_guarded(node, right):
                        self.add(
                            right,
                            "possible-zero-division",
                            "Nonconstant divisor has no visible dominating nonzero guard.",
                            "Validate the divisor or explicitly handle ZeroDivisionError.",
                        )

    def exception_rules(self, node: Node) -> None:
        """Detect broad silent handlers and handlers whose only effect is logging."""
        value = node.child_by_field_name("value")
        body = next((c for c in node.named_children if c.type == "block"), None)
        statements = [c for c in body.named_children if c.type != "comment"] if body else []
        if value is None:
            self.add(
                node,
                "bare-except",
                "Bare except catches all exceptions including process-control exceptions.",
                "Catch the specific recoverable exception types.",
            )
        names = (
            {node_text(n, self.data) for n in walk(value) if n.type == "identifier"}
            if value
            else set()
        )
        if (
            (value is None or names & {"Exception", "BaseException"})
            and statements
            and all(s.type == "pass_statement" for s in statements)
        ):
            self.add(
                node,
                "swallowed-exception",
                "Broad exception handler silently passes.",
                "Handle the failure explicitly or re-raise it.",
            )
        logs = []
        for statement in statements:
            if statement.type == "pass_statement":
                continue
            call = (
                statement.named_children[0]
                if statement.type == "expression_statement" and statement.named_children
                else None
            )
            name = self.call_name(call) if call and call.type == "call" else ""
            receiver, _, method = name.rpartition(".")
            logs.append(
                method in {"error", "warning", "exception", "info", "debug", "critical", "log"}
                and (receiver in {"logging", "logger", "log"} or receiver.endswith(".logger"))
            )
        if logs and all(logs):
            self.add(
                node,
                "log-only-except",
                "Exception handler only logs and then continues.",
                "Re-raise, return an explicit failure, or document intentional recovery.",
            )

    def string_expression(self, node: Node) -> bool:
        """Recognize literal strings and their explicit formatting calls."""
        while node.type == "parenthesized_expression" and node.named_children:
            node = node.named_children[0]
        if node.type in {"string", "concatenated_string"}:
            return True
        if node.type == "call":
            function = node.child_by_field_name("function")
            if function and function.type == "attribute":
                obj = function.child_by_field_name("object")
                method = node_text(function.child_by_field_name("attribute"), self.data)
                return bool(
                    obj and method in {"format", "format_map"} and self.string_expression(obj)
                )
        return False

    def fstring(self, node: Node) -> bool:
        """Identify f-string prefixes through string AST tokens."""
        while node.type == "parenthesized_expression" and node.named_children:
            node = node.named_children[0]
        if node.type not in {"string", "concatenated_string"}:
            return False
        return any(
            n.type == "string_start" and "f" in node_text(n, self.data).lower() for n in walk(node)
        )

    def is_resource(self, node: Node) -> bool:
        """Identify selected file and connection creation APIs heuristically."""
        name = self.call_name(node)
        return name in {
            "open",
            "io.open",
            "connect",
            "sqlite3.connect",
            "socket.socket",
            "requests.Session",
            "httpx.Client",
            "aiohttp.ClientSession",
        } or name.endswith((".open", ".connect"))

    def resource_managed(self, node: Node) -> bool:
        """Recognize with items, explicit returned ownership and a later local close."""
        parent = node.parent
        while parent and parent.type not in FUNCTIONS:
            if parent.type == "with_item":
                return True
            if parent.type == "return_statement":
                return parent.named_children == [node]
            if parent.type in {"expression_statement", "block"}:
                break
            parent = parent.parent
        assignment = node.parent
        if (
            assignment is None
            or assignment.type != "assignment"
            or assignment.child_by_field_name("right") != node
        ):
            return False
        left = assignment.child_by_field_name("left")
        if left is None or left.type != "identifier":
            return False
        name = node_text(left, self.data)
        owner = scope(node)
        for other in self.nodes:
            if other.start_byte <= node.end_byte or scope(other).id != owner.id:
                continue
            if (
                other.type == "assignment"
                and node_text(other.child_by_field_name("left"), self.data) == name
            ):
                return False
            if other.type == "call" and self.call_name(other) == f"{name}.close":
                return True
            if (
                other.type == "return_statement"
                and len(other.named_children) == 1
                and node_text(other.named_children[0], self.data) == name
            ):
                return True
        return False

    def number(self, node: Node) -> float | None:
        """Recognize numeric literals without eval or compiling inspected code."""
        if node.type == "parenthesized_expression" and node.named_children:
            return self.number(node.named_children[0])
        if node.type == "unary_operator" and len(node.named_children) == 1:
            value = self.number(node.named_children[0])
            operator = node_text(node.child_by_field_name("operator"), self.data)
            if value is not None and operator in {"+", "-"}:
                return value if operator == "+" else -value
        text = node_text(node, self.data).replace("_", "")
        if (
            node.type in {"integer", "float"}
            or node.type == "unary_operator"
            and all(c.type in {"integer", "float"} for c in node.named_children)
        ):
            try:
                return float(int(text, 0)) if node.type == "integer" else float(text)
            except ValueError:
                return None
        return None

    def guard_status(self, condition: Node, name: str) -> int:
        """Return 1 for nonzero-on-true, -1 for zero-on-true, or 0 if unknown."""
        if not name:
            return 0
        if node_text(condition, self.data) == name:
            return 1
        if condition.type == "boolean_operator":
            left, right = (
                condition.child_by_field_name("left"),
                condition.child_by_field_name("right"),
            )
            if left and right:
                values = [self.guard_status(left, name), self.guard_status(right, name)]
                operator = node_text(condition.child_by_field_name("operator"), self.data)
                if operator == "and":
                    return 1 if 1 in values else -1 if -1 in values else 0
                if operator == "or" and values[0] == values[1]:
                    return values[0]
        if condition.type == "parenthesized_expression" and condition.named_children:
            return self.guard_status(condition.named_children[0], name)
        if condition.type == "not_operator" and condition.named_children:
            return -self.guard_status(condition.named_children[-1], name)
        if condition.type == "comparison_operator" and len(condition.named_children) == 2:
            a, b = condition.named_children
            operators = [c.type for c in condition.children if not c.is_named]
            if (node_text(a, self.data) == name and self.number(b) == 0) or (
                node_text(b, self.data) == name and self.number(a) == 0
            ):
                if operators == ["!="]:
                    return 1
                if operators == ["=="]:
                    return -1
                if operators in ([">"], ["<"]):
                    return 1
        return 0

    def division_guarded(self, node: Node, denominator: Node) -> bool:
        """Recognize local guards, early exits and explicit ZeroDivisionError handling."""
        name = node_text(denominator, self.data) if denominator.type == "identifier" else ""
        current: Node | None = node
        while current and current.type not in FUNCTIONS:
            parent = current.parent
            if parent is None:
                break
            if parent.type == "if_statement":
                condition = parent.child_by_field_name("condition")
                consequence = parent.child_by_field_name("consequence")
                if (
                    (
                        condition
                        and consequence
                        and consequence.start_byte <= node.start_byte <= consequence.end_byte
                    )
                    and self.guard_status(condition, name) == 1
                    and not self.modified(name, condition.end_byte, node.start_byte)
                ):
                    return True
            if parent.type == "boolean_operator":
                left = parent.child_by_field_name("left")
                op = node_text(parent.child_by_field_name("operator"), self.data)
                if (
                    left
                    and left.end_byte < node.start_byte
                    and (
                        (op == "and" and self.guard_status(left, name) == 1)
                        or (op == "or" and self.guard_status(left, name) == -1)
                    )
                ):
                    return True
            if parent.type == "try_statement":
                body = parent.child_by_field_name("body")
                if (
                    body
                    and body.start_byte <= node.start_byte <= body.end_byte
                    and any(
                        child.type == "except_clause"
                        and any(
                            n.type == "identifier"
                            and node_text(n, self.data) == "ZeroDivisionError"
                            for n in walk(child.child_by_field_name("value") or child)
                        )
                        for child in parent.named_children
                    )
                ):
                    return True
            if parent.type == "block":
                for previous in parent.named_children:
                    if previous.end_byte >= node.start_byte:
                        break
                    if (
                        previous.type == "if_statement"
                        and previous.child_by_field_name("alternative") is None
                    ):
                        condition = previous.child_by_field_name("condition")
                        body = previous.child_by_field_name("consequence")
                        if (
                            (
                                condition
                                and body
                                and body.named_children
                                and body.named_children[-1].type
                                in {"return_statement", "raise_statement"}
                            )
                            and self.guard_status(condition, name) == -1
                            and not self.modified(name, previous.end_byte, node.start_byte)
                        ):
                            return True
            current = parent
        return False

    def modified(self, name: str, start: int, end: int) -> bool:
        """Invalidate a guard if a visible intervening assignment changes its name."""
        return any(
            n.type in {"assignment", "augmented_assignment", "named_expression"}
            and start <= n.start_byte < end
            and node_text(n.child_by_field_name("left") or n.child_by_field_name("name"), self.data)
            == name
            for n in self.nodes
        )

    def unused_imports(self) -> None:
        """Resolve simple imported bindings with lexical shadowing and explicit exports."""
        imports: list[tuple[Node, str, Node]] = []
        bindings: dict[int, set[str]] = {}
        for node in self.nodes:
            owner = scope(node)
            bindings.setdefault(owner.id, set())
            if node.type in {"import_statement", "import_from_statement"}:
                if node_text(node.child_by_field_name("module_name"), self.data) == "__future__":
                    continue
                for child in node.children_by_field_name("name"):
                    alias = child.child_by_field_name("alias")
                    name = node_text(alias or child, self.data).split(".")[0]
                    if child.type == "aliased_import" and alias is None:
                        continue
                    imported_name = node_text(child.child_by_field_name("name"), self.data)
                    if alias and node_text(alias, self.data) == imported_name:
                        continue
                    imports.append((alias or child, name, owner))
                    bindings[owner.id].add(name)
            elif node.type in {"assignment", "augmented_assignment", "for_statement"}:
                bindings[owner.id].update(
                    binding_names(node.child_by_field_name("left"), self.data)
                )
            elif node.type in FUNCTIONS:
                parameters = node.child_by_field_name("parameters")
                bindings.setdefault(node.id, set())
                if parameters:
                    for param in parameters.named_children:
                        parameter_name = param.child_by_field_name("name") or (
                            param.named_children[0]
                            if param.type == "typed_parameter" and param.named_children
                            else param
                        )
                        bindings[node.id].update(binding_names(parameter_name, self.data))
                bindings[owner.id].update(
                    binding_names(node.child_by_field_name("name"), self.data)
                )
        for declaration, name, owner in imports:
            if not name or name == "*" or name.startswith("_"):
                continue
            used = False
            for node in self.nodes:
                if node.type == "string_content" and node_text(node, self.data) == name:
                    parent = node.parent
                    while parent and parent.type not in {"assignment", "block", "module"}:
                        parent = parent.parent
                    if (
                        parent
                        and parent.type == "assignment"
                        and node_text(parent.child_by_field_name("left"), self.data) == "__all__"
                    ):
                        used = True
                        break
                if (
                    node.type != "identifier"
                    or node_text(node, self.data) != name
                    or self.binding_position(node)
                ):
                    continue
                current = node.parent
                import_context = False
                while current and current.type not in SCOPES:
                    if current.type in {"import_statement", "import_from_statement"}:
                        import_context = True
                        break
                    current = current.parent
                if import_context:
                    continue
                current = scope(node)
                if current.type in FUNCTIONS:
                    body = current.child_by_field_name("body")
                    if body and node.start_byte < body.start_byte:
                        current = scope(current)
                while current.id != owner.id:
                    if name in bindings.get(current.id, set()) or current.type == "module":
                        break
                    current = scope(current)
                else:
                    used = True
                    break
            if not used:
                self.add(
                    declaration,
                    "unused-import",
                    f"Imported binding {name!r} has no visible use.",
                    "Remove the unused import or explicitly document a side-effect import.",
                )

    def binding_position(self, node: Node) -> bool:
        """Exclude declarations, assignment targets and attribute-name components."""
        parent = node.parent
        if parent is None:
            return False
        if parent.type == "attribute" and parent.child_by_field_name("attribute") == node:
            return True
        if (
            parent.type
            in FUNCTIONS | {"class_definition", "default_parameter", "typed_default_parameter"}
            and parent.child_by_field_name("name") == node
        ):
            return True
        if parent.type == "keyword_argument" and parent.child_by_field_name("name") == node:
            return True
        if parent.type == "parameters":
            return True
        if (
            parent.type == "typed_parameter"
            and parent.named_children
            and parent.named_children[0] == node
        ):
            return True
        current = node
        while current.parent and current.parent.type in {
            "tuple_pattern",
            "list_pattern",
            "pattern_list",
            "list_splat_pattern",
            "dictionary_splat_pattern",
        }:
            current = current.parent
        return bool(
            current.parent
            and current.parent.type in {"assignment", "augmented_assignment", "for_statement"}
            and current.parent.child_by_field_name("left") == current
        )


def merge_findings(findings: list[Finding]) -> list[Finding]:
    """Merge all rules at a file/line and retain the highest severity deterministically."""
    groups: dict[tuple[str, int], dict[str, Finding]] = {}
    for finding in findings:
        group = groups.setdefault((finding.path, finding.line), {})
        previous = group.get(finding.rule)
        if previous is None or (SEVERITY[finding.severity], finding.col) < (
            SEVERITY[previous.severity],
            previous.col,
        ):
            group[finding.rule] = finding
    result: list[Finding] = []
    for (path, line), group in groups.items():
        ordered = sorted(group.values(), key=lambda f: (SEVERITY[f.severity], f.rule, f.col))
        primary = ordered[0]
        rules = sorted(group)
        digest = hashlib.sha256(f"{path}:{line}:{','.join(rules)}".encode()).hexdigest()[:12]
        result.append(
            primary.model_copy(
                update={
                    "id": "finding_" + digest,
                    "rule": ",".join(rules),
                    "merged_rules": rules,
                    "message": " | ".join(f.message for f in ordered),
                    "suggestion": " | ".join(
                        dict.fromkeys(f.suggestion for f in ordered if f.suggestion)
                    ),
                }
            )
        )
    return sorted(result, key=lambda f: (SEVERITY[f.severity], f.path, f.line, f.col, f.rule))


def review_directory(
    root: Path,
    *,
    languages: str | None = None,
    rules: str = "all",
    severity: str = "warning+",
    config: Config | None = None,
) -> ReviewReport:
    """Run offline rules on bounded, safely extracted source files."""
    if severity not in {"error", "error+", "warning", "warning+", "info", "info+"}:
        raise ValueError("Severity must be error, warning or info, optionally followed by +")
    level = severity.rstrip("+")
    selected = (
        RULES
        if rules in {"all", "default", "python"}
        else COMMON
        if rules == "general"
        else set(rules.split(","))
    )
    if not selected or not selected <= RULES:
        raise ValueError("Unknown rule set or rule name")
    allowed = (
        {LANGUAGES.get(s.strip(), s.strip()) for s in languages.split(",")}
        if languages
        else set(LANGUAGES.values())
    )
    if not allowed <= set(LANGUAGES.values()):
        raise ValueError("Unsupported review language")
    config = config or Config()
    inventory = scan(root, config)
    skipped = inventory.skipped.copy()
    warnings: list[str] = []
    raw: list[Finding] = []
    markers: Counter[str] = Counter()
    counts: Counter[str] = Counter()
    reviewed = 0
    errors: list[SkippedItem] = []
    for entry in inventory.files:
        if entry.error:
            skipped.append(SkippedItem(path=entry.path, reason=entry.error))
            errors.append(skipped[-1])
            continue
        if entry.kind != "code":
            skipped.append(SkippedItem(path=entry.path, reason="Not a supported source file"))
            continue
        language = LANGUAGES.get(entry.language or "", entry.language or "")
        if language not in allowed:
            skipped.append(
                SkippedItem(path=entry.path, reason="Excluded by review language filter")
            )
            continue
        extracted = extract_file(Path(inventory.root), entry.path, config.max_size)
        if extracted.document is None:
            skipped.append(
                SkippedItem(path=entry.path, reason=extracted.error or "Extraction failed")
            )
            errors.append(skipped[-1])
            continue
        analyzer = Analyzer(extracted.document.body, entry.path, language)
        analyzer.common()
        if language == "python":
            analyzer.python()
        reviewed += 1
        markers.update(analyzer.markers)
        warnings.extend(f"{entry.path}: {w}" for w in extracted.warnings)
        unique: dict[tuple[str, int, str], Finding] = {}
        for finding in analyzer.findings:
            key = (finding.path, finding.line, finding.rule)
            previous = unique.get(key)
            if finding.rule in selected and (
                previous is None
                or (SEVERITY[finding.severity], finding.col)
                < (SEVERITY[previous.severity], previous.col)
            ):
                unique[key] = finding
        counts.update(f.rule for f in unique.values())
        raw.extend(
            f
            for f in unique.values()
            if SEVERITY[f.severity] <= SEVERITY[level]
            if severity.endswith("+") or f.severity == level
        )
    return ReviewReport(
        root=inventory.root,
        files_reviewed=reviewed,
        findings=merge_findings(raw),
        skipped=sorted(skipped, key=lambda s: s.path),
        warnings=warnings,
        rule_counts=dict(sorted(counts.items())),
        marker_counts=dict(sorted(markers.items())),
        errors=errors,
    )
