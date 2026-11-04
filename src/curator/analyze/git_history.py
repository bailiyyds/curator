"""Bounded, offline Git metadata reads with executable helpers disabled."""

import os
import re
import shutil
import subprocess
from pathlib import Path
from threading import Thread

from curator.models.docgen import FileActivity, GitHistory
from curator.safety.paths import SafetyError, safe_path

MARKER = b"CURATOR_LOG_V1"


def parse_history(data: bytes, root: Path, max_commits: int = 200) -> GitHistory:
    """Parse NUL-delimited commit records and numstat paths, including tabs/newlines."""
    tokens = data.split(b"\x00")
    files: dict[str, FileActivity] = {}
    history = GitHistory(available=True)
    index = 0
    commit = date = subject = ""
    active = False
    seen: set[str] = set()
    while index < len(tokens):
        token = tokens[index].lstrip(b"\n")
        index += 1
        if token == MARKER:
            if index + 2 >= len(tokens):
                history.warnings.append("Incomplete Git commit header ignored")
                break
            raw_commit, raw_date, raw_subject = tokens[index : index + 3]
            index += 3
            if not re.fullmatch(rb"(?:[a-f0-9]{40}|[a-f0-9]{64})", raw_commit):
                history.warnings.append("Malformed Git commit ID ignored")
                active = False
                continue
            history.commit_count += 1
            if history.commit_count > max_commits:
                history.commit_count = max_commits
                history.warnings.append(f"History limited to the latest {max_commits} commits")
                break
            commit, date, subject = (
                raw_commit.decode(),
                raw_date.decode("utf-8", "replace"),
                raw_subject.decode("utf-8", "replace"),
            )
            active, seen = True, set()
        elif active and token:
            fields = token.split(b"\t", 2)
            if len(fields) != 3:
                history.warnings.append("Malformed Git numstat record ignored")
                continue
            added, deleted, raw_path = fields
            path = raw_path.decode("utf-8", "replace")
            try:
                safe_path(root, path)
                if not (added.isdigit() and deleted.isdigit() or added == deleted == b"-"):
                    raise ValueError("Invalid numstat counts")
            except (OSError, ValueError):
                history.warnings.append(f"Unsafe or malformed Git path ignored: {path}")
                continue
            activity = files.setdefault(path, FileActivity(path=path))
            if path not in seen:
                activity.commits += 1
                seen.add(path)
            if added == b"-":
                activity.binary_changes += 1
            else:
                activity.additions += int(added)
                activity.deletions += int(deleted)
            if activity.latest_commit is None:
                activity.latest_commit, activity.latest_date = commit, date
            if subject and subject not in activity.summaries and len(activity.summaries) < 5:
                activity.summaries.append(subject)
    history.files = sorted(files.values(), key=lambda f: f.path)
    return history


def read_history(
    root: Path, *, max_commits: int = 200, timeout: float = 10, max_bytes: int = 8 * 1024 * 1024
) -> GitHistory:
    """Read system Git only; never run a binary or script within the scanned root."""
    if max_commits < 1 or timeout <= 0 or max_bytes < 1:
        raise ValueError("Git budgets must be positive")
    root = root.resolve(strict=True)
    executable = shutil.which("git")
    if executable is None:
        return GitHistory(
            available=False, warnings=["Git unavailable; falling back to AST documentation"]
        )
    binary = Path(executable).resolve()
    if binary.is_relative_to(root):
        return GitHistory(
            available=False,
            warnings=[
                "Git executable resolves inside source root; refused and falling back to AST"
            ],
        )
    env = {key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")}
    env.update(
        GIT_OPTIONAL_LOCKS="0",
        GIT_TERMINAL_PROMPT="0",
        GIT_NO_LAZY_FETCH="1",
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_PAGER="",
    )
    args = [
        str(binary),
        "--no-pager",
        "--no-lazy-fetch",
        "--no-replace-objects",
        "--literal-pathspecs",
        "-c",
        "core.fsmonitor=false",
        "-c",
        "core.hooksPath=",
        "-c",
        "log.showSignature=false",
        "-c",
        "diff.external=",
        "-c",
        "core.attributesFile=",
        "-c",
        "core.quotePath=false",
        "-C",
        str(root),
        "log",
        f"--max-count={max_commits + 1}",
        "--no-show-signature",
        "--no-ext-diff",
        "--no-textconv",
        "--ignore-submodules=all",
        "--no-renames",
        "--relative",
        "--numstat",
        "-z",
        "--format=CURATOR_LOG_V1%x00%H%x00%aI%x00%s%x00",
        "--",
        ".",
    ]
    chunks: list[bytes] = []
    overflow = False
    try:
        with subprocess.Popen(
            args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env
        ) as process:
            if process.stdout is None:
                raise SafetyError("Git stdout pipe unavailable")
            stream = process.stdout

            def consume() -> None:
                nonlocal overflow
                value = stream.read(max_bytes + 1)
                overflow = len(value) > max_bytes
                chunks.append(value[:max_bytes])
                if overflow:
                    process.kill()

            reader = Thread(target=consume, daemon=True)
            reader.start()
            try:
                code = process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
                reader.join(timeout=5)
                return GitHistory(
                    available=False, warnings=["Git history deadline exceeded; falling back to AST"]
                )
            reader.join(timeout=5)
            if reader.is_alive() or overflow:
                return GitHistory(
                    available=False,
                    warnings=["Git history output budget exceeded; falling back to AST"],
                )
            if code != 0:
                return GitHistory(
                    available=False,
                    warnings=[
                        "Git history unavailable (not a repository, empty history or unsupported safe flags); falling back to AST"
                    ],
                )
    except OSError:
        return GitHistory(
            available=False, warnings=["Git could not be started; falling back to AST"]
        )
    return parse_history(b"".join(chunks), root, max_commits)
