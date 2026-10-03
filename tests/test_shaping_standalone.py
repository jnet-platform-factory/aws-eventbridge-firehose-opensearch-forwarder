"""The seam, asserted rather than documented.

`shaping.py` is the tenant half and must stay importable without any delivery
dependency. If it stops being, the generic half is no longer separable and the
field maps can no longer be validated anywhere but inside a built Lambda — which
is how hand-maintained per-deployment copies drift apart.

If this fails, the seam has been re-crossed. Fix the import, don't relax the test.
"""
import subprocess
import sys
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parent.parent / "app"

FORBIDDEN = ("opensearchpy", "boto3", "botocore", "requests_aws4auth")


def test_shaping_imports_without_delivery_dependencies():
    # Setting sys.modules[name] = None makes any import of that name raise
    # ImportError, including a *transitive* one — so this also catches the seam
    # being crossed indirectly, e.g. someone adding a repositories import to
    # domain.py.
    program = (
        "import sys\n"
        f"for name in {FORBIDDEN!r}:\n"
        "    sys.modules[name] = None\n"
        "import src.shaping\n"
        "assert callable(src.shaping.shape)\n"
        "assert callable(src.shaping._as_int)\n"
        "assert callable(src.shaping.load_config)\n"
        "print('ok')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", program],
        cwd=str(APP_ROOT),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        "src.shaping pulled in a delivery dependency:\n" + result.stderr
    )


def test_config_validation_needs_no_delivery_dependencies():
    """A field map must be checkable without an OpenSearch client.

    This is what lets CI validate every example map in a bare Python, and what
    lets a deployment validate its own config before deploying it.
    """
    examples = Path(__file__).resolve().parent / "fixtures" / "shaping"
    program = (
        "import sys, json, pathlib\n"
        f"for name in {FORBIDDEN!r}:\n"
        "    sys.modules[name] = None\n"
        "from src.shaping import load_config\n"
        f"for f in sorted(pathlib.Path({str(examples)!r}).glob('*.json')):\n"
        "    load_config(f.read_text())\n"
        "    print('ok', f.name)\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", program],
        cwd=str(APP_ROOT),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        "validating shaping configs pulled in a delivery dependency:\n" + result.stderr
    )
    # An empty glob would pass the loop above without validating anything.
    assert result.stdout.count("ok ") == len(list(examples.glob("*.json"))) > 0
