"""The leak gate catches what it must, and nothing it must not.

Both halves matter. A gate that misses a real identifier publishes it
irreversibly; a gate that fires on a legitimate string gets switched off, and
then it misses everything.
"""
import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "leak-check.py"
_spec = importlib.util.spec_from_file_location("leak_check", SCRIPT)
leak_check = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(leak_check)


def found(text, terms=()):
    return {what for what, _ in leak_check.scan_line(text, terms)}


# Every fixture is assembled at runtime, so that this file -- which the gate
# also scans -- never holds the literal it is testing for. Exempting the file
# instead would be a hole exactly the shape of a test fixture.
SOME_ACCOUNT = "9" * 4 + "1" * 4 + "7" * 4
AT = "@"

MUST_CATCH = [
    (f"arn:aws:iam::{SOME_ACCOUNT}:root", "an AWS account id"),
    (f"account {SOME_ACCOUNT}", "an AWS account id"),
    (f"Default: someone{AT}realcompany.io", "an email address"),
    (f"owner: first.last{AT}mail.realcompany.co.uk", "an email address"),
    ("AKIA" + "IOSFODNN7EXAMPLE", "an AWS key id"),
    ("-----BEGIN RSA " + "PRIVATE KEY-----", "a private key"),
    ("aws_secret_" + "access_key = hunter2", "a credential assignment"),
    ('"aws:SourceIp": ["203.0.113.7' + '/32"]', "a host CIDR"),
]


@pytest.mark.parametrize("text, expected", MUST_CATCH)
def test_catches(text, expected):
    assert expected in found(text), f"leak gate missed {expected} in {text!r}"


MUST_NOT_CATCH = [
    "arn:aws:iam::123456789012:role/app-cfn-exec-role",
    "arn:aws:es:us-east-1:111122223333:domain/acme/*",
    "AlertEmail=ops@example.com",
    "noreply@example.org",
    "https://github.com/jnet-platform-factory/aws-eventbridge-opensearch",
    "SemanticVersion: 1.0.0",
    "arn:aws:serverlessrepo:us-east-1:<account>:applications/serverless-events-observability",
    "search-acme-xxxx.us-east-1.es.amazonaws.com",
    "OpenSearchIndex: acme-events",
    "rate(1 minute)",
    "10.0.0.0/16",
    "timestamp 1727690400123",  # 13 digits: not an account id
    "version 2026.09.30",
    f"uses: owner/.github/actions/job-summary{AT}v0.0.1",  # a pinned action, not an address
    f"uses: actions/checkout{AT}v4.1.0",
]


@pytest.mark.parametrize("text", MUST_NOT_CATCH)
def test_does_not_catch(text):
    assert found(text) == set(), f"leak gate false-positived on {text!r}"


def test_denied_terms_are_case_insensitive():
    assert "a denied term" in found("Bucket: ACME-Internal-Assets", ["acme-internal-assets"])
    assert found("Bucket: something-else", ["acme-internal-assets"]) == set()


def test_this_repository_is_clean():
    assert leak_check.main([str(SCRIPT.parent.parent)]) == 0
