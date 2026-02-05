"""Tests for mailtrace.utils module."""

import datetime

import pytest

from mailtrace.utils import (
    RelayResult,
    analyze_log_from_message,
    get_hosts,
    print_blue,
    print_red,
    time_range_to_timedelta,
    time_validation,
)


class TestTimeValidation:
    """Tests for time_validation function."""

    def test_valid_time_and_range(self):
        """Valid time and time_range passes validation."""
        assert time_validation("2025-01-15 10:30:00", "1h") == ""

    def test_valid_empty_both(self):
        """Empty time and time_range passes validation."""
        assert time_validation("", "") == ""

    @pytest.mark.parametrize(
        "time,time_range,expected_msg",
        [
            ("2025-01-15T10:30:00", "1h", "should be in format YYYY-MM-DD HH:MM:SS"),
            ("2025-01-15", "1h", "should be in format"),
            ("2025-01-15 10:30:00", "", "must be provided together"),
            ("", "1h", "must be provided together"),
            ("2025-01-15 10:30:00", "1x", "should be in format [0-9]+[dhm]"),
            ("2025-01-15 10:30:00", "10", "should be in format [0-9]+[dhm]"),
        ],
    )
    def test_invalid_inputs(self, time, time_range, expected_msg):
        """Invalid inputs return appropriate error messages."""
        result = time_validation(time, time_range)
        assert expected_msg in result

    @pytest.mark.parametrize(
        "time_range",
        ["1d", "24h", "60m", "100d", "999h", "1440m"],
    )
    def test_valid_time_range_formats(self, time_range):
        """Various valid time_range formats pass validation."""
        assert time_validation("2025-01-15 10:30:00", time_range) == ""


class TestTimeRangeToTimedelta:
    """Tests for time_range_to_timedelta function."""

    @pytest.mark.parametrize(
        "time_range,expected",
        [
            ("5d", datetime.timedelta(days=5)),
            ("12h", datetime.timedelta(hours=12)),
            ("30m", datetime.timedelta(minutes=30)),
            ("1h", datetime.timedelta(hours=1)),
            ("1000h", datetime.timedelta(hours=1000)),
        ],
    )
    def test_valid_conversions(self, time_range, expected):
        """Converts valid time ranges to timedelta."""
        assert time_range_to_timedelta(time_range) == expected

    @pytest.mark.parametrize("invalid_input", ["invalid", "10x", "", "h"])
    def test_invalid_format_raises(self, invalid_input):
        """Raises ValueError for invalid formats."""
        with pytest.raises(ValueError, match="Invalid time range format"):
            time_range_to_timedelta(invalid_input)


class TestPrintFunctions:
    """Tests for colored print functions."""

    def test_print_blue(self, capsys):
        """print_blue outputs blue ANSI text."""
        print_blue("test message")
        captured = capsys.readouterr()
        assert "\033[94mtest message\033[0m" in captured.out

    def test_print_red(self, capsys):
        """print_red outputs red ANSI text."""
        print_red("error message")
        captured = capsys.readouterr()
        assert "\033[91merror message\033[0m" in captured.out


class TestGetHosts:
    """Tests for get_hosts function."""

    def test_short_hostname_with_domain(self):
        """Short hostname generates both short and FQDN forms."""
        result = get_hosts(["mx1"], "example.com")
        assert "mx1" in result
        assert "mx1.example.com" in result

    def test_fqdn_generates_short_form(self):
        """FQDN generates both FQDN and short forms."""
        result = get_hosts(["mx1.example.com"], "other.com")
        assert "mx1.example.com" in result
        assert "mx1" in result

    @pytest.mark.parametrize(
        "ip",
        ["192.168.1.1", "2001:0db8:85a3:0000:0000:8a2e:0370:7334"],
    )
    def test_ip_address_kept_as_is(self, ip):
        """IP addresses are kept unchanged."""
        result = get_hosts([ip], "example.com")
        assert ip in result

    def test_multiple_hostnames(self):
        """Multiple hostnames are all processed."""
        result = get_hosts(["mx1", "mx2"], "example.com")
        assert all(h in result for h in ["mx1", "mx2", "mx1.example.com", "mx2.example.com"])

    def test_empty_domain(self):
        """Short hostname without domain only returns short form."""
        assert get_hosts(["mx1"], "") == ["mx1"]

    def test_empty_hostnames(self):
        """Empty hostname list returns empty result."""
        assert get_hosts([], "example.com") == []

    def test_whitespace_handling(self):
        """Whitespace in hostnames is handled correctly."""
        result = get_hosts(["mx1", "  ", "  mx2  "], "example.com")
        assert "mx1" in result
        assert "mx2" in result
        assert "" not in result

    def test_deduplication(self):
        """Duplicate hostnames are deduplicated."""
        result = get_hosts(["mx1", "mx1", "mx1.example.com"], "example.com")
        assert len(set(result)) == len(result)


class TestRelayResult:
    """Tests for RelayResult dataclass."""

    def test_create_relay_result(self):
        """Can create RelayResult with all fields."""
        result = RelayResult(
            mail_id="ABC123",
            relay_host="mail.example.com",
            relay_ip="192.168.1.1",
            relay_port=25,
            smtp_code=250,
        )
        assert result.mail_id == "ABC123"
        assert result.relay_host == "mail.example.com"
        assert result.relay_port == 25

    def test_relay_result_with_none_fields(self):
        """Can create RelayResult with None fields."""
        result = RelayResult(
            mail_id=None,
            relay_host=None,
            relay_ip=None,
            relay_port=None,
            smtp_code=None,
        )
        assert result.mail_id is None


class TestAnalyzeLogFromMessage:
    """Tests for analyze_log_from_message function."""

    def test_successful_relay_message(self):
        """Parses complete relay message with 250 response."""
        message = "to=<user@example.com>, relay=mail.example.com[192.168.1.1]:25, delay=0.5, status=sent (250 2.0.0 OK: queued as ABC123)"
        result = analyze_log_from_message(message)

        assert result is not None
        assert result.mail_id == "ABC123"
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
            "250 2.0.0 OK: queued as ABC123",
        ],
    )
    def test_returns_none_for_incomplete_messages(self, message):
        """Returns None for messages missing required relay info."""
        assert analyze_log_from_message(message) is None

    def test_different_port(self):
        """Parses relay with non-standard port."""
        message = "relay=mail.example.com[10.0.0.1]:587, status=sent (250 2.0.0 OK: queued as XYZ789)"
        result = analyze_log_from_message(message)

        assert result is not None
        assert result.relay_port == 587

    def test_ipv6_relay(self):
        """Parses relay with IPv6 address."""
        message = "relay=mail.example.com[2001:db8::1]:25, status=sent (250 2.0.0 OK: queued as DEF456)"
        result = analyze_log_from_message(message)

        assert result is not None
        assert result.relay_ip == "2001:db8::1"

    def test_localhost_relay(self):
        """Parses relay to localhost."""
        message = "relay=localhost[127.0.0.1]:10025, status=sent (250 2.0.0 OK: queued as LOCAL123)"
        result = analyze_log_from_message(message)

        assert result is not None
        assert result.relay_host == "localhost"
        assert result.relay_ip == "127.0.0.1"
        assert result.relay_port == 10025
