"""Tests for the doctor config validation command."""

from unittest.mock import patch

from click.testing import CliRunner

from mailtrace.__main__ import cli
from mailtrace.config import (
    Config,
    Method,
    OpenSearchConfig,
    OpenSearchMappingConfig,
    SSHConfig,
)
from mailtrace.doctor import check_config


class TestCheckConfig:
    """Tests for the check_config function."""

    def _make_config(self, **mapping_overrides):
        """Helper to create a Config with custom mapping fields."""
        mapping = OpenSearchMappingConfig(**mapping_overrides)
        return Config(
            method=Method.OPENSEARCH,
            log_level="INFO",
            ssh_config=SSHConfig(username="dummy", password="dummy"),
            opensearch_config=OpenSearchConfig(
                host="localhost",
                port=9200,
                username="admin",
                password="admin",
                mapping=mapping,
            ),
        )

    def test_all_fields_configured(self):
        """All fields configured returns no errors and no warnings."""
        config = self._make_config(
            facility="f",
            service="s",
            queueid="q",
            queued_as="qa",
            message_id="m",
        )
        result = check_config(config)
        assert result["errors"] == []
        assert result["warnings"] == []

    def test_missing_optional_fields_reported_as_warnings(self):
        """Missing optional fields appear in warnings section."""
        config = self._make_config(
            facility=None,
            queueid=None,
            message_id=None,
        )
        result = check_config(config)
        assert result["errors"] == []
        warning_fields = [w["field"] for w in result["warnings"]]
        assert "facility" in warning_fields
        assert "queueid" in warning_fields
        assert "message_id" in warning_fields

    def test_reports_configured_fields(self):
        """Reports which fields are configured."""
        config = self._make_config()
        result = check_config(config)
        configured = result["configured_fields"]
        assert "hostname" in configured
        assert "message" in configured
        assert "timestamp" in configured

    def test_reports_unconfigured_fields(self):
        """Reports which fields are not configured."""
        config = self._make_config(facility=None)
        result = check_config(config)
        assert "facility" in result["unconfigured_fields"]

    def test_method_reported(self):
        """Reports the configured method."""
        config = self._make_config()
        result = check_config(config)
        assert result["method"] == "opensearch"


class TestDoctorCLI:
    """Tests for the doctor CLI command."""

    @patch("mailtrace.__main__.load_config")
    def test_doctor_command_exists(self, mock_load):
        """Doctor command is registered and shows help."""
        runner = CliRunner()
        result = runner.invoke(cli, ["doctor", "--help"])
        assert result.exit_code == 0
        assert "config" in result.output.lower()

    @patch("mailtrace.__main__.load_config")
    def test_doctor_runs_successfully(self, mock_load):
        """Doctor command runs and prints output."""
        mock_load.return_value = Config(
            method=Method.OPENSEARCH,
            log_level="INFO",
            ssh_config=SSHConfig(username="dummy", password="dummy"),
            opensearch_config=OpenSearchConfig(
                host="localhost",
                port=9200,
                username="admin",
                password="admin",
            ),
        )
        runner = CliRunner()
        result = runner.invoke(cli, ["doctor", "-c", "dummy"])
        assert result.exit_code == 0

    @patch("mailtrace.__main__.load_config")
    def test_doctor_reports_warnings(self, mock_load):
        """Doctor command reports warnings for missing fields."""
        mapping = OpenSearchMappingConfig(
            facility=None,
            queueid=None,
        )
        mock_load.return_value = Config(
            method=Method.OPENSEARCH,
            log_level="INFO",
            ssh_config=SSHConfig(username="dummy", password="dummy"),
            opensearch_config=OpenSearchConfig(
                host="localhost",
                port=9200,
                username="admin",
                password="admin",
                mapping=mapping,
            ),
        )
        runner = CliRunner()
        result = runner.invoke(cli, ["doctor", "-c", "dummy"])
        assert result.exit_code == 0
        assert "facility" in result.output
        assert "queueid" in result.output
