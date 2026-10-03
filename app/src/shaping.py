"""The tenant seam — one EventBridge envelope in, one OpenSearch document out.

Everything else in this service is tenant-agnostic. This module is the part that
is allowed to vary, and in the hand-maintained copies this product replaces it
was the *only* part that meaningfully did: comparing two of those forwarders
line for line, `application.py` differed in 2 lines of 9 and `repositories.py`
in 8 of 51, while the shaping differed in roughly half its lines.

So the shaping is not code here. It is a declarative field map supplied per
tenant as the `ShapingConfig` stack parameter, and this module is the
interpreter for it: one config per tenant, one deployed artifact.

    {
      "defaults": {"application": "unknown", "organization": "default_org"},
      "promote":  [{"from": "num_pages", "as": "num_pages", "type": "int"}],
      "nested":   {"organization_user_context": {"username": "username"}}
    }

Nothing here imports `opensearch-py`, `requests-aws4auth` or `boto3`. That is
checked by `tests/test_shaping_standalone.py`, and it is what keeps the generic
half separable: a tenant supplies JSON, never a dependency.
"""
import json
from typing import Any, Dict, Optional

from aws_lambda_powertools.utilities.data_classes import EventBridgeEvent

from .domain import AppEventModel

# The fields every document carries, whatever the tenant. They are resolved from
# the envelope's `detail` and are the only names `nested` may refer to.
CORE_FIELDS = ("environment", "application", "organization", "username")

DEFAULT_CONFIG: Dict[str, Any] = {
    "defaults": {
        "environment": "unknown",
        "application": "unknown",
        "organization": "default_org",
        "username": "default_user",
    },
    "promote": [],
    "nested": {},
}


class ShapingConfigError(ValueError):
    """A ShapingConfig that cannot be honoured exactly as written.

    Always fatal, never falls back to DEFAULT_CONFIG. A tenant who mistypes
    `num_pages` and silently gets the default shape has not lost a field — they
    have permanently decided the index's mapping for that field, because
    OpenSearch infers each field's type from the first non-null value it ever
    sees. Failing the cold start is recoverable; shaping the wrong document is
    not.
    """


def _as_int(value: Any) -> Optional[int]:
    """An integer, or ``None`` for anything that is not cleanly one.

    Deliberately strict, and it is worth saying why at length, because this
    three-line function is the guard on a decision that cannot be taken twice.

    An index with no explicit mapping — no index template, nothing in Terraform —
    has OpenSearch infer each field's type from the **first non-null value it ever
    sees**, and that inference is permanent for the life of the index. Where every
    stage writes to one shared index, a single dev event carrying ``"12"`` instead
    of ``12`` maps the field as text, and every ``sum`` over it fails from then on
    — fixable only by a reindex.

    The failure is also silent. ``OpenSearchAppEventsRepository.save_event``
    swallows indexing exceptions, so a mapper conflict discards the **whole event
    document**, not just this field, and raises no alarm.

    So: coerce what is unambiguously an integer, and drop anything else rather than
    let a careless emitter decide the mapping. ``bool`` is excluded on purpose — it
    is an ``int`` subclass in Python, and "True pages" is a bug worth losing, not
    recording.
    """
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    # A float or a numeric string is accepted only when it is exactly an integer.
    # This is the realistic case: `json.dumps(..., default=str)` on the publishing
    # side stringifies anything that is not JSON-native, so a Decimal page count
    # arrives here as "12".
    try:
        as_float = float(value)
    except (TypeError, ValueError):
        return None
    return int(as_float) if as_float.is_integer() else None


def _as_str(value: Any) -> Optional[str]:
    """A string, or ``None``. Same one-way-door reasoning as ``_as_int``."""
    if value is None:
        return None
    return value if isinstance(value, str) else str(value)


def _as_raw(value: Any) -> Any:
    """Whatever arrived, untouched.

    The honest default, and the risky one: an emitter that changes a promoted
    field's JSON type changes the index mapping. Prefer an explicit ``type``.
    """
    return value


COERCERS = {"int": _as_int, "str": _as_str, "raw": _as_raw}


def load_config(raw: Optional[str]) -> Dict[str, Any]:
    """Parse and validate a ShapingConfig JSON string into a usable field map.

    Strict on purpose — unknown keys are an error, not a shrug. A typo in a key
    name is indistinguishable from a field a tenant meant to promote and did not,
    and the consequence of the latter is a permanent index mapping. Every rejection
    names the offending key so the cold-start log says what to fix.
    """
    if raw is None or not raw.strip():
        return json.loads(json.dumps(DEFAULT_CONFIG))  # a fresh deep copy

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ShapingConfigError(f"ShapingConfig is not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ShapingConfigError(f"ShapingConfig must be a JSON object, got {type(parsed).__name__}")

    unknown = set(parsed) - {"defaults", "promote", "nested"}
    if unknown:
        raise ShapingConfigError(f"ShapingConfig has unknown key(s): {sorted(unknown)}")

    config = {
        "defaults": dict(DEFAULT_CONFIG["defaults"]),
        "promote": [],
        "nested": {},
    }

    defaults = parsed.get("defaults", {})
    if not isinstance(defaults, dict):
        raise ShapingConfigError("ShapingConfig.defaults must be an object")
    unknown = set(defaults) - set(CORE_FIELDS)
    if unknown:
        raise ShapingConfigError(
            f"ShapingConfig.defaults may only set {list(CORE_FIELDS)}; got {sorted(unknown)}. "
            "Anything else belongs in `promote`."
        )
    for field, value in defaults.items():
        if not isinstance(value, str):
            raise ShapingConfigError(f"ShapingConfig.defaults.{field} must be a string")
    config["defaults"].update(defaults)

    promote = parsed.get("promote", [])
    if not isinstance(promote, list):
        raise ShapingConfigError("ShapingConfig.promote must be a list")
    seen = set()
    for i, entry in enumerate(promote):
        if not isinstance(entry, dict):
            raise ShapingConfigError(f"ShapingConfig.promote[{i}] must be an object")
        unknown = set(entry) - {"from", "as", "type"}
        if unknown:
            raise ShapingConfigError(f"ShapingConfig.promote[{i}] has unknown key(s): {sorted(unknown)}")
        source = entry.get("from")
        if not isinstance(source, str) or not source:
            raise ShapingConfigError(f"ShapingConfig.promote[{i}].from is required and must be a non-empty string")
        target = entry.get("as", source)
        if not isinstance(target, str) or not target:
            raise ShapingConfigError(f"ShapingConfig.promote[{i}].as must be a non-empty string")
        # A promotion that shadows a core field would be resolved twice, by two
        # different rules, with the later one silently winning.
        if target in CORE_FIELDS or target in ("source", "event", "payload", "event_id", "timestamp"):
            raise ShapingConfigError(
                f"ShapingConfig.promote[{i}].as={target!r} collides with a built-in document field"
            )
        if target in seen:
            raise ShapingConfigError(f"ShapingConfig.promote[{i}].as={target!r} is promoted more than once")
        seen.add(target)
        coercion = entry.get("type", "raw")
        if coercion not in COERCERS:
            raise ShapingConfigError(
                f"ShapingConfig.promote[{i}].type={coercion!r} is not one of {sorted(COERCERS)}"
            )
        config["promote"].append({"from": source, "as": target, "type": coercion})

    nested = parsed.get("nested", {})
    if not isinstance(nested, dict):
        raise ShapingConfigError("ShapingConfig.nested must be an object")
    for name, spec in nested.items():
        if name in seen or name in CORE_FIELDS:
            raise ShapingConfigError(f"ShapingConfig.nested.{name} collides with another field")
        if not isinstance(spec, dict) or not spec:
            raise ShapingConfigError(f"ShapingConfig.nested.{name} must be a non-empty object")
        for key, ref in spec.items():
            if ref not in CORE_FIELDS:
                raise ShapingConfigError(
                    f"ShapingConfig.nested.{name}.{key}={ref!r} must name one of {list(CORE_FIELDS)}"
                )
        config["nested"][name] = dict(spec)

    return config


def shape(event: EventBridgeEvent, config: Optional[Dict[str, Any]] = None) -> AppEventModel:
    """One EventBridge envelope in, one OpenSearch document out.

    Called by both delivery paths — the per-event Lambda handler and the Firehose
    transform — and deliberately so. A second shaping path would be a second way
    around the coercers, and an index's type inference is a one-way door.
    """
    config = DEFAULT_CONFIG if config is None else config
    detail = event.detail or {}

    # `or`, not `.get(k, default)`: an empty string is as useless as a missing key
    # for every one of these, and the copies this replaces were inconsistent about
    # which of the two they used per field.
    core = {field: detail.get(field) or config["defaults"][field] for field in CORE_FIELDS}

    promoted = {
        entry["as"]: COERCERS[entry["type"]](detail.get(entry["from"]))
        for entry in config["promote"]
    }
    nested = {
        name: {key: core[ref] for key, ref in spec.items()}
        for name, spec in config["nested"].items()
    }

    return AppEventModel(
        source=event.source,
        event=event.detail_type,
        payload=detail,  # the entire detail object as payload
        **core,
        **nested,
        **promoted,
    )
