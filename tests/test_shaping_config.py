"""Each supported field-map shape produces exactly the document it describes.

The example maps in `tests/fixtures/shaping/` cover every shape the interpreter
supports: an `int` promotion with a nested user-context object, the nested object
alone, and a deployment-specific `application` fallback. If one of these fails,
a deployment relying on that shape would index a different document than its
config says — and a document shape decides an index mapping permanently.

Compared as parsed dicts, not bytes: promoted fields are pydantic extras and so
land after the declared ones, which changes key *order* in the JSON and nothing
else. OpenSearch does not care, and neither should this.
"""
import json

import pytest
from conftest import shaping_config

from src.shaping import CORE_FIELDS, ShapingConfigError, load_config, shape


class _Event:
    def __init__(self, detail, source="acme.documents", detail_type="DocumentProcessed"):
        self.detail = detail
        self.source = source
        self.detail_type = detail_type


DETAIL = {
    "environment": "prod",
    "application": "acme-documents",
    "organization": "acme-org",
    "username": "jdoe",
    "num_pages": 7,
    "extra": {"nested": True},
}


def _document(example, detail=None):
    doc = shape(_Event(detail if detail is not None else DETAIL), load_config(shaping_config(example))).to_dict()
    doc.pop("timestamp")  # generated, asserted separately
    return doc


BASE = {
    "event_id": None,
    "source": "acme.documents",
    "event": "DocumentProcessed",
    "payload": json.dumps(DETAIL, ensure_ascii=False),
    "environment": "prod",
    "application": "acme-documents",
    "organization": "acme-org",
    "username": "jdoe",
}
USER_CONTEXT = {"organization_user_context": {"username": "jdoe", "organization": "acme-org"}}


class TestEachShapeProducesItsDocument:
    def test_promote_and_nest(self):
        assert _document("promote-and-nest") == {**BASE, **USER_CONTEXT, "num_pages": 7}

    def test_nest_only(self):
        """Identical to promote-and-nest minus the promotion."""
        assert _document("nest-only") == {**BASE, **USER_CONTEXT}

    def test_application_fallback(self):
        """No nested user context, and no promoted fields."""
        assert _document("application-fallback") == BASE

    def test_application_falls_back_to_the_configured_default(self):
        detail = {k: v for k, v in DETAIL.items() if k != "application"}
        assert _document("application-fallback", detail)["application"] == "acme"

    def test_application_fallback_is_unknown_by_default(self):
        detail = {k: v for k, v in DETAIL.items() if k != "application"}
        assert _document("promote-and-nest", detail)["application"] == "unknown"

    def test_no_config_is_the_generic_shape(self):
        """No ShapingConfig at all: the four core fields, nothing promoted or nested."""
        doc = shape(_Event(DETAIL), load_config(None)).to_dict()
        doc.pop("timestamp")
        assert doc == BASE

    def test_timestamp_is_iso_utc(self):
        doc = shape(_Event(DETAIL), load_config(shaping_config("application-fallback"))).to_dict()
        assert doc["timestamp"].endswith("Z")


class TestDefaults:
    @pytest.mark.parametrize("field", CORE_FIELDS)
    def test_a_missing_core_field_falls_back(self, field):
        detail = {k: v for k, v in DETAIL.items() if k != field}
        assert _document("promote-and-nest", detail)[field] == load_config(
            shaping_config("promote-and-nest")
        )["defaults"][field]

    @pytest.mark.parametrize("field", CORE_FIELDS)
    def test_an_empty_core_field_also_falls_back(self, field):
        """`or`, not `.get(k, default)`.

        One rule for all four core fields: an explicitly empty value is treated
        exactly like a missing one, rather than landing in the document as "".
        """
        assert _document("promote-and-nest", {**DETAIL, field: ""})[field] != ""


class TestConfigValidationIsFatal:
    """A config that cannot be honoured exactly must fail the cold start.

    Not defensive programming — the alternative is a silently different document,
    and a document shape decides an index mapping permanently.
    """

    @pytest.mark.parametrize("raw, fragment", [
        ("{not json", "not valid JSON"),
        ('["a"]', "must be a JSON object"),
        ('{"defualts": {}}', "unknown key"),
        ('{"defaults": {"nope": "x"}}', "may only set"),
        ('{"defaults": {"application": 7}}', "must be a string"),
        ('{"promote": {}}', "must be a list"),
        ('{"promote": [{"as": "x"}]}', "from is required"),
        ('{"promote": [{"from": "a", "type": "float"}]}', "not one of"),
        ('{"promote": [{"from": "a", "as": "payload"}]}', "collides with a built-in"),
        ('{"promote": [{"from": "a", "as": "organization"}]}', "collides with a built-in"),
        ('{"promote": [{"from": "a", "as": "x"}, {"from": "b", "as": "x"}]}', "more than once"),
        ('{"nested": {"ctx": {"u": "not_a_core_field"}}}', "must name one of"),
        ('{"nested": {"ctx": {}}}', "non-empty object"),
    ])
    def test_rejected(self, raw, fragment):
        with pytest.raises(ShapingConfigError) as exc:
            load_config(raw)
        assert fragment in str(exc.value)

    def test_absent_config_is_the_generic_shape_not_an_error(self):
        """A deployment that sets nothing gets the four core fields and nothing else."""
        assert load_config(None)["promote"] == []
        assert load_config("  ")["nested"] == {}

    def test_every_example_map_is_valid(self):
        for example in ("promote-and-nest", "nest-only", "application-fallback"):
            load_config(shaping_config(example))
