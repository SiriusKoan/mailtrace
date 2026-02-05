"""Tests for mailtrace.aggregator module."""

from unittest.mock import MagicMock

import pytest

from mailtrace.aggregator import (
    _RELAY_SERVICES,
    _extract_next_mail_id,
    _parse_relay_info,
    do_trace,
    extract_message_ids,
    select_aggregator,
)
from mailtrace.aggregator.opensearch import OpenSearch
from mailtrace.aggregator.ssh_host import SSHHost
from mailtrace.config import Config, Method, OpenSearchConfig, SSHConfig
from mailtrace.models import LogEntry


@pytest.fixture
def config():
    """Create a test configuration."""
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


def make_log_entry(
    service="postfix/smtp",
    mail_id="ABC123",
    message="test",
    message_id=None,
    queued_as=None,
):
    """Helper to create LogEntry instances."""
    return LogEntry(
        datetime="2025-01-15T10:00:00Z",
        hostname="mx1",
        service=service,
        mail_id=mail_id,
        message=message,
        message_id=message_id,
        queued_as=queued_as,
    )


class TestRelayServices:
    """Tests for _RELAY_SERVICES constant."""

    def test_contains_expected_services(self):
        """Contains expected relay services."""
        assert "postfix/smtp" in _RELAY_SERVICES
        assert "postfix/lmtp" in _RELAY_SERVICES
        assert "postfix/qmgr" not in _RELAY_SERVICES


class TestExtractMessageIds:
    """Tests for extract_message_ids function."""

    def test_extracts_from_structured_field(self):
        """Extracts message_id from structured field."""
        logs = [make_log_entry(message_id="structured@example.com")]
        result = extract_message_ids(logs)
        assert "structured@example.com" in result

    def test_extracts_from_message_text(self):
        """Extracts message_id from message text when not in structured field."""
        logs = [make_log_entry(message="message-id=<fromtext@example.com>")]
        result = extract_message_ids(logs)
        assert "fromtext@example.com" in result

    def test_prefers_structured_field(self):
        """Prefers structured field over message text."""
        logs = [
            make_log_entry(
                message="message-id=<fromtext@example.com>",
                message_id="structured@example.com",
            )
        ]
        result = extract_message_ids(logs)
        assert result == {"structured@example.com"}

    def test_multiple_logs_deduplicated(self):
        """Extracts unique message_ids from multiple logs."""
        logs = [
            make_log_entry(mail_id="ABC123", message_id="id1@example.com"),
            make_log_entry(mail_id="DEF456", message_id="id2@example.com"),
            make_log_entry(mail_id="GHI789", message_id="id1@example.com"),
        ]
        result = extract_message_ids(logs)
        assert result == {"id1@example.com", "id2@example.com"}

    def test_empty_and_no_message_ids(self):
        """Returns empty set for empty logs or when no message_ids found."""
        assert extract_message_ids([]) == set()
        assert extract_message_ids([make_log_entry(service="postfix/qmgr")]) == set()


class TestExtractNextMailId:
    """Tests for _extract_next_mail_id function."""

    def test_extracts_from_structured_field(self):
        """Extracts mail_id from queued_as structured field."""
        entry = make_log_entry(message="status=sent", queued_as="STRUCTURED789")
        assert _extract_next_mail_id(entry) == "STRUCTURED789"

    def test_extracts_from_message(self):
        """Extracts mail_id from message when no structured field."""
        entry = make_log_entry(
            message="status=sent (250 2.0.0 OK: queued as FROMMSG456)"
        )
        assert _extract_next_mail_id(entry) == "FROMMSG456"

    def test_prefers_structured_field(self):
        """Prefers structured field over message text."""
        entry = make_log_entry(
            message="status=sent (250 2.0.0 OK: queued as FROMMSG456)",
            queued_as="STRUCTURED789",
        )
        assert _extract_next_mail_id(entry) == "STRUCTURED789"

    def test_returns_none_when_not_found(self):
        """Returns None when no queued_as info found."""
        entry = make_log_entry(message="status=sent without queue info")
        assert _extract_next_mail_id(entry) is None


class TestParseRelayInfo:
    """Tests for _parse_relay_info function."""

    def test_parses_complete_relay_info(self):
        """Parses complete relay information from log entry."""
        entry = make_log_entry(
            message="to=<user@example.com>, relay=mail.example.com[192.168.1.1]:25, status=sent (250 2.0.0 OK: queued as XYZ789)"
        )
        result = _parse_relay_info(entry)

        assert result is not None
        assert result.mail_id == "XYZ789"
        assert result.relay_host == "mail.example.com"
        assert result.relay_ip == "192.168.1.1"
        assert result.relay_port == 25
        assert result.smtp_code == 250

    @pytest.mark.parametrize(
        "message",
        [
            "status=deferred (450 4.7.1 try again later)",
            "from=<sender@example.com>, size=1234",
            "250 2.0.0 OK relay=mail.example.com[192.168.1.1]:25",
            "250 2.0.0 OK: queued as XYZ789",
        ],
    )
    def test_returns_none_for_incomplete_relay_info(self, message):
        """Returns None for messages missing required relay info."""
        entry = make_log_entry(message=message)
        assert _parse_relay_info(entry) is None

    def test_uses_structured_queued_as(self):
        """Uses structured queued_as field when available."""
        entry = make_log_entry(
            message="relay=mail.example.com[192.168.1.1]:25, status=sent (250 2.0.0 OK: queued as WRONG)",
            queued_as="CORRECT123",
        )
        result = _parse_relay_info(entry)
        assert result is not None
        assert result.mail_id == "CORRECT123"


class TestDoTrace:
    """Tests for do_trace function."""

    def test_returns_relay_result_on_success(self):
        """Returns RelayResult when relay entry found."""
        mock_aggregator = MagicMock()
        mock_aggregator.query_by.return_value = [
            make_log_entry(
                message="relay=mail.example.com[192.168.1.1]:25, status=sent (250 2.0.0 OK: queued as XYZ789)"
            )
        ]

        result = do_trace("ABC123", mock_aggregator)

        assert result is not None
        assert result.mail_id == "XYZ789"
        assert result.relay_host == "mail.example.com"

    def test_returns_none_when_no_relay(self):
        """Returns None when no relay entry found."""
        mock_aggregator = MagicMock()
        mock_aggregator.query_by.return_value = [
            make_log_entry(service="postfix/qmgr", message="from=<sender@example.com>")
        ]

        assert do_trace("ABC123", mock_aggregator) is None

    def test_returns_none_for_empty_results(self):
        """Returns None when no log entries found."""
        mock_aggregator = MagicMock()
        mock_aggregator.query_by.return_value = []

        assert do_trace("ABC123", mock_aggregator) is None

    def test_skips_non_relay_services(self):
        """Skips log entries from non-relay services."""
        mock_aggregator = MagicMock()
        mock_aggregator.query_by.return_value = [
            make_log_entry(
                service="postfix/qmgr",
                message="relay=mail.example.com[192.168.1.1]:25, status=sent (250 2.0.0 OK: queued as XYZ789)",
            )
        ]

        assert do_trace("ABC123", mock_aggregator) is None

    def test_queries_with_correct_mail_id(self):
        """Queries aggregator with correct mail_id."""
        mock_aggregator = MagicMock()
        mock_aggregator.query_by.return_value = []

        do_trace("TESTID123", mock_aggregator)

        query = mock_aggregator.query_by.call_args[0][0]
        assert query.mail_id == "TESTID123"


class TestSelectAggregator:
    """Tests for select_aggregator function."""

    def test_returns_opensearch_for_opensearch_method(self):
        """Returns OpenSearch class for OPENSEARCH method."""
        config = Config(
            method=Method.OPENSEARCH,
            log_level="INFO",
            ssh_config=SSHConfig(username="test", password="test"),
            opensearch_config=OpenSearchConfig(),
        )
        assert select_aggregator(config) is OpenSearch

    def test_returns_ssh_for_ssh_method(self):
        """Returns SSHHost class for SSH method."""
        config = Config(
            method=Method.SSH,
            log_level="INFO",
            ssh_config=SSHConfig(username="test", password="test"),
            opensearch_config=OpenSearchConfig(),
        )
        assert select_aggregator(config) is SSHHost

    def test_raises_for_unsupported_method(self, config):
        """Raises ValueError for unsupported method."""
        config.method = "invalid"  # type: ignore[assignment]

        with pytest.raises(ValueError, match="Unsupported method"):
            select_aggregator(config)
