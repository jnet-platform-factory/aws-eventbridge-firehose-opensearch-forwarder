#!/usr/bin/env python3
"""Check the SAR metadata against the limits `sam publish` enforces.

These are all things the publish step rejects -- but it rejects them *after*
build and package, so each one costs a full CI run to discover. Worse, a publish
failure is the one failure mode you cannot simply retry your way out of if it
happens to succeed partially: a SemanticVersion is immutable.

So: the same rules, checked in seconds, on every pull request.

Deliberately not parsed with a YAML library. The template uses CloudFormation
short tags (!Sub, !GetAtt, !If) which a plain yaml.safe_load refuses, and adding
a dependency to catch a string-length bug is the wrong trade.
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "template.yaml"

# https://docs.aws.amazon.com/serverlessrepo/latest/devguide/list-applications.html
MAX_DESCRIPTION = 256
MAX_NAME = 140
NAME_PATTERN = re.compile(r"^[a-zA-Z0-9\-]+$")
SEMVER = re.compile(r"^\d+\.\d+\.\d+$")


def folded_block(text: str, key: str) -> str:
    """The value of a `key: >` folded block, as one line -- which is how it is counted."""
    start = text.index(f"    {key}: >")
    lines = text[start:].splitlines()[1:]
    out = []
    for line in lines:
        if line.strip() and not line.startswith("      "):
            break
        if line.strip():
            out.append(line.strip())
    return " ".join(out)


def main() -> int:
    text = TEMPLATE.read_text()
    problems = []

    description = folded_block(text, "Description")
    if not 1 <= len(description) <= MAX_DESCRIPTION:
        problems.append(
            f"Description is {len(description)} characters; SAR allows 1-{MAX_DESCRIPTION}. "
            f"It is counted after YAML folds the block into one line."
        )

    name = re.search(r"^    Name: (.+)$", text, re.M)
    if not name:
        problems.append("No Name in the AWS::ServerlessRepo::Application metadata")
    else:
        value = name.group(1).strip()
        if not NAME_PATTERN.match(value) or len(value) > MAX_NAME:
            problems.append(f"Name {value!r} must match {NAME_PATTERN.pattern} and be <= {MAX_NAME} chars")

    version = re.search(r"^    SemanticVersion: (.+)$", text, re.M)
    if not version:
        problems.append("No SemanticVersion — sam publish cannot infer one")
    elif not SEMVER.match(version.group(1).strip()):
        problems.append(f"SemanticVersion {version.group(1).strip()!r} is not x.y.z")

    # LicenseUrl and ReadmeUrl are local paths pre-publish; sam package uploads
    # them. A missing file fails the publish, not the package.
    for key in ("LicenseUrl", "ReadmeUrl"):
        found = re.search(rf"^    {key}: (.+)$", text, re.M)
        if not found:
            problems.append(f"No {key} — required for a public listing")
        elif not (ROOT / found.group(1).strip()).exists():
            problems.append(f"{key} points at {found.group(1).strip()}, which does not exist")

    # The README is the public front page, and its quick-start block names a
    # SemanticVersion. Nothing about publishing 1.4.0 updates a "1.1.0" written
    # in prose, so the first thing a new consumer copies is silently a version
    # behind -- which is how it reached three versions stale once already.
    if version and SEMVER.match(version.group(1).strip()):
        current = version.group(1).strip()
        readme = ROOT / "README.md"
        if readme.exists():
            stale = {
                v
                for v in re.findall(
                    r"SemanticVersion:\s*([0-9]+\.[0-9]+\.[0-9]+)", readme.read_text()
                )
                if v != current
            }
            if stale:
                problems.append(
                    f"README.md advertises SemanticVersion {', '.join(sorted(stale))} "
                    f"but template.yaml publishes {current}"
                )

    if problems:
        print("SAR metadata will be rejected by `sam publish`:\n")
        for p in problems:
            print(f"  - {p}")
        return 1

    print(f"SAR metadata ok (Description {len(description)}/{MAX_DESCRIPTION} chars)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
