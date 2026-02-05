"""Tests for mailtrace.trace module."""

from unittest.mock import MagicMock, patch

import pytest

from mailtrace.config import Config, Method, OpenSearchConfig, SSHConfig
from mailtrace.graph import MailGraph
from mailtrace.models import LogEntry
from mailtrace.trace import (
    _build_hostname_map,
    _normalize_host,
    _reconstruct_chain,
    query_logs_by_keywords,
    trace_mail_flow,
    trace_mail_flow_by_message_id,
)
from mailtrace.utils import RelayResult


@pytest.fixture
def opensearch_config():
    """Create an OpenSearch configuration."""
    return Config(
        method=Method.OPENSEARCH,
        log_level="INFO",
        ssh_config=SSHConfig(username="test", password="test"),
        opensearch_config=OpenSearchConfig(
            host="localhost",
            port=9200,
            username="admin",
            password="admin",
        ),
    )


@pytest.fixture
def ssh_config():
    """Create an SSH configuration."""
    return Config(
        method=Method.SSH,
        log_level="INFO",
        ssh_config=SSHConfig(username="test", password="test"),
        opensearch_config=OpenSearchConfig(),
        clusters={"test-cluster": ["host1", "host2"]},
    )


def make_log_entry(
    hostname="mx1.example.com",
    service="postfix/smtp",
    mail_id="ABC123",
    message="test",
):
    """Helper to create LogEntry instances."""
    return LogEntry(
        datetime="2025-01-15T10:00:00Z",
        hostname=hostname,
        service=service,
        mail_id=mail_id,
        message=message,
    )


@pytest.fixture
def sample_logs():
    """Create sample log entries for testing."""
    return [
        make_log_entry(
            hostname="mx1.example.com",
            mail_id="ABC123",
            message="relay=relay1.example.com[192.168.1.1]:25, status=sent (250 2.0.0 OK: queued as DEF456)",
        ),
        make_log_entry(
            hostname="relay1.example.com",
            mail_id="DEF456",
            message="relay=final.example.com[192.168.1.2]:25, status=sent (250 2.0.0 OK: queued as GHI789)",
        ),
    ]


class TestBuildHostnameMap:
    """Tests for _build_hostname_map function."""

    def test_maps_short_to_full_hostname(self):
        """Creates mapping from short hostname to full hostname."""
        logs = [make_log_entry(hostname="mx1.example.com")]
        result = _build_hostname_map(logs)

        assert result["mx1"] == "mx1.example.com"
        assert result["mx1.example.com"] == "mx1.example.com"

    def test_handles_multiple_hosts(self):
        """Maps multiple hostnames correctly."""
        logs = [
            make_log_entry(hostname="mx1.example.com", mail_id="ABC123"),
            make_log_entry(hostname="relay1.example.com", mail_id="DEF456"),
        ]
        result = _build_hostname_map(logs)

        assert result["mx1"] == "mx1.example.com"
        assert result["relay1"] == "relay1.example.com"

    def test_handles_short_hostname(self):
        """Handles hostname without domain."""
        logs = [make_log_entry(hostname="mx1")]
        result = _build_hostname_map(logs)
        assert result["mx1"] == "mx1"

    def test_skips_none_hostname(self):
        """Skips entries with None hostname."""
        logs = [make_log_entry(hostname=None)]
        result = _build_hostname_map(logs)
        assert len(result) == 0

    def test_empty_logs(self):
        """Returns empty map for empty logs."""
        assert _build_hostname_map([]) == {}


class TestNormalizeHost:
    """Tests for _normalize_host function."""

    def test_resolves_short_name(self):
        """Resolves short hostname to canonical form."""
        hostname_map = {
            "mx1": "mx1.example.com",
            "mx1.example.com": "mx1.example.com",
        }
        assert _normalize_host("mx1", hostname_map) == "mx1.example.com"

    def test_resolves_fqdn_via_short(self):
        """Resolves FQDN by extracting short name."""
        hostname_map = {"mx1": "mx1.example.com"}
        assert _normalize_host("mx1.other.com", hostname_map) == "mx1.example.com"

    def test_returns_original_when_not_found(self):
        """Returns original hostname when not in map."""
        hostname_map = {"mx1": "mx1.example.com"}
        assert _normalize_host("unknown.host.com", hostname_map) == "unknown.host.com"

    def test_empty_map(self):
        """Returns original hostname with empty map."""
        assert _normalize_host("mx1.example.com", {}) == "mx1.example.com"


class TestReconstructChain:
    """Tests for _reconstruct_chain function."""

    def test_builds_graph_from_logs(self, sample_logs):
        """Builds graph edges from relay log entries."""
        graph = MailGraph()
        _reconstruct_chain(sample_logs, graph)

        result = graph.to_dict()
        assert len(result["edges"]) >= 1

    @pytest.mark.parametrize(
        "hostname,service,mail_id",
        [
            ("mx1.example.com", "postfix/qmgr", "ABC123"),
            ("mx1.example.com", "postfix/smtp", None),
            (None, "postfix/smtp", "ABC123"),
        ],
    )
    def test_skips_invalid_entries(self, hostname, service, mail_id):
        """Skips log entries that are invalid for chain building."""
        logs = [
            make_log_entry(
                hostname=hostname,
                service=service,
                mail_id=mail_id,
                message="relay=relay1[192.168.1.1]:25, status=sent (250 2.0.0 OK: queued as DEF456)",
            )
        ]
        graph = MailGraph()
        _reconstruct_chain(logs, graph)

        result = graph.to_dict()
        assert len(result["edges"]) == 0


class TestTraceMailFlow:
    """Tests for trace_mail_flow function."""

    @patch("mailtrace.trace.do_trace")
    def test_traces_single_hop(self, mock_do_trace, opensearch_config):
        """Traces single hop and adds to graph."""
        mock_aggregator_class = MagicMock()
        mock_do_trace.return_value = None

        graph = MailGraph()
        trace_mail_flow("ABC123", mock_aggregator_class, opensearch_config, "mx1", graph)

        assert mock_do_trace.called

    @patch("mailtrace.trace.do_trace")
    def test_follows_relay_chain(self, mock_do_trace, opensearch_config):
        """Follows chain of relays until no more hops."""
        mock_aggregator_class = MagicMock()
        mock_do_trace.side_effect = [
            RelayResult(
                mail_id="DEF456",
                relay_host="relay1",
                relay_ip="192.168.1.1",
                relay_port=25,
                smtp_code=250,
            ),
            None,
            None,
        ]

        graph = MailGraph()
        trace_mail_flow("ABC123", mock_aggregator_class, opensearch_config, "mx1", graph)

        result = graph.to_dict()
        assert len(result["edges"]) == 1
        assert result["edges"][0]["from"] == "mx1"
        assert result["edges"][0]["to"] == "relay1"

    @patch("mailtrace.trace.do_trace")
    def test_retries_without_hostname_filter(self, mock_do_trace, opensearch_config):
        """Retries trace without hostname filter when first attempt fails."""
        mock_aggregator_class = MagicMock()
        mock_do_trace.side_effect = [None, None]

        graph = MailGraph()
        trace_mail_flow("ABC123", mock_aggregator_class, opensearch_config, "mx1", graph)

        assert mock_do_trace.call_count == 2


class TestTraceMailFlowByMessageId:
    """Tests for trace_mail_flow_by_message_id function."""

    def test_queries_by_message_id(self, sample_logs):
        """Queries logs by message_id and builds graph."""
        mock_aggregator = MagicMock()
        mock_aggregator.query_by.return_value = sample_logs

        graph = MailGraph()
        result = trace_mail_flow_by_message_id("test@example.com", mock_aggregator, graph)

        query = mock_aggregator.query_by.call_args[0][0]
        assert query.message_id == "test@example.com"
        assert result == sample_logs

    def test_returns_empty_for_no_results(self):
        """Returns empty list when no logs found."""
        mock_aggregator = MagicMock()
        mock_aggregator.query_by.return_value = []

        graph = MailGraph()
        result = trace_mail_flow_by_message_id("notfound@example.com", mock_aggregator, graph)

        assert result == []


class TestQueryLogsByKeywords:
    """Tests for query_logs_by_keywords function."""

    @patch("mailtrace.trace._query_logs_by_message_id")
    def test_uses_message_id_optimization_for_opensearch(
        self, mock_query, opensearch_config
    ):
        """Uses message_id optimization for OpenSearch method."""
        mock_aggregator_class = MagicMock()
        mock_query.return_value = {"ABC123": ("mx1", [])}

        query_logs_by_keywords(
            opensearch_config,
            mock_aggregator_class,
            "mx-cluster",
            ["user@example.com"],
            "2025-01-15 10:00:00",
            "1h",
        )

        mock_query.assert_called_once()

    @patch("mailtrace.trace._query_logs_from_aggregator")
    def test_queries_each_host_for_ssh(self, mock_query, ssh_config):
        """Queries each host in cluster for SSH method."""
        mock_aggregator_class = MagicMock()
        mock_query.return_value = {}

        query_logs_by_keywords(
            ssh_config,
            mock_aggregator_class,
            "test-cluster",
            ["user@example.com"],
            "2025-01-15 10:00:00",
            "1h",
        )

        assert mock_query.call_count == 2

    def test_returns_empty_dict_when_no_results(self, opensearch_config):
        """Returns empty dict when no mail IDs found."""
        mock_aggregator_class = MagicMock()
        mock_aggregator_instance = MagicMock()
        mock_aggregator_class.return_value = mock_aggregator_instance
        mock_aggregator_instance.query_by.return_value = []

        result = query_logs_by_keywords(
            opensearch_config,
            mock_aggregator_class,
            "mx-cluster",
            ["notfound@example.com"],
            "2025-01-15 10:00:00",
            "1h",
        )

        assert result == {}
