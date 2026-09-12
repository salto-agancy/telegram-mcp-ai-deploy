#!/usr/bin/env python3
"""Fail when tracked/current candidate files or Git history contain credential shapes."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PATTERNS = {
    "private-key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "telegram-bot-token": re.compile(r"\b\d{8,12}:[A-Za-z0-9_-]{30,}\b"),
    "github-token": re.compile(r"\b(?:ghp|github_pat|gho|ghu|ghs|ghr)_[A-Za-z0-9_]{20,}\b"),
    "aws-access-key": re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    "slack-token": re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b"),
    "generic-assignment": re.compile(
        r"(?i)(?:api[_-]?hash|client[_-]?secret|access[_-]?token|api[_-]?token|password)"
        r"\s*[:=]\s*['\"]?([A-Za-z0-9_+=-]{24,})"
    ),
}
SAFE_MARKERS = ("replace_with", "your_", "example", "placeholder", "redacted", "${")


def git(*args: str) -> str:
    return subprocess.check_output(["git", "-C", str(ROOT), *args], text=True, stderr=subprocess.DEVNULL)


def candidate_files() -> list[Path]:
    names = git("ls-files", "--cached", "--others", "--exclude-standard", "-z").split("\0")
    return [ROOT / name for name in names if name]


def scan_text(label: str, text: str) -> list[tuple[str, int, str]]:
    findings: list[tuple[str, int, str]] = []
    for line_no, line in enumerate(text.splitlines(), 1):
        if any(marker in line.lower() for marker in SAFE_MARKERS):
            continue
        for rule, pattern in PATTERNS.items():
            if pattern.search(line):
                findings.append((label, line_no, rule))
    return findings


def scan_worktree() -> list[tuple[str, int, str]]:
    findings: list[tuple[str, int, str]] = []
    for path in candidate_files():
        if not path.is_file() or path.stat().st_size > 2_000_000:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        findings.extend(scan_text(str(path.relative_to(ROOT)), text))
    return findings


def scan_history() -> list[tuple[str, int, str]]:
    try:
        commits = git("rev-list", "--all").splitlines()
    except subprocess.CalledProcessError:
        return []
    findings: list[tuple[str, int, str]] = []
    for commit in commits:
        tree = git("ls-tree", "-r", "--name-only", commit).splitlines()
        for name in tree:
            try:
                raw = subprocess.check_output(["git", "-C", str(ROOT), "show", f"{commit}:{name}"], stderr=subprocess.DEVNULL)
                text = raw.decode("utf-8")
            except (subprocess.CalledProcessError, UnicodeDecodeError):
                continue
            findings.extend(scan_text(f"{commit[:12]}:{name}", text))
    return findings


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--worktree", action="store_true")
    parser.add_argument("--history", action="store_true")
    args = parser.parse_args()
    findings = scan_worktree() if args.worktree or not args.history else []
    if args.history:
        findings.extend(scan_history())
    if findings:
        for label, line, rule in findings:
            print(f"FOUND_SECRET: {rule}\nLOCATION: {label}:{line}\nVALUE: REDACTED", file=sys.stderr)
        print("SECRET SCAN FAILED — push is blocked", file=sys.stderr)
        return 1
    print("SECRET SCAN PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
