#!/usr/bin/env python3
"""Fail if anything internal is about to become public.

This repository is public, and so is anything published from it to the
Serverless Application Repository -- and a published SemanticVersion cannot be
withdrawn. So the whole tree is scanned, not a published subset: in a public
repository there is no private corner.

The patterns are deliberately generic. A gate that works by listing the real
account ids it must not publish is itself an inventory of those ids, and could
never be committed here. Instead:

- any 12-digit number is refused unless it is one of the documentation
  placeholders AWS itself uses;
- any email address is refused unless it is on a reserved example domain;
- credentials and host CIDRs are refused outright.

Organisation-specific terms (bucket names, internal domains) can be added
without committing them: one per line in a git-ignored `.leak-check-deny` file,
or comma-separated in the LEAK_CHECK_DENY environment variable (a CI secret).

    make leak-check                 # this tree
    python3 scripts/leak-check.py packaged-dir/   # any other tree
"""
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

SKIP_DIRS = {".git", "__pycache__", ".aws-sam", ".pytest_cache", ".ruff_cache", ".venv"}
SKIP_FILES = {".leak-check-deny"}
SKIP_SUFFIXES = {".pyc"}

# The account ids AWS uses in its own documentation, and the ones this repo's
# examples use. Anything else that looks like an account id is treated as one.
PLACEHOLDER_ACCOUNT_IDS = {"123456789012", "111122223333", "444455556666", "000000000000"}

# RFC 2606 reserved names, which can never belong to anyone.
EXAMPLE_EMAIL_DOMAINS = re.compile(r"@(?:[\w-]+\.)*(?:example\.(?:com|org|net)|example|test|invalid|localhost)$", re.I)

ACCOUNT_ID = re.compile(r"(?<![\w.-])\d{12}(?![\w-])")
# The last label must be alphabetic, as every real top-level domain is; without
# that, a pinned action reference such as `actions/checkout@v4.1.0` reads as an
# address on the domain "v4.1.0".
EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)*\.[A-Za-z]{2,}(?![\w-])")

PATTERNS = [
    (re.compile(r"\b(AKIA|ASIA|AIDA|AROA|AGPA|AIPA|ANPA|ANVA)[A-Z0-9]{16}\b"), "an AWS key id"),
    (re.compile(r"BEGIN (?:RSA |EC |OPENSSH |PGP )?PRIVATE KEY"), "a private key"),
    (re.compile(r"\baws_secret_access_key\s*[=:]", re.I), "a credential assignment"),
    # A /32 names one host -- typically an office or bastion IP allowed through
    # a policy. A VPC CIDR is fine; a host CIDR is not.
    (re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}/32\b"), "a host CIDR"),
]


def deny_terms(root: Path):
    terms = []
    env = os.environ.get("LEAK_CHECK_DENY", "")
    terms += [t.strip() for t in env.split(",") if t.strip()]
    deny_file = root / ".leak-check-deny"
    if deny_file.is_file():
        for line in deny_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                terms.append(line)
    return terms


def files(root: Path):
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root)
        if SKIP_DIRS & set(rel.parts) or path.name in SKIP_FILES or path.suffix in SKIP_SUFFIXES:
            continue
        yield path


def scan_line(line: str, terms=()):
    """Every (what, matched) finding in one line of text."""
    found = []
    for match in ACCOUNT_ID.finditer(line):
        if match.group(0) not in PLACEHOLDER_ACCOUNT_IDS:
            found.append(("an AWS account id", match.group(0)))
    for match in EMAIL.finditer(line):
        if not EXAMPLE_EMAIL_DOMAINS.search(match.group(0)):
            found.append(("an email address", match.group(0)))
    for pattern, what in PATTERNS:
        match = pattern.search(line)
        if match:
            found.append((what, match.group(0)))
    lowered = line.lower()
    for term in terms:
        if term.lower() in lowered:
            found.append(("a denied term", term))
    return found


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    root = Path(argv[0]).resolve() if argv else ROOT
    if not root.is_dir():
        print(f"leak-check: {root} is not a directory")
        return 2

    terms = deny_terms(ROOT)
    findings = []
    scanned = 0
    for path in files(root):
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue  # a binary; nothing readable to leak
        scanned += 1
        for lineno, line in enumerate(text.splitlines(), 1):
            for what, matched in scan_line(line, terms):
                findings.append((path.relative_to(root), lineno, what, matched))

    extra = f", {len(terms)} denied term(s)" if terms else ""
    print(f"leak-check: scanned {scanned} file(s){extra}")
    if not findings:
        print("leak-check: clean")
        return 0

    print(f"\nleak-check: REFUSING — {len(findings)} finding(s)\n")
    for rel, lineno, what, matched in findings:
        print(f"  {rel}:{lineno}  {what}: {matched}")
    print(
        "\nThis repository is public and a published SemanticVersion cannot be "
        "withdrawn.\nMove the value to a stack parameter with no default, or use "
        "a placeholder (123456789012, someone@example.com)."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
