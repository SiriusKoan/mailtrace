from __future__ import annotations
import os
from dataclasses import dataclass, field
from enum import Enum

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
    ssh_config: SSHConfig
    host_config: HostConfig
    hosts: dict[str, HostConfig]

    def __post_init__(self):
        if isinstance(self.method, str):
            self.method = Method(self.method)
        if isinstance(self.ssh_config, dict):
            self.ssh_config = SSHConfig(**self.ssh_config)
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
            time_format=host_config.time_format or self.host_config.time_format
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
