from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import yaml
from dotenv import load_dotenv

from mailtrace.utils import get_hosts

if TYPE_CHECKING:
    from mailtrace.mx_discovery import MXDiscovery

# Valid log levels for configuration
VALID_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}


class Method(Enum):
    """Enumeration of supported connection methods for log collection."""

    SSH = "ssh"
    OPENSEARCH = "opensearch"


@dataclass
class HostConfig:
    """Configuration for host-specific log settings."""

    log_files: list[str] = field(default_factory=list)
    log_parser: str = "SyslogParser"
    time_format: str = "%Y-%m-%d %H:%M:%S"

    def __post_init__(self) -> None:
        # Lazy import to avoid circular dependency
        from mailtrace.parser import PARSERS

        if self.log_parser not in PARSERS:
            raise ValueError(f"Invalid log parser: {self.log_parser}")


@dataclass
class SSHConfig:
    """Configuration for SSH connections.

    Attributes:
        username: SSH username for authentication
        password: SSH password (alternative to private_key)
        private_key: Path to SSH private key file (alternative to password)
        sudo_pass: Password for sudo operations
        sudo: Whether to use sudo for log file access
        timeout: SSH connection timeout in seconds
        ssh_config_file: Path to SSH config file (e.g., ~/.ssh/config or custom)
        host_config: Default host configuration for log files
        hosts: Dictionary mapping hostnames to their specific configurations
    """

    username: str = ""
    password: str = ""
    private_key: str = ""
    sudo_pass: str = ""
    sudo: bool = True
    timeout: int = 10
    ssh_config_file: str = ""
    host_config: HostConfig = field(default_factory=HostConfig)
    hosts: dict[str, HostConfig] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.username:
            raise ValueError("Username must be provided")
        if not self.password and not self.private_key:
            raise ValueError("Either password or private_key must be provided")

        # Convert dict to HostConfig if needed
        if isinstance(self.host_config, dict):
            self.host_config = HostConfig(**self.host_config)
        for hostname, host_config in self.hosts.items():
            if isinstance(host_config, dict):
                self.hosts[hostname] = HostConfig(**host_config)

    def get_host_config(self, hostname: str) -> HostConfig:
        """Get effective configuration for a specific host.

        Merges host-specific configuration with default configuration,
        with host-specific values taking precedence.
        """
        host_config = self.hosts.get(hostname, self.host_config)
        return HostConfig(
            log_files=host_config.log_files or self.host_config.log_files,
            log_parser=host_config.log_parser or self.host_config.log_parser,
            time_format=host_config.time_format
            or self.host_config.time_format,
        )


@dataclass
class OpenSearchMappingConfig:
    """Mapping of application field names to OpenSearch field names.

    Attributes:
        facility: OpenSearch field for log facility
        hostname: OpenSearch field for host name
        message: OpenSearch field for log message
        timestamp: OpenSearch field for log timestamp
        service: OpenSearch field for service name
        queueid: OpenSearch field for queue ID
        queued_as: OpenSearch field for queued as
        mail_id: OpenSearch field for mail ID
        message_id: OpenSearch field for RFC 2822 Message-ID header
        relay_host: OpenSearch field for relay hostname
        relay_ip: OpenSearch field for relay IP address
        relay_port: OpenSearch field for relay port number
        smtp_code: OpenSearch field for SMTP return code
    """

    facility: str = "log.syslog.facility.name"
    hostname: str = "host.name"
    message: str = "message"
    timestamp: str = "@timestamp"
    service: str = "log.syslog.appname"
    queueid: str = "log.syslog.structured_data.queueid"
    queued_as: str = "log.syslog.structured_data.queued_as"
    mail_id: str = ""
    message_id: str = ""
    relay_host: str = ""
    relay_ip: str = ""
    relay_port: str = ""
    smtp_code: str = ""


@dataclass
class MXDiscoveryConfig:
    """Configuration for MX record auto-discovery.

    Attributes:
        servers: List of DNS server IPs to query (empty = system resolver)
        timeout: DNS query timeout in seconds
        cache_ttl: Cache TTL in seconds (0 = no cache)
    """

    servers: list[str] = field(default_factory=list)
    timeout: int = 5
    cache_ttl: int = 0


@dataclass
class OpenSearchConfig:
    """Configuration for OpenSearch connections.

    Attributes:
        host: OpenSearch host address
        port: OpenSearch port number
        username: Username for OpenSearch authentication
        password: Password for OpenSearch authentication
        use_ssl: Whether to use SSL/TLS encryption
        verify_certs: Whether to verify SSL certificates
        index: OpenSearch index name for log storage
        time_zone: Timezone offset for log timestamps
        mapping: Mapping of application field names to OpenSearch field names
    """

    host: str = ""
    port: int = 9200
    username: str = ""
    password: str = ""
    use_ssl: bool = False
    verify_certs: bool = False
    index: str = ""
    time_zone: str = "+00:00"
    timeout: int = 10
    mapping: OpenSearchMappingConfig = field(
        default_factory=OpenSearchMappingConfig
    )

    def __post_init__(self) -> None:
        # Convert dict mapping to OpenSearchMappingConfig if needed
        if isinstance(self.mapping, dict):
            self.mapping = OpenSearchMappingConfig(**self.mapping)


@dataclass
class Config:
    """Main configuration class for the mail tracing application.

    Attributes:
        method: Connection method to use for log collection
        log_level: Logging level for the application
        ssh_config: SSH connection configuration
        opensearch_config: OpenSearch connection configuration
        clusters: Dictionary mapping cluster names to lists of host names for HA
        domain: Domain name for hostname resolution
        auto_continue: Whether to automatically continue tracing to next hop without user input
    """

    method: Method
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
    ssh_config: SSHConfig
    opensearch_config: OpenSearchConfig
    clusters: dict[str, list[str]] = field(default_factory=dict)
    domain: str = ""
    auto_continue: bool = False
    mx_discovery: MXDiscoveryConfig = field(default_factory=MXDiscoveryConfig)
    _mx_resolver: MXDiscovery | None = field(
        default=None, init=False, repr=False
    )

    def __post_init__(self) -> None:
        # Validate log level
        if self.log_level not in VALID_LOG_LEVELS:
            raise ValueError(f"Invalid log level: {self.log_level}")

        # Convert string method to enum if needed
        if isinstance(self.method, str):
            try:
                self.method = Method(self.method)
            except ValueError:
                raise ValueError(f"Invalid method: {self.method}")

        # Convert dicts to config objects if needed
        if isinstance(self.ssh_config, dict):
            self.ssh_config = SSHConfig(**self.ssh_config)
        if isinstance(self.opensearch_config, dict):
            self.opensearch_config = OpenSearchConfig(**self.opensearch_config)
        if isinstance(self.mx_discovery, dict):
            self.mx_discovery = MXDiscoveryConfig(**self.mx_discovery)

        # Initialize MX resolver
        from mailtrace.mx_discovery import MXDiscovery

        self._mx_resolver = MXDiscovery(self.mx_discovery)

    def cluster_to_hosts(self, name: str) -> list[str] | None:
        """Get list of hosts for a given cluster name, expanding mx: entries."""
        raw_entries = self.clusters.get(name)
        if raw_entries is None:
            return None

        # Expand mx: entries and merge
        hosts: list[str] = []
        assert self._mx_resolver is not None  # Always set in __post_init__
        for entry in raw_entries:
            hosts.extend(self._mx_resolver.expand_entry(entry))

        # Apply hostname expansion, deduplicate preserving order
        return list(dict.fromkeys(get_hosts(hosts, self.domain)))


def _load_env_passwords(config_data: dict) -> None:
    """Load passwords from environment variables if not provided in config."""
    # OpenSearch password
    if "opensearch_config" in config_data:
        os_config = config_data["opensearch_config"]
        if not os_config.get("password"):
            os_config["password"] = os.getenv(
                "MAILTRACE_OPENSEARCH_PASSWORD", ""
            )

    # SSH passwords
    if "ssh_config" in config_data:
        ssh_config = config_data["ssh_config"]
        if not ssh_config.get("password"):
            ssh_config["password"] = os.getenv("MAILTRACE_SSH_PASSWORD", "")
        if not ssh_config.get("sudo_pass"):
            ssh_config["sudo_pass"] = os.getenv("MAILTRACE_SUDO_PASSWORD", "")


def load_config(config_path: str | None = None) -> Config:
    """Load configuration from YAML file.

    Uses MAILTRACE_CONFIG environment variable or 'config.yaml' as default path.
    Passwords can be provided via environment variables if not in the config file.

    Args:
        config_path: Optional path to configuration file. If not provided, uses
            MAILTRACE_CONFIG environment variable or 'config.yaml' as default.

    Returns:
        Config: The loaded configuration object

    Raises:
        FileNotFoundError: If the configuration file doesn't exist
        ValueError: If the configuration file contains invalid data
    """
    config_path = config_path or os.getenv("MAILTRACE_CONFIG", "config.yaml")
    if not os.path.exists(config_path):
        raise FileNotFoundError(f"Config file not found: {config_path}")

    # Load .env file from the same directory as the config file.
    # override=False ensures shell env vars take precedence over .env.
    dotenv_path = Path(config_path).resolve().parent / ".env"
    load_dotenv(dotenv_path=dotenv_path, override=False)

    with open(config_path) as f:
        config_data = yaml.safe_load(f)

    _load_env_passwords(config_data)

    try:
        return Config(**config_data)
    except Exception as e:
        raise ValueError(f"Error loading config: {e}") from e
