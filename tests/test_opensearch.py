"""Unit tests for OpenSearch aggregator and parser."""

from unittest.mock import MagicMock, patch

import pytest

from mailtrace.aggregator.opensearch import OpenSearch
from mailtrace.config import (
    Config,
    Method,
    OpenSearchConfig,
    OpenSearchMappingConfig,
    SSHConfig,
)
from mailtrace.models import LogEntry, LogQuery
from mailtrace.parser import OpensearchParser


@pytest.fixture
def default_mapping():
    """Default OpenSearch field mapping configuration."""
    return OpenSearchMappingConfig(
        facility="log.syslog.facility.name",
        hostname="host.name",
        message="message",
        timestamp="@timestamp",
        service="log.syslog.appname",
        queueid="log.syslog.structured_data.queueid",
        queued_as="log.syslog.structured_data.queued_as",
        message_id="postfix.message_id",
    )


@pytest.fixture
def opensearch_config(default_mapping):
    """OpenSearch configuration for testing."""
    return OpenSearchConfig(
        host="localhost",
        port=9200,
        username="admin",
        password="admin",
        use_ssl=True,
        verify_certs=False,
        index="mailtrace-logs",
        time_zone="+08:00",
        timeout=10,
        mapping=default_mapping,
    )


@pytest.fixture
def config(opensearch_config):
    """Full config object for testing."""
    return Config(
        method=Method.OPENSEARCH,
        log_level="INFO",
        ssh_config=SSHConfig(username="dummy", password="dummy"),
        opensearch_config=opensearch_config,
        clusters={
            "mx-cluster": ["mx1", "mx2"],
            "relay-cluster": ["relay1"],
        },
        domain="example.com",
    )


@pytest.fixture
def mock_opensearch_hit():
    """Sample OpenSearch hit document."""
    hit_data = {
        "@timestamp": "2025-01-15T10:00:00.123Z",
        "host": {"name": "mx1.example.com"},
        "log": {
            "syslog": {
                "facility": {"name": "mail"},
                "appname": "postfix/smtp",
                "structured_data": {
                    "queueid": "A2DE917F931",
                    "queued_as": "B3EF928G042",
                },
            }
        },
        "message": "A2DE917F931: to=<user@example.com>, relay=mail.example.com[192.168.1.1]:25, status=sent (250 2.0.0 OK: queued as B3EF928G042)",
        "postfix": {"message_id": "abc123@example.com"},
    }
    return {"_source": hit_data, **hit_data}


@pytest.fixture
def mock_search():
    """Create a mock Search object with common setup."""
    mock = MagicMock()
    mock.extra.return_value = mock
    mock.query.return_value = mock
    mock.filter.return_value = mock
    mock.execute.return_value = []
    return mock


class TestOpensearchParser:
    """Tests for the OpensearchParser class."""

    def test_parse_basic_fields(self, default_mapping):
        """Parser extracts basic fields correctly."""
        parser = OpensearchParser(mapping=default_mapping)
        log = {
            "@timestamp": "2025-01-15T10:00:00.123Z",
            "host": {"name": "mx1.example.com"},
            "log": {
                "syslog": {
                    "appname": "postfix/smtp",
                    "structured_data": {"queueid": "ABC123"},
                }
            },
            "message": "ABC123: from=<sender@example.com>",
        }

        entry = parser.parse(log)

        assert entry.datetime == "2025-01-15T10:00:00.123Z"
        assert entry.hostname == "mx1.example.com"
        assert entry.service == "postfix/smtp"
        assert entry.mail_id == "ABC123"

    def test_parse_mail_id_from_queueid_field(self, default_mapping):
        """Parser extracts mail_id from structured queueid field."""
        parser = OpensearchParser(mapping=default_mapping)
        log = {
            "@timestamp": "2025-01-15T10:00:00Z",
            "host": {"name": "mx1"},
            "log": {
                "syslog": {
                    "appname": "postfix/qmgr",
                    "structured_data": {"queueid": "DEADBEEF123"},
                }
            },
            "message": "DEADBEEF123: from=<test@test.com>",
        }

        entry = parser.parse(log)
        assert entry.mail_id == "DEADBEEF123"

    def test_parse_mail_id_from_message_fallback(self):
        """Parser extracts mail_id from message when queueid field missing."""
        mapping = OpenSearchMappingConfig(
            hostname="host.name",
            message="message",
            timestamp="@timestamp",
            service="appname",
            queueid=None,
        )
        parser = OpensearchParser(mapping=mapping)
        log = {
            "@timestamp": "2025-01-15T10:00:00Z",
            "host": {"name": "mx1"},
            "appname": "postfix/smtp",
            "message": "XYZ789: to=<user@example.com>",
        }

        entry = parser.parse(log)
        assert entry.mail_id == "XYZ789"

    def test_parse_invalid_mail_id_returns_none(self):
        """Parser returns None for invalid mail_id format."""
        mapping = OpenSearchMappingConfig(
            hostname="host.name",
            message="message",
            timestamp="@timestamp",
            service="appname",
            queueid=None,
        )
        parser = OpensearchParser(mapping=mapping)
        log = {
            "@timestamp": "2025-01-15T10:00:00Z",
            "host": {"name": "mx1"},
            "appname": "postfix/smtp",
            "message": "invalid-id: some log message",
        }

        entry = parser.parse(log)
        assert entry.mail_id is None

    def test_parse_queued_as_field(self, default_mapping):
        """Parser extracts queued_as from structured field."""
        parser = OpensearchParser(mapping=default_mapping)
        log = {
            "@timestamp": "2025-01-15T10:00:00Z",
            "host": {"name": "mx1"},
            "log": {
                "syslog": {
                    "appname": "postfix/smtp",
                    "structured_data": {
                        "queueid": "ABC123",
                        "queued_as": "DEF456",
                    },
                }
            },
            "message": "ABC123: status=sent",
        }

        entry = parser.parse(log)
        assert entry.queued_as == "DEF456"

    def test_parse_message_id_field(self, default_mapping):
        """Parser extracts message_id from configured field."""
        parser = OpensearchParser(mapping=default_mapping)
        log = {
            "@timestamp": "2025-01-15T10:00:00Z",
            "host": {"name": "mx1"},
            "log": {
                "syslog": {
                    "appname": "postfix/cleanup",
                    "structured_data": {"queueid": "ABC123"},
                }
            },
            "message": "ABC123: message-id=<unique@example.com>",
            "postfix": {"message_id": "unique@example.com"},
        }

        entry = parser.parse(log)
        assert entry.message_id == "unique@example.com"

    def test_parse_strips_mail_id_prefix_from_message(self, default_mapping):
        """Parser strips mail_id prefix from message content."""
        parser = OpensearchParser(mapping=default_mapping)
        log = {
            "@timestamp": "2025-01-15T10:00:00Z",
            "host": {"name": "mx1"},
            "log": {
                "syslog": {
                    "appname": "postfix/smtp",
                    "structured_data": {"queueid": "ABC123"},
                }
            },
            "message": "ABC123: from=<sender@example.com>, size=1234",
        }

        entry = parser.parse(log)
        assert entry.message == "from=<sender@example.com>, size=1234"

    def test_parse_with_enrichment_extracts_relay_info(self, default_mapping):
        """Parser enrichment extracts relay info from message."""
        parser = OpensearchParser(mapping=default_mapping)
        log = {
            "@timestamp": "2025-01-15T10:00:00Z",
            "host": {"name": "mx1"},
            "log": {
                "syslog": {
                    "appname": "postfix/smtp",
                    "structured_data": {"queueid": "ABC123"},
                }
            },
            "message": "ABC123: to=<user@example.com>, relay=mail.example.com[192.168.1.1]:25, status=sent (250 2.0.0 OK: queued as XYZ789)",
        }

        entry = parser.parse_with_enrichment(log)

        assert entry.relay_host == "mail.example.com"
        assert entry.relay_ip == "192.168.1.1"
        assert entry.relay_port == 25
        assert entry.smtp_code == 250
        assert entry.queued_as == "XYZ789"

    def test_parse_with_enrichment_extracts_message_id_from_text(self):
        """Parser enrichment extracts message_id from message text."""
        mapping = OpenSearchMappingConfig(
            hostname="host.name",
            message="message",
            timestamp="@timestamp",
            service="appname",
            queueid="queueid",
            message_id=None,
        )
        parser = OpensearchParser(mapping=mapping)
        log = {
            "@timestamp": "2025-01-15T10:00:00Z",
            "host": {"name": "mx1"},
            "appname": "postfix/cleanup",
            "queueid": "ABC123",
            "message": "ABC123: message-id=<test-msg-id@example.com>",
        }

        entry = parser.parse_with_enrichment(log)
        assert entry.message_id == "test-msg-id@example.com"

    def test_parse_nested_field_access(self, default_mapping):
        """Parser handles nested field paths correctly."""
        parser = OpensearchParser(mapping=default_mapping)
        log = {
            "@timestamp": "2025-01-15T10:00:00Z",
            "host": {"name": "deeply.nested.hostname"},
            "log": {
                "syslog": {
                    "appname": "postfix/qmgr",
                    "structured_data": {"queueid": "NESTED123"},
                }
            },
            "message": "NESTED123: test",
        }

        entry = parser.parse(log)
        assert entry.hostname == "deeply.nested.hostname"
        assert entry.mail_id == "NESTED123"

    def test_parse_missing_fields_returns_none(self):
        """Parser returns None for missing optional fields."""
        mapping = OpenSearchMappingConfig(
            hostname="host.name",
            message="message",
            timestamp="@timestamp",
            service="appname",
        )
        parser = OpensearchParser(mapping=mapping)
        log = {
            "@timestamp": "2025-01-15T10:00:00Z",
            "message": "some message without structure",
        }

        entry = parser.parse(log)
        assert entry.hostname is None
        assert entry.service is None
        assert entry.mail_id is None

    def test_parse_empty_message(self):
        """Parser handles empty message field."""
        mapping = OpenSearchMappingConfig(
            hostname="host.name",
            message="message",
            timestamp="@timestamp",
            service="appname",
        )
        parser = OpensearchParser(mapping=mapping)
        log = {
            "@timestamp": "2025-01-15T10:00:00Z",
            "host": {"name": "mx1"},
            "appname": "postfix/smtp",
            "message": "",
        }

        entry = parser.parse(log)
        assert entry.message == ""
        assert entry.mail_id is None


class TestOpenSearchAggregator:
    """Tests for the OpenSearch aggregator class."""

    @patch("mailtrace.aggregator.opensearch.OpenSearchClient")
    def test_init_creates_client_with_correct_config(self, mock_client_class, config):
        """Aggregator initializes OpenSearch client with correct parameters."""
        OpenSearch(host="mx-cluster", config=config)

        mock_client_class.assert_called_once_with(
            hosts=[{"host": "localhost", "port": 9200}],
            http_auth=("admin", "admin"),
            use_ssl=True,
            verify_certs=False,
            timeout=10,
        )

    @patch("mailtrace.aggregator.opensearch.OpenSearchClient")
    def test_init_expands_cluster_to_hosts(self, mock_client_class, config):
        """Aggregator expands cluster name to list of hosts."""
        aggregator = OpenSearch(host="mx-cluster", config=config)

        assert "mx1" in aggregator.hosts
        assert "mx2" in aggregator.hosts
        assert "mx1.example.com" in aggregator.hosts
        assert "mx2.example.com" in aggregator.hosts

    @patch("mailtrace.aggregator.opensearch.OpenSearchClient")
    def test_init_single_host(self, mock_client_class, config):
        """Aggregator handles single hostname (not a cluster)."""
        aggregator = OpenSearch(host="standalone-host", config=config)

        assert "standalone-host" in aggregator.hosts
        assert "standalone-host.example.com" in aggregator.hosts

    @patch("mailtrace.aggregator.opensearch.OpenSearchClient")
    @patch("mailtrace.aggregator.opensearch.Search")
    def test_query_by_keywords(
        self, mock_search_class, mock_client_class, config, mock_search
    ):
        """Aggregator builds correct query for keyword search."""
        mock_search_class.return_value = mock_search
        aggregator = OpenSearch(host="mx1", config=config)

        query = LogQuery(keywords=["user@example.com"])
        aggregator.query_by(query)

        calls = mock_search.query.call_args_list
        keyword_call = [c for c in calls if "match_phrase" in str(c)]
        assert len(keyword_call) > 0

    @patch("mailtrace.aggregator.opensearch.OpenSearchClient")
    @patch("mailtrace.aggregator.opensearch.Search")
    def test_query_by_mail_id_with_queueid_field(
        self, mock_search_class, mock_client_class, config, mock_search
    ):
        """Aggregator uses term query on queueid field for mail_id search."""
        mock_search_class.return_value = mock_search
        aggregator = OpenSearch(host="mx1", config=config)

        query = LogQuery(mail_id="ABC123DEF")
        aggregator.query_by(query)

        calls = mock_search.query.call_args_list
        term_calls = [c for c in calls if "term" in str(c)]
        assert len(term_calls) > 0

    @patch("mailtrace.aggregator.opensearch.OpenSearchClient")
    @patch("mailtrace.aggregator.opensearch.Search")
    def test_query_by_mail_id_fallback_to_wildcard(
        self, mock_search_class, mock_client_class, config, mock_search
    ):
        """Aggregator uses wildcard query when queueid field not configured."""
        config.opensearch_config.mapping.queueid = None
        mock_search_class.return_value = mock_search
        aggregator = OpenSearch(host="mx1", config=config)

        query = LogQuery(mail_id="ABC123DEF")
        aggregator.query_by(query)

        calls = mock_search.query.call_args_list
        wildcard_calls = [c for c in calls if "wildcard" in str(c)]
        assert len(wildcard_calls) > 0

    @patch("mailtrace.aggregator.opensearch.OpenSearchClient")
    @patch("mailtrace.aggregator.opensearch.Search")
    def test_query_by_message_id_with_field(
        self, mock_search_class, mock_client_class, config, mock_search
    ):
        """Aggregator uses term query on message_id field."""
        mock_search_class.return_value = mock_search
        aggregator = OpenSearch(host="mx1", config=config)

        query = LogQuery(message_id="unique-msg@example.com")
        aggregator.query_by(query)

        calls = mock_search.query.call_args_list
        term_calls = [c for c in calls if "term" in str(c)]
        assert len(term_calls) > 0

    @patch("mailtrace.aggregator.opensearch.OpenSearchClient")
    @patch("mailtrace.aggregator.opensearch.Search")
    def test_query_by_message_id_fallback_to_match_phrase(
        self, mock_search_class, mock_client_class, config, mock_search
    ):
        """Aggregator uses match_phrase when message_id field not configured."""
        config.opensearch_config.mapping.message_id = None
        mock_search_class.return_value = mock_search
        aggregator = OpenSearch(host="mx1", config=config)

        query = LogQuery(message_id="unique-msg@example.com")
        aggregator.query_by(query)

        calls = mock_search.query.call_args_list
        match_phrase_calls = [c for c in calls if "match_phrase" in str(c)]
        assert len(match_phrase_calls) > 0

    @patch("mailtrace.aggregator.opensearch.OpenSearchClient")
    @patch("mailtrace.aggregator.opensearch.Search")
    def test_query_by_time_range(
        self, mock_search_class, mock_client_class, config, mock_search
    ):
        """Aggregator builds correct time range filter."""
        mock_search_class.return_value = mock_search
        aggregator = OpenSearch(host="mx1", config=config)

        query = LogQuery(
            keywords=["test"],
            time="2025-01-15T10:00:00",
            time_range="1h",
        )
        aggregator.query_by(query)

        calls = mock_search.filter.call_args_list
        assert len(calls) > 0
        assert "range" in str(calls[0])

    @patch("mailtrace.aggregator.opensearch.OpenSearchClient")
    @patch("mailtrace.aggregator.opensearch.Search")
    def test_query_skips_hostname_filter_for_message_id(
        self, mock_search_class, mock_client_class, config, mock_search
    ):
        """Aggregator skips hostname filter for message_id queries."""
        mock_search_class.return_value = mock_search
        aggregator = OpenSearch(host="mx1", config=config)

        query = LogQuery(message_id="unique-msg@example.com")
        aggregator.query_by(query)

        calls = mock_search.query.call_args_list
        hostname_calls = [c for c in calls if "terms" in str(c) and "host" in str(c)]
        assert len(hostname_calls) == 0

    @patch("mailtrace.aggregator.opensearch.OpenSearchClient")
    @patch("mailtrace.aggregator.opensearch.Search")
    def test_query_skips_hostname_filter_for_cross_host_mail_id(
        self, mock_search_class, mock_client_class, config, mock_search
    ):
        """Aggregator skips hostname filter for mail_id when no hosts configured."""
        mock_search_class.return_value = mock_search
        aggregator = OpenSearch(host="unknown-cluster", config=config)
        aggregator.hosts = []

        query = LogQuery(mail_id="ABC123")
        aggregator.query_by(query)

        calls = mock_search.query.call_args_list
        hostname_terms_calls = [
            c for c in calls if "terms" in str(c) and "host.name" in str(c)
        ]
        assert len(hostname_terms_calls) == 0

    @patch("mailtrace.aggregator.opensearch.OpenSearchClient")
    @patch("mailtrace.aggregator.opensearch.Search")
    def test_query_includes_hostname_filter_for_keyword_search(
        self, mock_search_class, mock_client_class, config, mock_search
    ):
        """Aggregator includes hostname filter for keyword searches."""
        mock_search_class.return_value = mock_search
        aggregator = OpenSearch(host="mx1", config=config)

        query = LogQuery(keywords=["test@example.com"])
        aggregator.query_by(query)

        calls = mock_search.query.call_args_list
        hostname_terms_calls = [c for c in calls if "terms" in str(c)]
        assert len(hostname_terms_calls) > 0

    @patch("mailtrace.aggregator.opensearch.OpenSearchClient")
    @patch("mailtrace.aggregator.opensearch.Search")
    def test_query_includes_facility_filter(
        self, mock_search_class, mock_client_class, config, mock_search
    ):
        """Aggregator includes mail facility filter."""
        mock_search_class.return_value = mock_search
        aggregator = OpenSearch(host="mx1", config=config)

        query = LogQuery(keywords=["test"])
        aggregator.query_by(query)

        calls = mock_search.query.call_args_list
        facility_calls = [c for c in calls if "match" in str(c)]
        assert len(facility_calls) > 0

    @patch("mailtrace.aggregator.opensearch.OpenSearchClient")
    @patch("mailtrace.aggregator.opensearch.Search")
    def test_query_returns_parsed_log_entries(
        self,
        mock_search_class,
        mock_client_class,
        config,
        mock_opensearch_hit,
        mock_search,
    ):
        """Aggregator returns correctly parsed LogEntry objects."""
        mock_hit = MagicMock()
        mock_hit.to_dict.return_value = mock_opensearch_hit
        mock_response = MagicMock()
        mock_response.__iter__ = lambda self: iter([mock_hit])
        mock_search.execute.return_value = mock_response
        mock_search_class.return_value = mock_search

        aggregator = OpenSearch(host="mx1", config=config)
        query = LogQuery(keywords=["test"])
        results = aggregator.query_by(query)

        assert len(results) == 1
        assert isinstance(results[0], LogEntry)

    @patch("mailtrace.aggregator.opensearch.OpenSearchClient")
    @patch("mailtrace.aggregator.opensearch.Search")
    def test_query_sets_result_size_limit(
        self, mock_search_class, mock_client_class, config, mock_search
    ):
        """Aggregator sets result size limit to 1000."""
        mock_search_class.return_value = mock_search
        aggregator = OpenSearch(host="mx1", config=config)

        query = LogQuery(keywords=["test"])
        aggregator.query_by(query)

        mock_search.extra.assert_called_once_with(size=1000)

    @patch("mailtrace.aggregator.opensearch.OpenSearchClient")
    @patch("mailtrace.aggregator.opensearch.Search")
    def test_query_empty_results(
        self, mock_search_class, mock_client_class, config, mock_search
    ):
        """Aggregator returns empty list when no results found."""
        mock_search_class.return_value = mock_search
        aggregator = OpenSearch(host="mx1", config=config)

        query = LogQuery(keywords=["nonexistent"])
        results = aggregator.query_by(query)

        assert results == []


class TestTimeRangeCalculation:
    """Tests for time range calculation in queries."""

    @pytest.mark.parametrize(
        "time_range,description",
        [
            ("2h", "hours"),
            ("1d", "days"),
            ("30m", "minutes"),
        ],
    )
    @patch("mailtrace.aggregator.opensearch.OpenSearchClient")
    @patch("mailtrace.aggregator.opensearch.Search")
    def test_time_range_formats(
        self,
        mock_search_class,
        mock_client_class,
        config,
        mock_search,
        time_range,
        description,
    ):
        """Time range in various formats is calculated correctly."""
        mock_search_class.return_value = mock_search
        aggregator = OpenSearch(host="mx1", config=config)

        query = LogQuery(
            keywords=["test"],
            time="2025-01-15T12:00:00",
            time_range=time_range,
        )
        aggregator.query_by(query)

        calls = mock_search.filter.call_args_list
        assert len(calls) == 1

    @patch("mailtrace.aggregator.opensearch.OpenSearchClient")
    @patch("mailtrace.aggregator.opensearch.Search")
    def test_time_with_utc_suffix(
        self, mock_search_class, mock_client_class, config, mock_search
    ):
        """Time with Z suffix is handled correctly."""
        mock_search_class.return_value = mock_search
        aggregator = OpenSearch(host="mx1", config=config)

        query = LogQuery(
            keywords=["test"],
            time="2025-01-15T12:00:00Z",
            time_range="1h",
        )
        aggregator.query_by(query)


class TestOpenSearchConfiguration:
    """Tests for OpenSearch configuration handling."""

    def test_mapping_config_defaults(self):
        """OpenSearchMappingConfig has correct defaults."""
        mapping = OpenSearchMappingConfig()

        # Required fields have sensible defaults
        assert mapping.hostname == "host.name"
        assert mapping.message == "message"
        assert mapping.timestamp == "@timestamp"
        # Optional fields default to None
        assert mapping.facility is None
        assert mapping.queueid is None

    def test_opensearch_config_converts_dict_mapping(self):
        """OpenSearchConfig converts dict mapping to config object."""
        config = OpenSearchConfig(
            host="localhost",
            port=9200,
            username="admin",
            password="admin",
            mapping={  # type: ignore[arg-type]
                "hostname": "custom.hostname",
                "message": "custom.message",
            },
        )

        assert isinstance(config.mapping, OpenSearchMappingConfig)
        assert config.mapping.hostname == "custom.hostname"
        assert config.mapping.message == "custom.message"

    def test_opensearch_config_default_values(self):
        """OpenSearchConfig has correct default values."""
        config = OpenSearchConfig()

        assert config.port == 9200
        assert config.use_ssl is False
        # SECURITY: verify_certs defaults to True for secure connections
        assert config.verify_certs is True
        assert config.time_zone == "+00:00"
        assert config.timeout == 10
