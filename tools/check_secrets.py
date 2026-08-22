#!/usr/bin/env python3
"""Fail CI when high-confidence credentials are present in tracked files."""
from __future__ import annotations

from pathlib import Path
import re
import subprocess


_PATTERNS = (
    ("OpenAI-style key", re.compile(rb"\b" + re.escape(b"sk" + b"-")
                                     + rb"[A-Za-z0-9_-]{20,}\b")),
    ("GitHub token", re.compile(rb"\bgh[pousr]_[A-Za-z0-9]{20,}\b")),
    ("AWS access key", re.compile(rb"\bAKIA[0-9A-Z]{16}\b")),
    ("private key", re.compile((b"-----BEGIN " +
                                b"(?:RSA |EC |OPENSSH )?PRIVATE KEY-----"))),
)
_SENSITIVE_NAMES = {".env", "mytips.md", "credentials.json"}
_MAX_TEXT_BYTES = 5 * 1024 * 1024
_ALLOW_MARKER = b"secret-scan: allow-test-fixture"


def _candidate_paths() -> tuple[Path, ...]:
    result = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        check=True, capture_output=True)
    return tuple(Path(raw.decode("utf-8", errors="surrogateescape"))
                 for raw in result.stdout.split(b"\0") if raw)


def _scan(path: Path) -> list[tuple[int, str]]:
    if path.name.lower() in _SENSITIVE_NAMES:
        return [(0, "sensitive filename is tracked")]
    try:
        if path.stat().st_size > _MAX_TEXT_BYTES:
            return []
        data = path.read_bytes()
    except OSError:
        return []
    if b"\0" in data:
        return []
    findings: list[tuple[int, str]] = []
    for label, pattern in _PATTERNS:
        for match in pattern.finditer(data):
            line_end = data.find(b"\n", match.end())
            if line_end < 0:
                line_end = len(data)
            if _ALLOW_MARKER in data[max(0, match.start() - 200):line_end]:
                continue
            line = data.count(b"\n", 0, match.start()) + 1
            findings.append((line, label))
    return findings


def main() -> int:
    findings = [(path, line, label) for path in _candidate_paths()
                for line, label in _scan(path)]
    for path, line, label in findings:
        location = f"{path}:{line}" if line else str(path)
        print(f"secret scan: {location}: {label}")
    if findings:
        print(f"secret scan failed: {len(findings)} finding(s); values suppressed")
        return 1
    print("secret scan passed: tracked tree contains no high-confidence credentials")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
