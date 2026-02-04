"""Tests for mailtrace.parser module."""

import pytest

from mailtrace.parser import check_mail_id_valid


class TestCheckMailIdValid:
    """Tests for mail ID validation."""

    @pytest.mark.parametrize(
        "mail_id",
        [
            "6426E11F766",  # Postfix uppercase (standard)
            "8AF9ADF2A0",  # Postfix uppercase
            "ABC123",  # Simple uppercase
            "A1B2C3D4E5",  # Mixed alphanumeric uppercase
        ],
    )
    def test_uppercase_mail_ids_valid(self, mail_id: str) -> None:
        """Uppercase alphanumeric mail IDs should be valid."""
        assert check_mail_id_valid(mail_id) is True

    @pytest.mark.parametrize(
        "mail_id",
        [
            "hFvh2dXa",  # mxlog sink format (mixed case)
            "mTVzWNyg",  # mxlog sink format (mixed case)
            "abc123",  # All lowercase
            "AbCdEf123",  # Mixed case
        ],
    )
    def test_lowercase_mail_ids_valid(self, mail_id: str) -> None:
        """Lowercase and mixed-case mail IDs should be valid.

        Some mail servers (e.g., mxlog sink) use lowercase queue IDs.
        """
        assert check_mail_id_valid(mail_id) is True

    @pytest.mark.parametrize(
        "mail_id",
        [
            "invalid-id",  # Contains dash
            "invalid@id",  # Contains at-sign
            "queue.id",  # Contains dot
            "queue id",  # Contains space
            "queue:id",  # Contains colon
            "<abc123>",  # Contains angle brackets
            "",  # Empty string
        ],
    )
    def test_invalid_mail_ids(self, mail_id: str) -> None:
        """Mail IDs with special characters should be invalid."""
        assert check_mail_id_valid(mail_id) is False
