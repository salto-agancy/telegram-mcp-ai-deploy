#!/usr/bin/env python3
"""Reject common personal infrastructure markers in public source and Git history."""

from __future__ import annotations

import argparse
import ipaddress
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SELF = "scripts/check_public_safety.py"

PATTERNS = {
    "absolute-user-path": re.compile(r"(?i)(?:/Users/[^/\s]+|[A-Z]:\\Users\\[^\\\s]+)"),
    "personal-email": re.compile(r"(?i)\b[\w.+-]+@[\w.-]+\.[a-z]{2,}\b"),
    "production-name": re.compile(
        r"(?i)\b(?:hostinger|timeweb|abdavidyan|salto_dima|salto9726|unfornate)\b"
    ),
    "personal-bot": re.compile(r"(?i)@salto_[A-Za-z0-9_]+"),
    "phone-number": re.compile(r"(?<![\w])\+[1-9]\d[\d ()-]{8,}\d(?![\w])"),
    "ipv4": re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])"),
}

SAFE_EMAIL_SUFFIXES = ("@users.noreply.github.com",)
SAFE_EMAILS = {"noreply@github.com"}
SAFE_PUBLIC_IPS = {
    "1.1.1.1",
    "8.8.8.8",
    "93.184.216.34",
}


def git(*args: str, binary: bool = False):
    return subprocess.check_output(
        ["git", "-C", str(ROOT), *args],
        text=not binary,
        stderr=subprocess.DEVNULL,
    )


def is_finding(rule: str, value: str) -> bool:
    lower = value.lower()
    if rule == "personal-email":
        return lower not in SAFE_EMAILS and not lower.endswith(SAFE_EMAIL_SUFFIXES)
    if rule == "phone-number":
        digits = re.sub(r"\D", "", value)
        return len(digits) >= 10 and not digits.startswith("123456789")
    if rule == "ipv4":
        try:
            address = ipaddress.ip_address(value)
        except ValueError:
            return False
        if value in SAFE_PUBLIC_IPS:
            return False
        return not (address.is_loopback or address.is_private or address.is_link_local)
    return True


def scan(label: str, text: str) -> list[tuple[str, str, int]]:
    findings: list[tuple[str, str, int]] = []
    for line_number, line in enumerate(text.splitlines(), 1):
        for rule, pattern in PATTERNS.items():
            for match in pattern.finditer(line):
                if is_finding(rule, match.group(0)):
                    findings.append((rule, label, line_number))
    return findings


def tree_findings(revision: str | None) -> list[tuple[str, str, int]]:
    if revision:
        names = git("ls-tree", "-r", "--name-only", revision).splitlines()
    else:
        names = git("ls-files", "--cached", "--others", "--exclude-standard").splitlines()
    findings: list[tuple[str, str, int]] = []
    for name in names:
        if name == SELF:
            continue
        try:
            raw = git("show", f"{revision}:{name}", binary=True) if revision else (ROOT / name).read_bytes()
            if len(raw) > 3_000_000:
                continue
            text = raw.decode("utf-8")
        except (OSError, subprocess.CalledProcessError, UnicodeDecodeError):
            continue
        label = f"{revision[:12]}:{name}" if revision else name
        findings.extend(scan(label, text))
    return findings


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--worktree", action="store_true")
    parser.add_argument("--history", action="store_true")
    args = parser.parse_args()

    findings = tree_findings(None) if args.worktree or not args.history else []
    if args.history:
        for revision in git("rev-list", "--all").splitlines():
            findings.extend(tree_findings(revision))
        # GitHub Actions checks out a synthetic merge commit for pull requests. Its
        # author metadata comes from the account profile rather than either real
        # commit, so scan only publishable non-merge commit metadata here. Every
        # tree, including merge trees, is still scanned above.
        findings.extend(
            scan(
                "git-metadata",
                git("log", "--all", "--no-merges", "--format=%H %an %ae"),
            )
        )

    unique = sorted(set(findings))
    if unique:
        for rule, label, line_number in unique:
            print(
                f"FOUND_PRIVATE_DATA: {rule}\nLOCATION: {label}:{line_number}\nVALUE: REDACTED",
                file=sys.stderr,
            )
        print("PUBLIC SAFETY SCAN FAILED", file=sys.stderr)
        return 1
    print("PUBLIC SAFETY SCAN PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
