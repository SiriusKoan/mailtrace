"""Tests for mailtrace.parser module."""

import pytest

from mailtrace.config import OpenSearchMappingConfig
from mailtrace.parser import (
    PARSERS,
    OpensearchParser,
    Rfc3164Parser,
    Rfc5424Parser,
    SyslogParser,
    _extract_mail_id,
    _extract_service,
    _get_nested_value,
    check_mail_id_valid,
)


class TestGetNestedValue:
    """Tests for _get_nested_value helper function."""

    @pytest.mark.parametrize(
        "data,key,expected",
        [
            ({"name": "test"}, "name", "test"),
            (
                {"host": {"name": "mx1.example.com"}},
                "host.name",
                "mx1.example.com",
            ),
            (
                {"log": {"syslog": {"facility": {"name": "mail"}}}},
                "log.syslog.facility.name",
                "mail",
            ),
            ({"name": "test"}, "missing", None),
            ({"host": {"name": "mx1"}}, "host.ip", None),
            ({"host": {"name": "mx1"}}, "log.syslog.facility", None),
            ({"host": "string_value"}, "host.name", None),
            ({}, "any.key", None),
        ],
    )
    def test_get_nested_value(self, data, key, expected):
        """Returns correct value for various nested key paths."""
        assert _get_nested_value(data, key) == expected


class TestExtractMailId:
    """Tests for _extract_mail_id helper function."""

    @pytest.mark.parametrize(
        "input_str,expected",
        [
            ("ABC123:", "ABC123"),
            ("ABC123", "ABC123"),
            ("invalid-id:", None),
            ("", None),
            (":", None),
        ],
    )
    def test_extract_mail_id(self, input_str, expected):
        """Extracts mail ID correctly from various inputs."""
        assert _extract_mail_id(input_str) == expected


class TestExtractService:
    """Tests for _extract_service helper function."""

    @pytest.mark.parametrize(
        "input_str,expected",
        [
            ("postfix/qmgr[123456]", "postfix/qmgr"),
            ("postfix/smtp[99999]", "postfix/smtp"),
            ("postfix/cleanup", "postfix/cleanup"),
            ("postfix/smtp[]", "postfix/smtp"),
        ],
    )
    def test_extract_service(self, input_str, expected):
        """Extracts service name from various formats."""
        assert _extract_service(input_str) == expected


class TestCheckMailIdValid:
    """Tests for mail ID validation."""

    @pytest.mark.parametrize(
        "mail_id",
        [
            "6426E11F766",
            "8AF9ADF2A0",
            "ABC123",
            "A1B2C3D4E5",
        ],
    )
    def test_uppercase_mail_ids_valid(self, mail_id):
        """Uppercase alphanumeric mail IDs are valid."""
        assert check_mail_id_valid(mail_id) is True

    @pytest.mark.parametrize(
        "mail_id",
        [
            "hFvh2dXa",
            "mTVzWNyg",
            "abc123",
            "AbCdEf123",
        ],
    )
    def test_lowercase_mail_ids_valid(self, mail_id):
        """Lowercase and mixed-case mail IDs are valid (mxlog sink format)."""
        assert check_mail_id_valid(mail_id) is True

    @pytest.mark.parametrize(
        "mail_id",
        [
            "invalid-id",
            "invalid@id",
            "queue.id",
            "queue id",
            "queue:id",
            "<abc123>",
            "",
        ],
    )
    def test_invalid_mail_ids(self, mail_id):
        """Mail IDs with special characters are invalid."""
        assert check_mail_id_valid(mail_id) is False


class TestSyslogParserAutoDetect:
    """Tests for SyslogParser auto-detection logic."""

    def test_detects_rfc5424_by_digit(self):
        """Detects RFC 5424 format when log starts with digit."""
        parser = SyslogParser()
        log = (
            "2025-01-15T10:00:00.123+08:00 mx1 postfix/smtp[123]: ABC123: test"
        )
        entry = parser.parse(log)
        assert entry.datetime == "2025-01-15T10:00:00.123+08:00"

    def test_detects_rfc3164_by_letter(self):
        """Detects RFC 3164 format when log starts with letter."""
        parser = SyslogParser()
        log = "Jan 15 10:00:00 mx1 postfix/smtp[123]: ABC123: test message"
        entry = parser.parse(log)
        assert entry.datetime == "Jan 15 10:00:00"

    @pytest.mark.parametrize(
        "log,error_match",
        [
            ("", "Empty log entry"),
            ("!@#$% invalid log", "Unrecognized log format"),
            ("-some log entry", "Unrecognized log format"),
        ],
    )
    def test_invalid_formats_raise_error(self, log, error_match):
        """Raises ValueError for invalid log formats."""
        parser = SyslogParser()
        with pytest.raises(ValueError, match=error_match):
            parser.parse(log)


class TestSyslogParserRfc5424:
    """Tests for RFC 5424 parsing in SyslogParser."""

    def test_parse_complete_log(self):
        """Parses complete RFC 5424 log entry."""
        parser = SyslogParser()
        log = "2025-01-15T10:00:00.123456+08:00 mx1.example.com postfix/smtp[12345]: A2DE917F931: to=<user@example.com>, status=sent"
        entry = parser.parse(log)

        assert entry.datetime == "2025-01-15T10:00:00.123456+08:00"
        assert entry.hostname == "mx1.example.com"
        assert entry.service == "postfix/smtp"
        assert entry.mail_id == "A2DE917F931"
        assert entry.message == "to=<user@example.com>, status=sent"

    def test_parse_utc_timestamp(self):
        """Parses RFC 5424 log with UTC timestamp."""
        parser = SyslogParser()
        log = "2025-01-15T02:00:00Z mx1 postfix/qmgr[999]: XYZ789: from=<test@test.com>"
        entry = parser.parse(log)

        assert entry.datetime == "2025-01-15T02:00:00Z"
        assert entry.mail_id == "XYZ789"

    def test_parse_without_message(self):
        """Parses RFC 5424 log without message part."""
        parser = SyslogParser()
        log = "2025-01-15T10:00:00+00:00 mx1 postfix/smtp[123]: ABC123:"
        entry = parser.parse(log)

        assert entry.mail_id == "ABC123"
        assert entry.message == ""

    def test_parse_invalid_mail_id(self):
        """Returns None for invalid mail ID in RFC 5424."""
        parser = SyslogParser()
        log = "2025-01-15T10:00:00+00:00 mx1 postfix/smtp[123]: lost-connection: from unknown"
        entry = parser.parse(log)
        assert entry.mail_id is None

    def test_invalid_format_too_few_parts(self):
        """Raises ValueError for RFC 5424 with too few parts."""
        parser = SyslogParser()
        with pytest.raises(ValueError, match="Invalid RFC 5424"):
            parser.parse("2025-01-15T10:00:00 mx1 postfix")

    def test_parse_lowercase_mail_id(self):
        """Parses RFC 5424 log with lowercase mail ID."""
        parser = SyslogParser()
        log = (
            "2025-01-15T10:00:00+00:00 mx1 postfix/smtp[123]: abc123def: test"
        )
        entry = parser.parse(log)
        assert entry.mail_id == "abc123def"


class TestSyslogParserRfc3164:
    """Tests for RFC 3164 parsing in SyslogParser."""

    def test_parse_complete_log(self):
        """Parses complete RFC 3164 log entry."""
        parser = SyslogParser()
        log = "Jan 15 10:30:45 mailer1 postfix/qmgr[54321]: DEADBEEF123: from=<sender@example.com>, size=1234"
        entry = parser.parse(log)

        assert entry.datetime == "Jan 15 10:30:45"
        assert entry.hostname == "mailer1"
        assert entry.service == "postfix/qmgr"
        assert entry.mail_id == "DEADBEEF123"
        assert entry.message == "from=<sender@example.com>, size=1234"

    @pytest.mark.parametrize(
        "log,expected_datetime",
        [
            (
                "Feb 1 08:00:00 mx1 postfix/smtp[123]: ABC123: test",
                "Feb 1 08:00:00",
            ),
            (
                "Feb  1 08:00:00 mx1 postfix/smtp[123]: ABC123: test",
                "Feb 1 08:00:00",
            ),
        ],
    )
    def test_parse_single_digit_day(self, log, expected_datetime):
        """Parses RFC 3164 logs with single-digit day formats."""
        parser = SyslogParser()
        entry = parser.parse(log)
        assert entry.datetime == expected_datetime
        assert entry.mail_id == "ABC123"

    def test_parse_without_message(self):
        """Parses RFC 3164 log without message part."""
        parser = SyslogParser()
        log = "Mar 10 12:00:00 mx1 postfix/smtp[123]: XYZ789:"
        entry = parser.parse(log)

        assert entry.mail_id == "XYZ789"
        assert entry.message == ""

    def test_parse_invalid_mail_id(self):
        """Returns None for invalid mail ID in RFC 3164."""
        parser = SyslogParser()
        log = "Apr 20 15:30:00 mx1 postfix/smtpd[123]: client-disconnect: from unknown"
        entry = parser.parse(log)
        assert entry.mail_id is None

    def test_invalid_format_too_few_parts(self):
        """Raises ValueError for RFC 3164 with too few parts."""
        parser = SyslogParser()
        with pytest.raises(ValueError, match="Invalid RFC 3164"):
            parser.parse("Jan 15 10:00:00 mx1 postfix")

    @pytest.mark.parametrize(
        "month",
        [
            "Jan",
            "Feb",
            "Mar",
            "Apr",
            "May",
            "Jun",
            "Jul",
            "Aug",
            "Sep",
            "Oct",
            "Nov",
            "Dec",
        ],
    )
    def test_parse_all_months(self, month):
        """Parses RFC 3164 logs with all month abbreviations."""
        parser = SyslogParser()
        log = f"{month} 15 10:00:00 mx1 postfix/smtp[1]: A1: msg"
        entry = parser.parse(log)
        assert entry.datetime.startswith(month)


class TestRfc5424Parser:
    """Tests for Rfc5424Parser (dedicated RFC 5424 parser)."""

    def test_parse_valid_log(self):
        """Parses valid RFC 5424 log entry."""
        parser = Rfc5424Parser()
        log = "2025-06-01T14:30:00.000+00:00 relay1 postfix/smtp[999]: CAFE123: test"
        entry = parser.parse(log)

        assert entry.datetime == "2025-06-01T14:30:00.000+00:00"
        assert entry.hostname == "relay1"
        assert entry.service == "postfix/smtp"
        assert entry.mail_id == "CAFE123"

    def test_does_not_accept_rfc3164(self):
        """RFC 5424 parser parses RFC 3164 format incorrectly."""
        parser = Rfc5424Parser()
        log = "Jan 15 10:00:00 mx1 postfix/smtp[123]: ABC123: test"
        entry = parser.parse(log)
        assert entry.datetime == "Jan"


class TestRfc3164Parser:
    """Tests for Rfc3164Parser (dedicated RFC 3164 parser)."""

    def test_parse_valid_log(self):
        """Parses valid RFC 3164 log entry."""
        parser = Rfc3164Parser()
        log = "Dec 25 23:59:59 santa postfix/smtp[12345]: GIFT123: merry christmas"
        entry = parser.parse(log)

        assert entry.datetime == "Dec 25 23:59:59"
        assert entry.hostname == "santa"
        assert entry.service == "postfix/smtp"
        assert entry.mail_id == "GIFT123"
        assert entry.message == "merry christmas"

    def test_does_not_validate_month(self):
        """RFC 3164 parser does not validate month abbreviation."""
        parser = Rfc3164Parser()
        log = "Xyz 15 10:00:00 mx1 postfix/smtp[123]: ABC123: test"
        entry = parser.parse(log)
        assert entry.datetime == "Xyz 15 10:00:00"


class TestParserEnrichment:
    """Tests for parse_with_enrichment functionality."""

    def test_enrichment_extracts_relay_info(self):
        """Enrichment extracts relay info from message."""
        parser = SyslogParser()
        log = "2025-01-15T10:00:00+00:00 mx1 postfix/smtp[123]: ABC123: to=<user@example.com>, relay=mail.example.com[192.168.1.1]:25, status=sent (250 2.0.0 OK: queued as XYZ789)"
        entry = parser.parse_with_enrichment(log)

        assert entry.relay_host == "mail.example.com"
        assert entry.relay_ip == "192.168.1.1"
        assert entry.relay_port == 25
        assert entry.smtp_code == 250
        assert entry.queued_as == "XYZ789"

    def test_enrichment_extracts_message_id(self):
        """Enrichment extracts message-id from message text."""
        parser = SyslogParser()
        log = "2025-01-15T10:00:00+00:00 mx1 postfix/cleanup[123]: ABC123: message-id=<unique123@example.com>"
        entry = parser.parse_with_enrichment(log)
        assert entry.message_id == "unique123@example.com"

    def test_enrichment_skips_already_complete_entry(self):
        """Enrichment returns entry unchanged if already complete."""
        mapping = OpenSearchMappingConfig(
            hostname="host",
            message="msg",
            timestamp="ts",
            service="svc",
            relay_host="rh",
            relay_ip="ri",
            relay_port="rp",
            smtp_code="sc",
            queued_as="qa",
        )
        parser = OpensearchParser(mapping=mapping)
        log = {
            "ts": "2025-01-15T10:00:00Z",
            "host": "mx1",
            "svc": "postfix/smtp",
            "msg": "some message",
            "rh": "relay.example.com",
            "ri": "10.0.0.1",
            "rp": 25,
            "sc": 250,
            "qa": "COMPLETE123",
        }
        entry = parser.parse_with_enrichment(log)

        assert entry.relay_host == "relay.example.com"
        assert entry.queued_as == "COMPLETE123"

    def test_enrichment_no_relay_info_in_message(self):
        """Enrichment handles messages without relay info."""
        parser = SyslogParser()
        log = "2025-01-15T10:00:00+00:00 mx1 postfix/qmgr[123]: ABC123: from=<sender@example.com>, size=1234"
        entry = parser.parse_with_enrichment(log)

        assert entry.relay_host is None
        assert entry.queued_as is None


class TestOpensearchParserMailIdMapping:
    """Tests for OpensearchParser mail_id field mapping."""

    def test_mail_id_from_dedicated_mapping(self):
        """Extracts mail_id from dedicated mail_id mapping field."""
        mapping = OpenSearchMappingConfig(
            hostname="host.name",
            message="message",
            timestamp="@timestamp",
            service="appname",
            queueid="",
            mail_id="postfix.mail_id",
        )
        parser = OpensearchParser(mapping=mapping)
        log = {
            "@timestamp": "2025-01-15T10:00:00Z",
            "host": {"name": "mx1"},
            "appname": "postfix/smtp",
            "message": "some log message without mail_id prefix",
            "postfix": {"mail_id": "DEDICATED123"},
        }

        entry = parser.parse(log)
        assert entry.mail_id == "DEDICATED123"

    def test_mail_id_mapping_takes_precedence_over_message(self):
        """Dedicated mail_id mapping takes precedence over message parsing."""
        mapping = OpenSearchMappingConfig(
            hostname="host.name",
            message="message",
            timestamp="@timestamp",
            service="appname",
            queueid="",
            mail_id="custom.queue_id",
        )
        parser = OpensearchParser(mapping=mapping)
        log = {
            "@timestamp": "2025-01-15T10:00:00Z",
            "host": {"name": "mx1"},
            "appname": "postfix/smtp",
            "message": "FROMMESSAGE123: test",
            "custom": {"queue_id": "FROMFIELD456"},
        }

        entry = parser.parse(log)
        assert entry.mail_id == "FROMFIELD456"

    def test_queueid_takes_precedence_over_mail_id_mapping(self):
        """Structured queueid field takes precedence over mail_id mapping."""
        mapping = OpenSearchMappingConfig(
            hostname="host.name",
            message="message",
            timestamp="@timestamp",
            service="appname",
            queueid="structured.queueid",
            mail_id="fallback.mail_id",
        )
        parser = OpensearchParser(mapping=mapping)
        log = {
            "@timestamp": "2025-01-15T10:00:00Z",
            "host": {"name": "mx1"},
            "appname": "postfix/smtp",
            "message": "test message",
            "structured": {"queueid": "PRIMARY123"},
            "fallback": {"mail_id": "SECONDARY456"},
        }

        entry = parser.parse(log)
        assert entry.mail_id == "PRIMARY123"


class TestParsersRegistry:
    """Tests for the PARSERS registry."""

    def test_registry_contains_all_parsers(self):
        """Registry contains all expected parser classes."""
        expected = {
            "SyslogParser",
            "Rfc5424Parser",
            "Rfc3164Parser",
            "OpensearchParser",
        }
        assert set(PARSERS.keys()) == expected

    def test_registry_values_are_classes(self):
        """Registry values are parser classes, not instances."""
        for parser_class in PARSERS.values():
            assert isinstance(parser_class, type)

    def test_syslog_parser_from_registry(self):
        """Can instantiate SyslogParser from registry."""
        parser = PARSERS["SyslogParser"]()
        log = "2025-01-15T10:00:00+00:00 mx1 postfix/smtp[1]: A1: test"
        entry = parser.parse(log)
        assert entry.mail_id == "A1"

    def test_opensearch_parser_from_registry(self):
        """Can instantiate OpensearchParser from registry."""
        mapping = OpenSearchMappingConfig()
        parser = PARSERS["OpensearchParser"](mapping=mapping)  # type: ignore[call-arg]
        assert isinstance(parser, OpensearchParser)
