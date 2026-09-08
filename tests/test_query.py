import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

from mailtrace.config import OpenSearchMappingConfig, TracingConfig
from mailtrace.tracing.query import SCROLL_KEEPALIVE, query_all_logs


class FakeSearch:
    def __init__(self, **_: object) -> None:
        self.body: dict[str, object] = {}

    def extra(self, **kwargs: object) -> "FakeSearch":
        self.body.update(kwargs)
        return self

    def query(self, *_: object, **__: object) -> "FakeSearch":
        return self

    def filter(self, *_: object, **__: object) -> "FakeSearch":
        return self

    def sort(self, *_: object, **__: object) -> "FakeSearch":
        return self

    def to_dict(self) -> dict[str, object]:
        return self.body


class FakeScrollClient:
    def __init__(self, responses: list[dict[str, object]]) -> None:
        self.responses = responses
        self.search_calls: list[dict[str, object]] = []
        self.scroll_calls: list[dict[str, object]] = []
        self.clear_scroll_calls: list[dict[str, object]] = []

    def search(self, **kwargs: object) -> dict[str, object]:
        self.search_calls.append(kwargs)
        return self.responses.pop(0)

    def scroll(self, **kwargs: object) -> dict[str, object]:
        self.scroll_calls.append(kwargs)
        return self.responses.pop(0)

    def clear_scroll(self, **kwargs: object) -> None:
        self.clear_scroll_calls.append(kwargs)


def _hit(index: int) -> dict[str, object]:
    return {
        "_id": str(index),
        "_source": {
            "@timestamp": "2026-08-02T00:00:00Z",
            "host": {"name": "host"},
            "log": {"syslog": {"appname": "postfix/cleanup"}},
            "message": f"Q{index:05d}: message-id=<message-{index}@example.com>",
        },
    }


def _response(scroll_id: str, hits: list[dict[str, object]]) -> dict[str, object]:
    return {"_scroll_id": scroll_id, "hits": {"hits": hits}}


def _config(scroll_batch_size: int = 1000) -> SimpleNamespace:
    return SimpleNamespace(
        tracing=SimpleNamespace(scroll_batch_size=scroll_batch_size),
        opensearch_config=SimpleNamespace(
            host="opensearch",
            port=9200,
            username="",
            password="",
            use_ssl=False,
            verify_certs=False,
            timeout=10,
            index="*-mail",
            time_zone="+00:00",
            mapping=OpenSearchMappingConfig(),
        )
    )


class ScrollQueryTest(unittest.TestCase):
    def test_scroll_batch_size_must_be_positive(self) -> None:
        with self.assertRaises(ValueError):
            TracingConfig(scroll_batch_size=0)

    def test_query_all_logs_reads_more_than_ten_thousand_hits(self) -> None:
        hits = [_hit(index) for index in range(10001)]
        config = _config()
        batch_size = config.tracing.scroll_batch_size
        pages = [
            _response(f"scroll-{page}", hits[start:start + batch_size])
            for page, start in enumerate(range(0, len(hits), batch_size), 1)
        ]
        pages.append(_response("scroll-final", []))
        client = FakeScrollClient(pages)

        with patch("mailtrace.tracing.query.OSClient", return_value=client):
            with patch("mailtrace.tracing.query.Search", FakeSearch):
                logs = query_all_logs(
                    config,
                    datetime(2026, 8, 2),
                    datetime(2026, 8, 3),
                )

        self.assertEqual(len(logs), 10001)
        self.assertEqual(len(client.search_calls), 1)
        self.assertEqual(len(client.scroll_calls), 11)
        self.assertEqual(
            client.search_calls[0]["params"],
            {"scroll": SCROLL_KEEPALIVE},
        )
        self.assertEqual(
            client.search_calls[0]["body"]["size"],
            batch_size,
        )
        self.assertEqual(
            client.clear_scroll_calls,
            [{"body": {"scroll_id": ["scroll-final"]}}],
        )

    def test_query_all_logs_uses_configured_scroll_batch_size(self) -> None:
        batch_size = 2500
        hits = [_hit(index) for index in range(2501)]
        pages = [
            _response("scroll-1", hits[:batch_size]),
            _response("scroll-2", hits[batch_size:]),
            _response("scroll-final", []),
        ]
        client = FakeScrollClient(pages)

        with patch("mailtrace.tracing.query.OSClient", return_value=client):
            with patch("mailtrace.tracing.query.Search", FakeSearch):
                logs = query_all_logs(
                    _config(batch_size),
                    datetime(2026, 8, 2),
                    datetime(2026, 8, 3),
                )

        self.assertEqual(len(logs), len(hits))
        self.assertEqual(
            client.search_calls[0]["body"]["size"],
            batch_size,
        )

    def test_query_all_logs_clears_scroll_when_parsing_fails(self) -> None:
        client = FakeScrollClient(
            [_response("scroll-1", [_hit(1)])]
        )

        with patch("mailtrace.tracing.query.OSClient", return_value=client):
            with patch("mailtrace.tracing.query.Search", FakeSearch):
                with patch(
                    "mailtrace.tracing.query.OpensearchParser.parse_with_enrichment",
                    side_effect=RuntimeError("parse failed"),
                ):
                    logs = query_all_logs(
                        _config(),
                        datetime(2026, 8, 2),
                        datetime(2026, 8, 3),
                    )

        self.assertEqual(logs, [])
        self.assertEqual(
            client.clear_scroll_calls,
            [{"body": {"scroll_id": ["scroll-1"]}}],
        )


if __name__ == "__main__":
    unittest.main()
