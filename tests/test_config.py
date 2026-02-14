"""Tests for config validation of nullable mapping fields."""

import logging

import pytest

from mailtrace.config import OpenSearchMappingConfig


class TestOpenSearchMappingConfigRequired:
    """Tests for required field validation."""

    def test_default_mapping_is_valid(self):
        """Default config with all defaults should pass validation."""
        mapping = OpenSearchMappingConfig()
        assert mapping.timestamp == "@timestamp"
        assert mapping.message == "message"
        assert mapping.hostname == "host.name"

    def test_required_field_timestamp_none_raises(self):
        """Setting timestamp to None should raise ValueError."""
        with pytest.raises(ValueError, match="timestamp"):
            OpenSearchMappingConfig(timestamp=None)

    def test_required_field_message_none_raises(self):
        """Setting message to None should raise ValueError."""
        with pytest.raises(ValueError, match="message"):
            OpenSearchMappingConfig(message=None)

    def test_required_field_hostname_none_raises(self):
        """Setting hostname to None should raise ValueError."""
        with pytest.raises(ValueError, match="hostname"):
            OpenSearchMappingConfig(hostname=None)

    def test_required_field_empty_string_raises(self):
        """Setting a required field to empty string should raise ValueError."""
        with pytest.raises(ValueError, match="timestamp"):
            OpenSearchMappingConfig(timestamp="")


class TestOpenSearchMappingConfigOptional:
    """Tests for optional field handling."""

    def test_optional_fields_accept_none(self):
        """Optional fields should accept None without error."""
        mapping = OpenSearchMappingConfig(
            facility=None,
            service=None,
            queueid=None,
            queued_as=None,
            mail_id=None,
            message_id=None,
            relay_host=None,
            relay_ip=None,
            relay_port=None,
            smtp_code=None,
        )
        assert mapping.facility is None
        assert mapping.queueid is None

    def test_optional_fields_accept_string(self):
        """Optional fields should accept string values."""
        mapping = OpenSearchMappingConfig(
            facility="custom.facility",
            queueid="custom.queueid",
        )
        assert mapping.facility == "custom.facility"
        assert mapping.queueid == "custom.queueid"

    def test_all_optional_fields_none_by_default(self):
        """All optional fields default to None."""
        mapping = OpenSearchMappingConfig()
        assert mapping.facility is None
        assert mapping.service is None
        assert mapping.queueid is None
        assert mapping.queued_as is None
        assert mapping.mail_id is None
        assert mapping.message_id is None
        assert mapping.relay_host is None
        assert mapping.relay_ip is None
        assert mapping.relay_port is None
        assert mapping.smtp_code is None


class TestOpenSearchMappingConfigWarnings:
    """Tests for warning logs on missing nice-to-have fields."""

    def test_warns_on_missing_facility(self, caplog):
        """Missing facility field logs a warning."""
        with caplog.at_level(logging.WARNING, logger="mailtrace"):
            OpenSearchMappingConfig(facility=None)
        assert "facility" in caplog.text

    def test_warns_on_missing_queueid(self, caplog):
        """Missing queueid field logs a warning."""
        with caplog.at_level(logging.WARNING, logger="mailtrace"):
            OpenSearchMappingConfig(queueid=None)
        assert "queueid" in caplog.text

    def test_warns_on_missing_message_id(self, caplog):
        """Missing message_id field logs a warning."""
        with caplog.at_level(logging.WARNING, logger="mailtrace"):
            OpenSearchMappingConfig(message_id=None)
        assert "message_id" in caplog.text

    def test_warns_on_missing_service(self, caplog):
        """Missing service field logs a warning."""
        with caplog.at_level(logging.WARNING, logger="mailtrace"):
            OpenSearchMappingConfig(service=None)
        assert "service" in caplog.text

    def test_no_warning_when_fields_configured(self, caplog):
        """No warnings when all nice-to-have fields are configured."""
        with caplog.at_level(logging.WARNING, logger="mailtrace"):
            OpenSearchMappingConfig(
                facility="log.syslog.facility.name",
                service="log.syslog.appname",
                queueid="postfix.queueid.keyword",
                message_id="postfix.message_id",
            )
        assert caplog.text == ""

    def test_dict_construction_with_nullable_fields(self):
        """Config constructed from dict (YAML path) handles None."""
        from mailtrace.config import OpenSearchConfig

        os_config = OpenSearchConfig(
            host="localhost",
            port=9200,
            username="admin",
            password="admin",
            mapping={
                "hostname": "host.name",
                "message": "message",
                "timestamp": "@timestamp",
                "facility": None,
                "queueid": None,
            },
        )
        assert isinstance(os_config.mapping, OpenSearchMappingConfig)
        assert os_config.mapping.facility is None
        assert os_config.mapping.queueid is None
        assert os_config.mapping.hostname == "host.name"
