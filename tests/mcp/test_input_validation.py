"""Tests for MCP tool input validation using Pydantic models."""

import pytest
from pydantic import ValidationError

from mailtrace.mcp.tools import QueryLogsInput, TraceMailInput


class TestQueryLogsInputValidation:
    """Tests for QueryLogsInput Pydantic model validation."""

    def test_valid_input_minimal(self):
        """Test valid input with required fields only."""
        input_data = QueryLogsInput(
            host="mail.example.com",
            keywords=["user@example.com"],
        )
        assert input_data.host == "mail.example.com"
        assert input_data.keywords == ["user@example.com"]
        assert input_data.time is None
        assert input_data.time_range is None

    def test_valid_input_full(self):
        """Test valid input with all fields."""
        input_data = QueryLogsInput(
            host="mail.example.com",
            keywords=["user@example.com", "example.org"],
            time="2025-01-15 10:00:00",
            time_range="10h",
        )
        assert input_data.host == "mail.example.com"
        assert input_data.keywords == ["user@example.com", "example.org"]
        assert input_data.time == "2025-01-15 10:00:00"
        assert input_data.time_range == "10h"

    def test_host_required(self):
        """Test that host is required."""
        with pytest.raises(ValidationError) as exc_info:
            QueryLogsInput(keywords=["user@example.com"])
        assert "host" in str(exc_info.value)

    def test_host_cannot_be_empty(self):
        """Test that host cannot be empty string."""
        with pytest.raises(ValidationError) as exc_info:
            QueryLogsInput(host="", keywords=["user@example.com"])
        assert "host" in str(exc_info.value).lower()

    def test_host_whitespace_stripped(self):
        """Test that whitespace is stripped from host."""
        input_data = QueryLogsInput(
            host="  mail.example.com  ",
            keywords=["user@example.com"],
        )
        assert input_data.host == "mail.example.com"

    def test_keywords_required(self):
        """Test that keywords is required."""
        with pytest.raises(ValidationError) as exc_info:
            QueryLogsInput(host="mail.example.com")
        assert "keywords" in str(exc_info.value)

    def test_keywords_cannot_be_empty_list(self):
        """Test that keywords cannot be empty list."""
        with pytest.raises(ValidationError) as exc_info:
            QueryLogsInput(host="mail.example.com", keywords=[])
        assert "keywords" in str(exc_info.value).lower()

    def test_keywords_cannot_be_all_whitespace(self):
        """Test that keywords cannot be all whitespace strings."""
        with pytest.raises(ValidationError) as exc_info:
            QueryLogsInput(host="mail.example.com", keywords=["  ", "\t"])
        assert "keyword" in str(exc_info.value).lower()

    def test_keywords_whitespace_stripped(self):
        """Test that whitespace is stripped from keywords."""
        input_data = QueryLogsInput(
            host="mail.example.com",
            keywords=["  user@example.com  ", "  example.org  "],
        )
        assert input_data.keywords == ["user@example.com", "example.org"]

    def test_keywords_empty_strings_filtered(self):
        """Test that empty strings are filtered from keywords."""
        input_data = QueryLogsInput(
            host="mail.example.com",
            keywords=["user@example.com", "", "  ", "example.org"],
        )
        assert input_data.keywords == ["user@example.com", "example.org"]


class TestTraceMailInputValidation:
    """Tests for TraceMailInput Pydantic model validation."""

    def test_valid_input_minimal(self):
        """Test valid input with required fields only."""
        input_data = TraceMailInput(
            host="mail.example.com",
            mail_id="ABC123DEF",
        )
        assert input_data.host == "mail.example.com"
        assert input_data.mail_id == "ABC123DEF"
        assert input_data.time is None
        assert input_data.time_range is None

    def test_valid_input_full(self):
        """Test valid input with all fields."""
        input_data = TraceMailInput(
            host="mail.example.com",
            mail_id="ABC123DEF",
            time="2025-01-15 10:00:00",
            time_range="10h",
        )
        assert input_data.host == "mail.example.com"
        assert input_data.mail_id == "ABC123DEF"
        assert input_data.time == "2025-01-15 10:00:00"
        assert input_data.time_range == "10h"

    def test_host_required(self):
        """Test that host is required."""
        with pytest.raises(ValidationError) as exc_info:
            TraceMailInput(mail_id="ABC123DEF")
        assert "host" in str(exc_info.value)

    def test_host_cannot_be_empty(self):
        """Test that host cannot be empty string."""
        with pytest.raises(ValidationError) as exc_info:
            TraceMailInput(host="", mail_id="ABC123DEF")
        assert "host" in str(exc_info.value).lower()

    def test_either_mail_id_or_keywords_required(self):
        """Test that either mail_id or keywords is required."""
        with pytest.raises(ValidationError) as exc_info:
            TraceMailInput(host="mail.example.com")
        assert "mail_id" in str(exc_info.value) or "keywords" in str(
            exc_info.value
        )

    def test_keywords_only_is_valid(self):
        """Test that providing only keywords is valid."""
        input_data = TraceMailInput(
            host="mail.example.com",
            keywords=["user@example.com"],
        )
        assert input_data.keywords == ["user@example.com"]
        assert input_data.mail_id is None

    def test_empty_keywords_list_fails(self):
        """Test that empty keywords list fails validation."""
        with pytest.raises(ValidationError):
            TraceMailInput(
                host="mail.example.com",
                keywords=[],
            )

    def test_host_whitespace_stripped(self):
        """Test that whitespace is stripped from host."""
        input_data = TraceMailInput(
            host="  mail.example.com  ",
            mail_id="ABC123DEF",
        )
        assert input_data.host == "mail.example.com"

    def test_mail_id_whitespace_stripped(self):
        """Test that whitespace is stripped from mail_id."""
        input_data = TraceMailInput(
            host="mail.example.com",
            mail_id="  ABC123DEF  ",
        )
        assert input_data.mail_id == "ABC123DEF"
