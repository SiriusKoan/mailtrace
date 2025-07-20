import os
from dataclasses import dataclass, field
from enum import Enum
from typing import Literal

import yaml

from .parser import PARSERS


class Method(Enum):
    SSH = "ssh"
    OPENSEARCH = "opensearch"
    LOGHOST = "loghost"


@dataclass
class SSHConfig:
    username: str = ""
    password: str = ""
    private_key: str = ""
    sudo_pass: str = ""
    sudo: bool = True

    def __post_init__(self):
        if not self.username:
            raise ValueError("Username must be provided")
        if not self.password and not self.private_key:
            raise ValueError("Either password or private_key must be provided")


@dataclass
class OpensearchConfig:
    host: str = ""
    port: int = 9200
    username: str = ""
    password: str = ""
    use_ssl: bool = False
    verify_certs: bool = False
    index: str = ""


@dataclass
class HostConfig:
    log_files: list[str] = field(default_factory=list)
    log_parser: str = ""
    time_format: str = "%Y-%m-%d %H:%M:%S"

    def __post_init__(self):
        if self.log_parser not in PARSERS:
            raise ValueError(f"Invalid log parser: {self.log_parser}")


@dataclass
class Config:
    method: Method
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
    ssh_config: SSHConfig
    opensearch_config: OpensearchConfig
    host_config: HostConfig
    hosts: dict[str, HostConfig]

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
            self.opensearch_config = OpensearchConfig(**self.opensearch_config)
        if isinstance(self.host_config, dict):
            self.host_config = HostConfig(**self.host_config)
        for hostname, host_config in self.hosts.items():
            if isinstance(host_config, dict):
                self.hosts[hostname] = HostConfig(**host_config)

    def get_host_config(self, hostname: str) -> HostConfig:
        host_config = self.hosts.get(hostname, self.host_config)
        return HostConfig(
            log_files=host_config.log_files or self.host_config.log_files,
            log_parser=host_config.log_parser or self.host_config.log_parser,
            time_format=host_config.time_format
            or self.host_config.time_format,
        )


def load_config():
    config_path = os.getenv("MAILTRACE_CONFIG", "config.yaml")
    if not os.path.exists(config_path):
        raise FileNotFoundError(f"Config file not found: {config_path}")
    with open(config_path) as f:
        config_data = yaml.safe_load(f)
    try:
        return Config(**config_data)
    except Exception as e:
        raise ValueError(f"Error loading config: {e}") from e
