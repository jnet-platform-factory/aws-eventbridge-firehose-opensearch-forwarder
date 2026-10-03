"""The search API: query shapes, hydration, and the routes.

These need the delivery dependencies, because the API reads OpenSearch. The
default test lane deliberately does not install them -- that is what proves the
shaping seam -- so everything here skips there and runs in the full lane. See
tests/requirements.txt and tests/requirements-full.txt.
"""
import json
from importlib.util import find_spec

import pytest

pytestmark = pytest.mark.skipif(
    find_spec("opensearchpy") is None,
    reason="search API needs the delivery dependencies; run the full lane",
)


class FakeClient:
    """Records the body it was asked to search, returns what it was told to."""

    def __init__(self, response=None, get_response=None, raises=False):
        self.response = response or {"hits": {"hits": [], "total": {"value": 0}}}
        self.get_response = get_response
        self.raises = raises
        self.last_body = None
        self.last_index = None

    def search(self, index=None, body=None):
        self.last_index, self.last_body = index, body
        return self.response

    def get(self, index=None, id=None):
        if self.raises:
            raise RuntimeError("not found")
        return self.get_response


def _repo(client):
    from src.repositories import OpenSearchAppEventsRepository
    repo = OpenSearchAppEventsRepository.model_construct(
        endpoint="example.com", region="us-east-1", index="idx", service="es"
    )
    object.__setattr__(repo, "_client", client)
    return repo


class TestSearchQuery:
    def test_no_filters_is_match_all(self):
        c = FakeClient(); _repo(c).search()
        assert c.last_body["query"] == {"match_all": {}}

    def test_filters_use_keyword_subfields(self):
        """An analysed `source` would match 'billing' against 'billing.invoice'."""
        c = FakeClient()
        _repo(c).search(source="billing.invoice", event="InvoiceIssued", organization="acme")
        terms = {list(f["term"])[0] for f in c.last_body["query"]["bool"]["filter"]}
        assert terms == {"source.keyword", "event.keyword", "organization.keyword"}

    def test_free_text_searches_the_payload(self):
        c = FakeClient(); _repo(c).search(text="INV-9")
        assert "payload" in c.last_body["query"]["bool"]["must"][0]["multi_match"]["fields"]

    def test_date_bounds_become_one_range(self):
        c = FakeClient(); _repo(c).search(date_from="2026-01-01", date_to="2026-02-01")
        rng = c.last_body["query"]["bool"]["filter"][0]["range"]["timestamp"]
        assert rng == {"gte": "2026-01-01", "lte": "2026-02-01"}

    def test_newest_first_and_paged(self):
        c = FakeClient(); _repo(c).search(size=10, offset=20)
        assert c.last_body["sort"] == [{"timestamp": {"order": "desc"}}]
        assert (c.last_body["from"], c.last_body["size"]) == (20, 10)

    def test_total_is_tracked(self):
        """Without track_total_hits the total silently caps at 10000."""
        c = FakeClient(); _repo(c).search()
        assert c.last_body["track_total_hits"] is True


class TestHydration:
    def test_payload_comes_back_as_an_object(self):
        """It is stored as a JSON string; a client wants JSON, not a string of it."""
        c = FakeClient({"hits": {"total": {"value": 1}, "hits": [
            {"_id": "abc", "_source": {"source": "s", "payload": '{"a": 1}'}}]}})
        out = _repo(c).search()
        assert out["items"][0]["payload"] == {"a": 1}
        assert out["total"] == 1

    def test_unparseable_payload_is_surfaced_not_dropped(self):
        c = FakeClient({"hits": {"total": {"value": 1}, "hits": [
            {"_id": "abc", "_source": {"payload": "not json"}}]}})
        assert _repo(c).search()["items"][0]["payload"] == {"_unparsed": "not json"}

    def test_document_id_fills_in_a_missing_event_id(self):
        c = FakeClient({"hits": {"total": {"value": 1}, "hits": [
            {"_id": "doc-1", "_source": {"source": "s"}}]}})
        assert _repo(c).search()["items"][0]["event_id"] == "doc-1"

    def test_an_existing_event_id_wins(self):
        """Under Firehose it holds the EventBridge id, which is more useful."""
        c = FakeClient({"hits": {"total": {"value": 1}, "hits": [
            {"_id": "doc-1", "_source": {"event_id": "eb-9"}}]}})
        assert _repo(c).search()["items"][0]["event_id"] == "eb-9"


class TestFacets:
    def test_composite_aggregation_over_both_fields(self):
        c = FakeClient({"aggregations": {"pairs": {"buckets": [
            {"key": {"source": "billing.invoice", "event": "InvoiceIssued"}, "doc_count": 42}]}}})
        out = _repo(c).facets()
        assert out == [{"source": "billing.invoice", "detail_type": "InvoiceIssued", "count": 42}]
        assert list(c.last_body["aggs"]["pairs"]["composite"]["sources"][0]) == ["source"]

    def test_no_aggregations_is_empty_not_an_error(self):
        assert _repo(FakeClient({})).facets() == []


class TestGet:
    def test_found(self):
        c = FakeClient(get_response={"found": True, "_id": "x", "_source": {"payload": '{"a":1}'}})
        assert _repo(c).get("x")["payload"] == {"a": 1}

    def test_not_found_is_none(self):
        assert _repo(FakeClient(get_response={"found": False})).get("x") is None

    def test_a_client_error_is_none_not_a_crash(self):
        """A miss must not 500 the API."""
        assert _repo(FakeClient(raises=True)).get("x") is None
