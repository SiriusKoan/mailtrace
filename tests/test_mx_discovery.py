"""Tests for MX auto-discovery functionality."""

import logging
import os
import tempfile
import time
from unittest.mock import MagicMock, patch

import dns.resolver
import pytest

from mailtrace.config import (
    Config,
    Method,
    MXDiscoveryConfig,
    OpenSearchConfig,
    SSHConfig,
)
from mailtrace.mx_discovery import MXDiscovery


class TestMXDiscoveryExpandEntry:
    """Tests for MXDiscovery.expand_entry method."""

    def test_static_entry_passes_through(self):
        """Non-mx: entries pass through unchanged."""
        config = MXDiscoveryConfig()
        discovery = MXDiscovery(config)

        result = discovery.expand_entry("mail.example.com")

        assert result == ["mail.example.com"]

    def test_static_entry_with_ip(self):
        """IP addresses pass through unchanged."""
        config = MXDiscoveryConfig()
        discovery = MXDiscovery(config)

        result = discovery.expand_entry("192.168.1.1")

        assert result == ["192.168.1.1"]


class TestMXDiscoveryConfig:
    """Tests for MXDiscoveryConfig dataclass."""

    def test_default_values(self):
        """MXDiscoveryConfig has sensible defaults."""
        config = MXDiscoveryConfig()
        assert config.servers == []
        assert config.timeout == 5
        assert config.cache_ttl == 0

    def test_custom_values(self):
        """MXDiscoveryConfig accepts custom values."""
        config = MXDiscoveryConfig(
            servers=["8.8.8.8", "1.1.1.1"],
            timeout=10,
            cache_ttl=300,
        )
        assert config.servers == ["8.8.8.8", "1.1.1.1"]
        assert config.timeout == 10
        assert config.cache_ttl == 300


class TestMXDiscoveryResolve:
    """Tests for MXDiscovery.resolve method."""

    def test_resolve_returns_mx_hostnames(self):
        """Successful MX lookup returns list of hostnames."""
        config = MXDiscoveryConfig()
        discovery = MXDiscovery(config)

        # Mock MX record response
        mock_mx1 = MagicMock()
        mock_mx1.exchange = "mx1.example.com."
        mock_mx2 = MagicMock()
        mock_mx2.exchange = "mx2.example.com."

        with patch.object(dns.resolver.Resolver, "resolve") as mock_resolve:
            mock_resolve.return_value = [mock_mx1, mock_mx2]

            result = discovery.resolve("example.com")

        assert result == ["mx1.example.com", "mx2.example.com"]

    def test_expand_mx_entry_resolves(self):
        """mx: prefixed entries trigger MX resolution."""
        config = MXDiscoveryConfig()
        discovery = MXDiscovery(config)

        mock_mx = MagicMock()
        mock_mx.exchange = "mail.example.com."

        with patch.object(dns.resolver.Resolver, "resolve") as mock_resolve:
            mock_resolve.return_value = [mock_mx]

            result = discovery.expand_entry("mx:example.com")

        assert result == ["mail.example.com"]


class TestMXDiscoveryErrors:
    """Tests for MXDiscovery error handling."""

    @pytest.fixture(autouse=True)
    def _enable_log_propagation(self):
        """Ensure mailtrace logger propagates so caplog can capture records."""
        mailtrace_logger = logging.getLogger("mailtrace")
        original = mailtrace_logger.propagate
        mailtrace_logger.propagate = True
        yield
        mailtrace_logger.propagate = original

    def test_timeout_returns_empty_and_logs_warning(self, caplog):
        """DNS timeout logs warning and returns empty list."""
        config = MXDiscoveryConfig(timeout=1)
        discovery = MXDiscovery(config)

        with caplog.at_level(logging.WARNING):
            with patch.object(dns.resolver.Resolver, "resolve") as mock_resolve:
                mock_resolve.side_effect = dns.resolver.Timeout()

                result = discovery.resolve("example.com")

        assert result == []
        assert "MX lookup failed for example.com" in caplog.text
        assert "Timeout" in caplog.text

    def test_nxdomain_returns_empty_and_logs_warning(self, caplog):
        """NXDOMAIN logs warning and returns empty list."""
        config = MXDiscoveryConfig()
        discovery = MXDiscovery(config)

        with caplog.at_level(logging.WARNING):
            with patch.object(dns.resolver.Resolver, "resolve") as mock_resolve:
                mock_resolve.side_effect = dns.resolver.NXDOMAIN()

                result = discovery.resolve("nonexistent.invalid")

        assert result == []
        assert "MX lookup failed for nonexistent.invalid" in caplog.text
        assert "NXDOMAIN" in caplog.text

    def test_no_answer_returns_empty_and_logs_warning(self, caplog):
        """No MX records logs warning and returns empty list."""
        config = MXDiscoveryConfig()
        discovery = MXDiscovery(config)

        with caplog.at_level(logging.WARNING):
            with patch.object(dns.resolver.Resolver, "resolve") as mock_resolve:
                mock_resolve.side_effect = dns.resolver.NoAnswer()

                result = discovery.resolve("example.com")

        assert result == []
        assert "No MX records found for example.com" in caplog.text


class TestMXDiscoveryCustomServers:
    """Tests for custom DNS server configuration."""

    def test_custom_servers_used(self):
        """Custom DNS servers are set on resolver."""
        config = MXDiscoveryConfig(servers=["8.8.8.8", "1.1.1.1"])
        discovery = MXDiscovery(config)

        mock_mx = MagicMock()
        mock_mx.exchange = "mx.example.com."

        with patch(
            "mailtrace.mx_discovery.dns.resolver.Resolver"
        ) as MockResolver:
            mock_resolver = MagicMock()
            mock_resolver.resolve.return_value = [mock_mx]
            MockResolver.return_value = mock_resolver

            discovery.resolve("example.com")

        assert mock_resolver.nameservers == ["8.8.8.8", "1.1.1.1"]

    def test_system_resolver_when_no_servers(self):
        """Empty servers list uses system resolver (nameservers not set)."""
        config = MXDiscoveryConfig(servers=[])
        discovery = MXDiscovery(config)

        mock_mx = MagicMock()
        mock_mx.exchange = "mx.example.com."

        with patch(
            "mailtrace.mx_discovery.dns.resolver.Resolver"
        ) as MockResolver:
            mock_resolver = MagicMock()
            mock_resolver.resolve.return_value = [mock_mx]
            MockResolver.return_value = mock_resolver

            result = discovery.resolve("example.com")

        # When servers=[], the code doesn't set nameservers attribute
        # Verify nameservers was never assigned (no setattr call for nameservers)
        nameserver_calls = [
            call
            for call in mock_resolver.mock_calls
            if "nameservers" in str(call)
        ]
        assert len(nameserver_calls) == 0
        # Also verify the resolution succeeded
        assert result == ["mx.example.com"]


class TestMXDiscoveryCaching:
    """Tests for MXDiscovery caching behavior."""

    def test_cache_disabled_always_queries(self):
        """With cache_ttl=0, always queries DNS."""
        config = MXDiscoveryConfig(cache_ttl=0)
        discovery = MXDiscovery(config)

        mock_mx = MagicMock()
        mock_mx.exchange = "mx.example.com."

        with patch.object(dns.resolver.Resolver, "resolve") as mock_resolve:
            mock_resolve.return_value = [mock_mx]

            discovery.resolve("example.com")
            discovery.resolve("example.com")

        assert mock_resolve.call_count == 2

    def test_cache_enabled_reuses_result(self):
        """With cache_ttl>0, reuses cached result."""
        config = MXDiscoveryConfig(cache_ttl=300)
        discovery = MXDiscovery(config)

        mock_mx = MagicMock()
        mock_mx.exchange = "mx.example.com."

        with patch.object(dns.resolver.Resolver, "resolve") as mock_resolve:
            mock_resolve.return_value = [mock_mx]

            result1 = discovery.resolve("example.com")
            result2 = discovery.resolve("example.com")

        assert mock_resolve.call_count == 1
        assert result1 == result2 == ["mx.example.com"]

    def test_cache_expired_queries_again(self):
        """Expired cache triggers fresh DNS query."""
        config = MXDiscoveryConfig(cache_ttl=1)
        discovery = MXDiscovery(config)

        mock_mx = MagicMock()
        mock_mx.exchange = "mx.example.com."

        with patch.object(dns.resolver.Resolver, "resolve") as mock_resolve:
            mock_resolve.return_value = [mock_mx]

            discovery.resolve("example.com")

            # Simulate cache expiry by manipulating internal cache
            domain_cache = discovery._cache["example.com"]
            discovery._cache["example.com"] = (
                domain_cache[0],
                time.time() - 10,
            )

            discovery.resolve("example.com")

        assert mock_resolve.call_count == 2


class TestConfigMXDiscoveryIntegration:
    """Tests for Config integration with MX discovery."""

    def test_config_has_mx_discovery_with_defaults(self):
        """Config includes mx_discovery field with defaults."""
        config = Config(
            method=Method.SSH,
            log_level="INFO",
            ssh_config=SSHConfig(username="test", password="test"),
            opensearch_config=OpenSearchConfig(),
        )

        assert hasattr(config, "mx_discovery")
        assert config.mx_discovery.servers == []
        assert config.mx_discovery.timeout == 5
        assert config.mx_discovery.cache_ttl == 0

    def test_config_accepts_mx_discovery_dict(self):
        """Config converts mx_discovery dict to MXDiscoveryConfig."""
        config = Config(
            method=Method.SSH,
            log_level="INFO",
            ssh_config=SSHConfig(username="test", password="test"),
            opensearch_config=OpenSearchConfig(),
            mx_discovery={"servers": ["8.8.8.8"], "timeout": 10},
        )

        assert config.mx_discovery.servers == ["8.8.8.8"]
        assert config.mx_discovery.timeout == 10


class TestClusterToHostsWithMX:
    """Tests for cluster_to_hosts MX expansion."""

    def test_static_cluster_unchanged(self):
        """Static cluster entries work as before."""
        config = Config(
            method=Method.SSH,
            log_level="INFO",
            ssh_config=SSHConfig(username="test", password="test"),
            opensearch_config=OpenSearchConfig(),
            clusters={
                "my-cluster": ["host1.example.com", "host2.example.com"]
            },
            domain="example.com",
        )

        result = config.cluster_to_hosts("my-cluster")

        assert "host1.example.com" in result
        assert "host2.example.com" in result

    def test_mx_entry_expanded(self):
        """mx: prefixed entries are expanded via DNS."""
        config = Config(
            method=Method.SSH,
            log_level="INFO",
            ssh_config=SSHConfig(username="test", password="test"),
            opensearch_config=OpenSearchConfig(),
            clusters={"mx-cluster": ["mx:example.com"]},
            domain="example.com",
        )

        mock_mx = MagicMock()
        mock_mx.exchange = "mail.example.com."

        with patch.object(dns.resolver.Resolver, "resolve") as mock_resolve:
            mock_resolve.return_value = [mock_mx]

            result = config.cluster_to_hosts("mx-cluster")

        assert "mail.example.com" in result

    def test_mixed_cluster_merged(self):
        """Mixed static and mx: entries are merged."""
        config = Config(
            method=Method.SSH,
            log_level="INFO",
            ssh_config=SSHConfig(username="test", password="test"),
            opensearch_config=OpenSearchConfig(),
            clusters={"hybrid": ["mx:example.com", "static.example.com"]},
            domain="example.com",
        )

        mock_mx = MagicMock()
        mock_mx.exchange = "mx.example.com."

        with patch.object(dns.resolver.Resolver, "resolve") as mock_resolve:
            mock_resolve.return_value = [mock_mx]

            result = config.cluster_to_hosts("hybrid")

        assert "mx.example.com" in result
        assert "static.example.com" in result

    def test_nonexistent_cluster_returns_none(self):
        """Nonexistent cluster returns None."""
        config = Config(
            method=Method.SSH,
            log_level="INFO",
            ssh_config=SSHConfig(username="test", password="test"),
            opensearch_config=OpenSearchConfig(),
        )

        result = config.cluster_to_hosts("nonexistent")

        assert result is None


class TestConfigLoadingWithMXDiscovery:
    """Tests for loading config with mx_discovery from YAML."""

    def test_load_config_with_mx_discovery(self):
        """Config loads mx_discovery settings from YAML."""
        from mailtrace.config import load_config

        config_yaml = """
method: ssh
log_level: INFO
ssh_config:
  username: test
  password: testpass
opensearch_config: {}
mx_discovery:
  servers:
    - 8.8.8.8
  timeout: 10
  cache_ttl: 60
clusters:
  test-cluster:
    - mx:example.com
    - static.example.com
domain: example.com
"""
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False
        ) as f:
            f.write(config_yaml)
            f.flush()

            try:
                config = load_config(f.name)

                assert config.mx_discovery.servers == ["8.8.8.8"]
                assert config.mx_discovery.timeout == 10
                assert config.mx_discovery.cache_ttl == 60
            finally:
                os.unlink(f.name)

    def test_load_config_without_mx_discovery_uses_defaults(self):
        """Config without mx_discovery section uses defaults."""
        from mailtrace.config import load_config

        config_yaml = """
method: ssh
log_level: INFO
ssh_config:
  username: test
  password: testpass
opensearch_config: {}
"""
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False
        ) as f:
            f.write(config_yaml)
            f.flush()

            try:
                config = load_config(f.name)

                assert config.mx_discovery.servers == []
                assert config.mx_discovery.timeout == 5
                assert config.mx_discovery.cache_ttl == 0
            finally:
                os.unlink(f.name)
