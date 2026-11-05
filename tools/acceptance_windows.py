"""Run from the repository root on Windows; leaves temporary evidence fixtures intact."""

import ctypes
import hashlib
import json
import subprocess
import sys
import tempfile
import time
from ctypes import wintypes
from pathlib import Path

from curator.core.scan import scan
from curator.safety.paths import SafetyError, safe_path


class Counters(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("PageFaultCount", wintypes.DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
    ]


repo = Path.cwd()
root = Path(tempfile.mkdtemp(prefix="curator-mixed-"))
pdf = (repo / "examples/sample.pdf").read_bytes()
for i in range(1000):
    suffix, content = [
        (".txt", b"text sample alpha"),
        (".md", "# 文件策展\n\nMarkdown body".encode()),
        (".py", b"def add(a, b):\n    return a + b\n"),
        (".pdf", pdf),
        (".png", b"\x89PNG\r\n\x1a\nimage"),
        (".bin", bytes(range(256))),
    ][i % 6]
    (root / (str(i) + suffix)).write_bytes(content)
start = time.perf_counter()
report = scan(root)
elapsed = time.perf_counter() - start
ctypes.windll.kernel32.GetCurrentProcess.restype = wintypes.HANDLE
handle = ctypes.windll.kernel32.GetCurrentProcess()
counters = Counters()
counters.cb = ctypes.sizeof(counters)
ctypes.windll.psapi.GetProcessMemoryInfo.argtypes = [
    wintypes.HANDLE,
    ctypes.POINTER(Counters),
    wintypes.DWORD,
]
assert ctypes.windll.psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb)
assert len(report.files) == 1000 and elapsed < 10
assert counters.PeakWorkingSetSize < 300 * 1024 * 1024

outer = Path(tempfile.mkdtemp(prefix="curator-outside-"))
(outer / "sentinel.txt").write_text("unchanged", encoding="utf-8")
linkroot = Path(tempfile.mkdtemp(prefix="curator-junction-"))
link = linkroot / "escape"
result = subprocess.run(
    ["cmd", "/c", "mklink", "/J", str(link), str(outer)], capture_output=True, check=True
)
before = hashlib.sha256((outer / "sentinel.txt").read_bytes()).hexdigest()
linked = scan(linkroot)
assert not linked.files and any(item.path == "escape" for item in linked.skipped)
try:
    safe_path(linkroot, "escape/created.txt")
except SafetyError:
    pass
else:
    raise AssertionError("Junction write was not rejected")
assert [p.name for p in outer.iterdir()] == ["sentinel.txt"]
assert hashlib.sha256((outer / "sentinel.txt").read_bytes()).hexdigest() == before
assert [p.name for p in linkroot.iterdir()] == ["escape"]
evidence = {
    "mixed_files": len(report.files),
    "elapsed_seconds": elapsed,
    "peak_process_working_set_bytes": counters.PeakWorkingSetSize,
    "kind_counts": {
        kind: sum(f.kind == kind for f in report.files) for kind in {f.kind for f in report.files}
    },
    "junction_skipped": [item.model_dump() for item in linked.skipped],
    "junction_write_rejected": True,
    "outside_unchanged": True,
    "platform": sys.platform,
}
(repo / "ACCEPTANCE-METRICS.json").write_text(
    json.dumps(evidence, indent=2, ensure_ascii=False), encoding="utf-8"
)
sys.stdout.write(json.dumps(evidence, indent=2, ensure_ascii=True))
