#!/usr/bin/env python3
"""Assert the built artifact actually contains its dependencies.

SAR 1.1.0 shipped 12 KB of bare source and no dependencies, and every tenant
deploying it got the same cold start:

    Runtime.ImportModuleError: Unable to import module 'src.handler':
    No module named 'aws_lambda_powertools'

The cause was `sam package --template-file template.yaml` -- the ORIGINAL
template -- after `sam build` had carefully produced a built one under
.aws-sam/build/. Packaging the source template uploads `app/` verbatim,
dependencies and all missing, and every gate still passes: the template is valid,
the tests pass, the leak check finds nothing, and the publish succeeds. The only
thing that notices is a Lambda at runtime, in someone else's account, after the
version is immutable.

So this checks the one thing none of those did: that what is about to be uploaded
can actually start.
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BUILD = ROOT / ".aws-sam" / "build"

# Distribution name in requirements.txt -> the package directory pip installs.
IMPORT_NAMES = {
    "aws-lambda-powertools": "aws_lambda_powertools",
    "pydantic": "pydantic",
    "opensearch-py": "opensearchpy",
    "requests-aws4auth": "requests_aws4auth",
}


def required() -> list[str]:
    text = (ROOT / "app" / "requirements.txt").read_text()
    names = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        dist = re.split(r"[=<>!~\[]", line, 1)[0].strip()
        names.append(IMPORT_NAMES.get(dist, dist.replace("-", "_")))
    return names


def main() -> int:
    if not BUILD.is_dir():
        print(f"check-build: {BUILD} does not exist — run `sam build` first")
        return 2

    funcs = [d for d in BUILD.iterdir() if d.is_dir() and (d / "src").is_dir()]
    if not funcs:
        print(f"check-build: no built function directories under {BUILD}")
        return 2

    wanted = required()
    problems = []
    for func in sorted(funcs):
        missing = [n for n in wanted if not (func / n).exists()]
        status = "ok" if not missing else "MISSING " + ", ".join(missing)
        print(f"  {func.name}: {status}")
        if missing:
            problems.append(func.name)

    if problems:
        print(
            "\ncheck-build: the built artifact is missing dependencies. Packaging this "
            "publishes a function that cannot import its own handler.\n"
            "Package the BUILT template (.aws-sam/build/template.yaml), not the source one."
        )
        return 1

    print(f"check-build: all {len(funcs)} function(s) carry their dependencies")
    return 0


if __name__ == "__main__":
    sys.exit(main())
