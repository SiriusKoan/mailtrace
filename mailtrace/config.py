import os
from dataclasses import dataclass, field
from enum import Enum
from typing import Literal

import yaml

from .parser import PARSERS


class Method(Enum):
    """Enumeration of supported connection methods for log collection."""

    SSH = "ssh"
    OPENSEARCH = "opensearch"


@dataclass
class HostConfig:
    """Configuration for host-specific log settings.

    Attributes:
        log_files: List of log file paths to monitor
        log_parser: Parser type to use for log processing
        time_format: Time format string for parsing timestamps
    """

    log_files: list[str] = field(default_factory=list)
    log_parser: str = ""
    time_format: str = "%Y-%m-%d %H:%M:%S"

    def __post_init__(self):
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
    """

    username: str = ""
    password: str = ""
    private_key: str = ""
    sudo_pass: str = ""
    sudo: bool = True
    host_config: HostConfig = field(default_factory=HostConfig)
    hosts: dict[str, HostConfig] = field(default_factory=dict)

    def __post_init__(self):
        if not self.username:
            raise ValueError("Username must be provided")
        if not self.password and not self.private_key:
            raise ValueError("Either password or private_key must be provided")
        if isinstance(self.host_config, dict):
            self.host_config = HostConfig(**self.host_config)
        for hostname, host_config in self.hosts.items():
            if isinstance(host_config, dict):
                self.hosts[hostname] = HostConfig(**host_config)

    def get_host_config(self, hostname: str) -> HostConfig:
        """Get effective configuration for a specific host.

        Merges host-specific configuration with default configuration,
        with host-specific values taking precedence.

        Args:
            hostname: Name of the host to get configuration for

        Returns:
            HostConfig object with merged configuration
        """

        host_config = self.hosts.get(hostname, self.host_config)
        return HostConfig(
            log_files=host_config.log_files or self.host_config.log_files,
            log_parser=host_config.log_parser or self.host_config.log_parser,
            time_format=host_config.time_format
            or self.host_config.time_format,
        )


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
    """

    host: str = ""
    port: int = 9200
    username: str = ""
    password: str = ""
    use_ssl: bool = False
    verify_certs: bool = False
    index: str = ""
    time_zone: str = "+00:00"


@dataclass
class Config:
    """Main configuration class for the mail tracing application.

    Attributes:
        method: Connection method to use for log collection
        log_level: Logging level for the application
        ssh_config: SSH connection configuration
        opensearch_config: OpenSearch connection configuration
        host_config: Default host configuration
        hosts: Per-host configuration overrides
    """

    method: Method
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
    ssh_config: SSHConfig
    opensearch_config: OpenSearchConfig

    def __post_init__(self):
        # value checking
        if self.log_level not in [
            "DEBUG",
            "INFO",
            "WARNING",
            "ERROR",
            "CRITICAL",
        ]:
            raise ValueError(f"Invalid log level: {self.log_level}")
        if self.method not in [method.value for method in Method]:
            raise ValueError(f"Invalid method: {self.method}")
        # type checking
        if isinstance(self.method, str):
            self.method = Method(self.method)
        if isinstance(self.ssh_config, dict):
            self.ssh_config = SSHConfig(**self.ssh_config)
        if isinstance(self.opensearch_config, dict):
            self.opensearch_config = OpenSearchConfig(**self.opensearch_config)


def load_config(config_path: str | None = None):
    """Load configuration from YAML file.

    Loads configuration from the file specified by MAILTRACE_CONFIG
    environment variable, or 'config.yaml' if not set.

    Returns:
        Config object with loaded configuration

    Raises:
        FileNotFoundError: If the configuration file doesn't exist
        ValueError: If the configuration file contains invalid data
    """

    config_path = config_path or os.getenv("MAILTRACE_CONFIG", "config.yaml")
    if not os.path.exists(config_path):
        raise FileNotFoundError(f"Config file not found: {config_path}")
    with open(config_path) as f:
        config_data = yaml.safe_load(f)
    try:
        return Config(**config_data)
    except Exception as e:
        raise ValueError(f"Error loading config: {e}") from e
