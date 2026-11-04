"""Plan source-grounded Markdown pages with non-overwriting generated variants."""

import difflib
import hashlib
import html
import re
from importlib.resources import files
from pathlib import Path
from string import Template
from urllib.parse import quote

from curator.analyze.docast import extract_module
from curator.analyze.git_history import read_history
from curator.core.config import Config
from curator.core.scan import scan
from curator.extract import extract_file
from curator.extract.code import LANGUAGES
from curator.models import SkippedItem
from curator.models.docgen import DocArtifact, DocPlan, FileActivity, GitHistory, ModuleDoc
from curator.safety.paths import SafetyError, collision, safe_path


def inline(value: str) -> str:
    """Render untrusted source labels as Markdown code, never HTML or links."""
    value = value.replace("\r", " ").replace("\n", " ").replace("|", r"\|")
    fence = "`" * (max([len(m.group()) for m in re.finditer(r"`+", value)] + [0]) + 1)
    return fence + " " + value + " " + fence


def fenced(value: str, language: str = "text") -> str:
    """Choose a fence that source text cannot close."""
    fence = "`" * (max([len(m.group()) for m in re.finditer(r"`+", value)] + [2]) + 1)
    return f"{fence}{language}\n{value.rstrip()}\n{fence}"


def anchor(path: str, name: str, line: int) -> str:
    """Create a unique stable anchor independent of Markdown heading normalization."""
    return "symbol-" + hashlib.sha256(f"{path}:{name}:{line}".encode()).hexdigest()[:16]


def render_history(activity: list[FileActivity]) -> str:
    """Display local change totals and recent commit subjects without executing markup."""
    lines = [
        "## Git activity",
        "",
        "| File | Commits | Added | Deleted | Binary changes |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for item in activity:
        lines.append(
            f"| {inline(item.path)} | {item.commits} | {item.additions} | {item.deletions} | {item.binary_changes} |"
        )
    for item in activity:
        lines.extend(["", f"### {inline(item.path)}", ""])
        if item.latest_commit:
            lines.extend(
                [
                    f"Latest: {inline(item.latest_commit[:12])} · {inline(item.latest_date or '')}",
                    "",
                ]
            )
        lines.extend(f"- {inline(subject)}" for subject in item.summaries)
    return (
        "\n".join(lines)
        if activity
        else "## Git activity\n\nNo local change history available for these files."
    )


def render_module(modules: list[ModuleDoc], activity: list[FileActivity], source_mode: str) -> str:
    """Render module/class/function hierarchy, signatures, parameter tables and calls."""
    template = Template(
        files("curator").joinpath("templates/doc-module.md").read_text(encoding="utf-8")
    )
    toc: list[str] = []
    body: list[str] = []
    for module in modules:
        if module.docstring:
            body.extend(
                [f"## Module description · {inline(module.path)}", "", fenced(module.docstring), ""]
            )
        if module.calls:
            body.extend(["Module-level calls: " + ", ".join(inline(c) for c in module.calls), ""])
        for symbol in module.symbols:
            target = anchor(symbol.path, symbol.qualified_name, symbol.line)
            label = (
                html.escape(symbol.qualified_name, quote=False)
                .replace(chr(92), chr(92) * 2)
                .replace("[", chr(92) + "[")
                .replace("]", chr(92) + "]")
            )
            toc.append(f"- [{label}](#{target})")
            level = min(6, 2 + symbol.qualified_name.count("."))
            body.extend(
                [
                    f'<a id="{target}"></a>',
                    f"{'#' * level} {inline(symbol.qualified_name)}",
                    "",
                    f"Source: {inline(f'{symbol.path}:{symbol.line}:{symbol.col}')} · {symbol.kind}",
                    "",
                    fenced(symbol.signature, module.language),
                    "",
                ]
            )
            if symbol.decorators:
                body.extend(
                    ["Decorators / annotations:", "", fenced("\n".join(symbol.decorators)), ""]
                )
            if symbol.docstring:
                body.extend([fenced(symbol.docstring), ""])
            if symbol.kind == "function":
                body.extend(["| Parameter | Annotation | Default |", "| --- | --- | --- |"])
                body.extend(
                    f"| {inline(p.name)} | {inline(p.annotation or '—')} | {inline(p.default or '—')} |"
                    for p in symbol.parameters
                )
                if not symbol.parameters:
                    body.append("| — | — | — |")
                body.extend(
                    [
                        "",
                        "Direct textual calls: "
                        + (", ".join(inline(c) for c in symbol.calls) or "None found."),
                        "",
                    ]
                )
    if not body:
        body = ["No AST declarations requested or no supported declarations found."]
    sources = ", ".join(inline(m.path) for m in modules)
    return (
        template.substitute(
            title=inline(modules[0].module),
            source=f"Sources: {sources}\n\nMode: {source_mode}",
            toc="\n".join(toc) or "No declarations.",
            body="\n".join(body),
            history=render_history(activity),
        ).rstrip()
        + "\n"
    )


def _existing(root: Path, relative: str) -> Path | None:
    target = safe_path(root, relative)
    if not target.parent.exists():
        return None
    found = next(
        (p for p in target.parent.iterdir() if p.name.casefold() == target.name.casefold()), None
    )
    if found is not None:
        found = safe_path(root, found.relative_to(root).as_posix())
        if not found.is_file():
            raise SafetyError("Document output conflicts with a directory")
    return found


def _artifact(
    root: Path, desired: str, content: str, sources: list[str], reserved: set[str], max_size: int
) -> DocArtifact:
    base = _existing(root, desired)
    old = None
    if base:
        if base.stat().st_size > max_size:
            raise SafetyError("Existing document exceeds safe diff-read budget")
        old = base.read_text(encoding="utf-8")
    if old == content:
        return DocArtifact(
            path=base.relative_to(root).as_posix() if base else desired,
            content=content,
            source_paths=sources,
            base_path=desired,
            write_required=False,
        )
    difference = (
        "".join(
            difflib.unified_diff(
                (old or "").splitlines(keepends=True),
                content.splitlines(keepends=True),
                fromfile=base.relative_to(root).as_posix() if base else "/dev/null",
                tofile=desired,
            )
        )
        if base
        else None
    )
    candidate = desired
    if base or candidate.casefold() in reserved:
        path = Path(desired)
        number = 1
        while True:
            suffix = ".generated" if number == 1 else f".generated-{number}"
            candidate = path.with_name(path.stem + suffix + path.suffix).as_posix()
            existing = _existing(root, candidate)
            if (
                existing
                and existing.stat().st_size <= max_size
                and existing.read_text(encoding="utf-8") == content
            ):
                return DocArtifact(
                    path=existing.relative_to(root).as_posix(),
                    content=content,
                    source_paths=sources,
                    base_path=desired,
                    diff=difference,
                    write_required=False,
                )
            if candidate.casefold() not in reserved and existing is None:
                break
            number += 1
    safe_path(root, candidate)
    reserved.add(candidate.casefold())
    return DocArtifact(
        path=candidate, content=content, source_paths=sources, base_path=desired, diff=difference
    )


def generate_docs(
    root: Path,
    *,
    out: str = "docs",
    source: str = "both",
    split: str = "module",
    config: Config | None = None,
) -> DocPlan:
    """Build a complete no-write documentation plan; degrade Git failures to AST."""
    if source not in {"ast", "git", "both"} or split not in {"module", "file"}:
        raise ValueError("Invalid documentation source or split mode")
    root = root.resolve(strict=True)
    output = safe_path(root, out)
    if collision(output) and not output.is_dir():
        raise SafetyError("Documentation output conflicts with an existing path")
    config = config or Config()
    history = (
        read_history(
            root,
            max_commits=config.doc_git_max_commits,
            timeout=config.doc_git_timeout,
            max_bytes=config.doc_git_max_bytes,
        )
        if source != "ast"
        else GitHistory(available=False)
    )
    history.files = [
        a
        for a in history.files
        if not (
            a.path.casefold() == out.casefold()
            or a.path.casefold().startswith(out.casefold() + "/")
        )
    ]
    effective = source if source == "ast" or history.available else "ast"
    warnings = history.warnings.copy()
    inventory = scan(root, config)
    skipped = inventory.skipped.copy()
    modules: list[ModuleDoc] = []
    errors: list[SkippedItem] = []
    for entry in inventory.files:
        if entry.path.casefold() == out.casefold() or entry.path.casefold().startswith(
            out.casefold() + "/"
        ):
            skipped.append(
                SkippedItem(path=entry.path, reason="Documentation output excluded from input")
            )
            continue
        if entry.error or entry.kind != "code":
            skipped.append(
                SkippedItem(path=entry.path, reason=entry.error or "Not a supported code file")
            )
            if entry.error:
                errors.append(skipped[-1])
            continue
        language = LANGUAGES.get(entry.language or "", entry.language or "")
        if effective == "git":
            modules.append(
                ModuleDoc(
                    path=entry.path,
                    module=entry.path.rsplit(".", 1)[0].replace("/", "."),
                    language=language,
                )
            )
            continue
        extracted = extract_file(root, entry.path, config.max_size)
        if extracted.document is None:
            skipped.append(
                SkippedItem(path=entry.path, reason=extracted.error or "Extraction failed")
            )
            errors.append(skipped[-1])
            continue
        module = extract_module(extracted.document.body, entry.path, language)
        if module.parse_errors:
            warnings.append(f"{entry.path}: syntax recovery may make documentation incomplete")
        modules.append(module)
    groups: dict[str, list[ModuleDoc]] = {}
    for module in modules:
        if split == "module" and module.language == "go":
            # Files in one Go source directory form the package documentation unit.
            key = (
                Path(module.path).parent.as_posix() + "/go-package-" + (module.package or "unknown")
            )
        else:
            key = module.path
        groups.setdefault(key, []).append(module)
    reserved: set[str] = set()
    artifacts: list[DocArtifact] = []
    index_lines = [
        "# Documentation index",
        "",
        f"Mode: {effective}; split: {split}",
        "",
        "## Modules",
        "",
    ]
    for key, group in sorted(groups.items()):
        first = Path(group[0].path)
        if split == "module":
            relative = (
                first.parent / f"go-{group[0].package or 'package'}.md"
                if group[0].language == "go"
                else first.with_suffix(".md")
            )
            desired = f"{out}/modules/{relative.as_posix()}"
        else:
            desired = f"{out}/files/{first.as_posix()}.md"
        if desired.casefold() in reserved:
            desired = f"{out}/modules/{first.as_posix()}.md"
        paths = [m.path for m in group]
        activity = [a for a in history.files if a.path in paths]
        content = render_module(group, activity, effective)
        artifact = _artifact(root, desired, content, paths, reserved, config.max_size)
        artifacts.append(artifact)
        target = quote(Path(artifact.path).relative_to(Path(out)).as_posix(), safe="/")
        index_lines.append(f"- [{group[0].module.replace('[', '').replace(']', '')}]({target})")
    if history.available:
        index_lines.extend(["", render_history(history.files), ""])
    if warnings:
        index_lines.extend(["", "## Warnings", "", fenced("\n".join(warnings))])
    if artifacts:
        index_content = "\n".join(index_lines).rstrip() + "\n"
        artifacts.append(
            _artifact(
                root,
                f"{out}/index.md",
                index_content,
                [m.path for m in modules],
                reserved,
                config.max_size,
            )
        )
    elif history.available and history.files:
        content = "\n".join(index_lines).rstrip() + "\n"
        artifacts.append(_artifact(root, f"{out}/index.md", content, [], reserved, config.max_size))
    return DocPlan(
        root=str(root),
        requested_from=source,
        effective_from=effective,
        split=split,
        modules=modules,
        history=history,
        artifacts=artifacts,
        skipped=sorted(skipped, key=lambda s: s.path),
        warnings=warnings,
        errors=errors,
    )
