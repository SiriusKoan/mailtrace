import os
from dataclasses import dataclass
from enum import Enum

import yaml

from .parser import PARSERS


class Method(Enum):
    SSH = "ssh"
    OPENSEARCH = "opensearch"
    LOGHOST = "loghost"


@dataclass
class SSHConfig:
    username: str
    password: str
    private_key: str
    sudo_pass: str
    sudo: bool = True

    def __post_init__(self):
        if not self.username:
            raise ValueError("Username must be provided")
        if not self.password and not self.private_key:
            raise ValueError("Either password or private_key must be provided")


@dataclass
class HostConfig:
    log_files: list[str]
    log_parser: str
    time_format: str = "%Y-%m-%d %H:%M:%S"

    def __post_init__(self):
        if self.log_parser not in PARSERS:
            raise ValueError(f"Invalid log parser: {self.log_parser}")


@dataclass
class Config:
    method: Method
    ssh_config: SSHConfig
    host_config: HostConfig

    def __post_init__(self):
        if isinstance(self.method, str):
            self.method = Method(self.method)
        if isinstance(self.ssh_config, dict):
            self.ssh_config = SSHConfig(**self.ssh_config)
        if isinstance(self.host_config, dict):
            self.host_config = HostConfig(**self.host_config)


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
