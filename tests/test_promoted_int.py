"""A promoted `int` reaches OpenSearch as a top-level integer, or not at all.

The example is a page count, `num_pages`, promoted by the
`tests/fixtures/shaping/promote-and-nest.json` field map. The field is not
declared in Python; it is a `promote` entry in config, and that is the behaviour
under test.

What is being protected is narrower than it looks. `payload` is serialized to a
JSON string, so the *only* aggregatable page count is the promoted top-level
field, and an index with no explicit mapping fixes each field's type from the
first non-null value it sees — permanently, for every writer of that index. A
stringified count landing first would make `sum` fail forever and, because
`save_event` swallows indexing errors, would do it silently while dropping whole
documents. Hence the emphasis below on *type*, not just presence.
"""
import json

import pytest
from conftest import shaping_config

from src.shaping import _as_int, load_config, shape

PROMOTING = load_config(shaping_config("promote-and-nest"))


class _Event:
    """The three attributes `shape` reads.

    A stand-in for `EventBridgeEvent` rather than the real class: `shape` only
    touches `.source`, `.detail_type` and `.detail`, and constructing the
    powertools wrapper would mean carrying a full EventBridge envelope for no
    added confidence.
    """

    def __init__(self, detail, source="acme.documents", detail_type="DocumentScanned"):
        self.detail = detail
        self.source = source
        self.detail_type = detail_type


def _detail(**extra):
    """A detail with the keys the bus always stamps, plus whatever the test is about."""
    return {
        "environment": "dev",
        "application": "acme-documents",
        "organization": "acme-org",
        "username": "jdoe",
        **extra,
    }


def _entity(**extra):
    return shape(_Event(_detail(**extra)), PROMOTING)


class TestPromotion:
    def test_a_top_level_count_is_promoted_off_the_detail(self):
        assert _entity(num_pages=12).num_pages == 12

    def test_it_is_an_int_on_the_document_not_a_string(self):
        """The whole point. `sum` needs a number; the mapping is decided by the first one."""
        written = _entity(num_pages=12).to_dict()
        assert written["num_pages"] == 12
        assert isinstance(written["num_pages"], int)

    def test_an_event_with_no_pages_reports_none(self):
        """Null creates no mapping and is ignored by `sum`, so absence stays absence."""
        assert _entity().num_pages is None

    def test_a_nested_count_is_not_promoted(self):
        """Only top-level detail keys are promoted.

        A nested one, such as `processing_results.num_pages`, is invisible to the
        aggregation — it survives solely inside the `payload` string. If this ever
        starts passing, an emitter has been instrumented in a place the chart
        cannot read.
        """
        assert _entity(processing_results={"num_pages": 12}).num_pages is None

    def test_the_count_still_travels_inside_the_payload(self):
        """Promotion copies, it does not move — the payload stays a faithful record."""
        assert _entity(num_pages=12).payload["num_pages"] == 12

    def test_a_map_without_the_promotion_gets_no_such_field(self):
        """The other half of config-driven shaping: a map that omits it adds nothing."""
        nest_only = load_config(shaping_config("nest-only"))
        assert "num_pages" not in shape(_Event(_detail(num_pages=12)), nest_only).to_dict()


class TestCoercion:
    """Anything that is not cleanly an integer must be dropped, not passed through."""

    @pytest.mark.parametrize("value, expected", [
        (12, 12),
        ("12", 12),        # `json.dumps(..., default=str)` stringifies a Decimal in transit
        (12.0, 12),
        ("  12  ", 12),
        (0, 0),            # a real zero-page reading is not the same as no reading
    ])
    def test_integral_values_are_kept(self, value, expected):
        assert _as_int(value) == expected

    @pytest.mark.parametrize("value", [
        None,
        "",
        "twelve",
        "12 pages",
        12.5,              # half a page is a bug in the emitter, not a page count
        float("nan"),
        float("inf"),
        True,              # bool is an int subclass in Python; "True pages" is meaningless
        False,
        [12],
        {"pages": 12},
        object(),
    ])
    def test_everything_else_becomes_none(self, value):
        assert _as_int(value) is None

    def test_a_bad_value_on_a_real_event_does_not_reach_the_document(self):
        assert _entity(num_pages="twelve").num_pages is None


class TestEnvelopeUnchanged:
    """The field is additive: nothing that already worked may shift."""

    def test_the_existing_top_level_fields_are_untouched(self):
        entity = _entity(num_pages=3)
        assert entity.organization == "acme-org"
        assert entity.application == "acme-documents"
        assert entity.environment == "dev"
        assert entity.username == "jdoe"
        assert entity.source == "acme.documents"
        assert entity.event == "DocumentScanned"

    def test_payload_is_still_a_json_string_once_serialized(self):
        """The constraint that forced a top-level field in the first place.

        A dict in memory, one opaque string on the document — which is exactly why
        a page count nested inside it can never be summed.
        """
        entity = _entity(num_pages=3)
        assert isinstance(entity.payload, dict)
        written = entity.to_dict()
        assert isinstance(written["payload"], str)
        assert json.loads(written["payload"])["num_pages"] == 3
