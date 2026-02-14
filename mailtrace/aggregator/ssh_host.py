import datetime
import logging

import paramiko

from mailtrace.aggregator.base import LogAggregator
from mailtrace.config import Config
from mailtrace.models import LogEntry, LogQuery
from mailtrace.parser import PARSERS
from mailtrace.utils import time_range_to_timedelta

logger = logging.getLogger("mailtrace")


class SSHHost(LogAggregator):
    """
    A log aggregator that connects to remote hosts via SSH to query log files.

    This establishes SSH connections to remote hosts and executes commands
    to read and filter log files based on query parameters such as time ranges,
    keywords, and mail IDs. Can handle a single host or a cluster name that
    resolves to multiple hosts.
    """

    def __init__(self, host: str, config: Config):
        """
        Initialize SSH connection to the specified host or cluster.

        Args:
            host: The hostname, IP address, or cluster name to connect to
            config: Configuration object
        """

        self.config: Config = config
        self.ssh_config = config.ssh_config

        # Resolve cluster name to list of hosts
        self.hosts = config.cluster_to_hosts(host) or [host]
        logger.info(f"SSHHost resolved '{host}' to hosts: {self.hosts}")

        # Create SSH clients for each host
        self._clients: dict[str, paramiko.SSHClient] = {}
        for resolved_host in self.hosts:
            self._clients[resolved_host] = self._create_ssh_client(
                resolved_host
            )

    def _create_ssh_client(self, host: str) -> paramiko.SSHClient:
        """
        Create and connect an SSH client for the given host.

        Args:
            host: The hostname or IP address to connect to

        Returns:
            Connected paramiko SSHClient instance
        """
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.WarningPolicy())

        # Prepare connection parameters
        connect_params = {
            "hostname": host,
            "username": self.ssh_config.username,
            "timeout": self.ssh_config.timeout,
        }

        # Add private key or password
        if self.ssh_config.private_key:
            connect_params["key_filename"] = self.ssh_config.private_key
        else:
            connect_params["password"] = self.ssh_config.password

        # Load and merge SSH config file if specified
        if self.ssh_config.ssh_config_file:
            import os

            ssh_config = paramiko.SSHConfig()
            config_path = os.path.expanduser(self.ssh_config.ssh_config_file)
            try:
                with open(config_path) as f:
                    ssh_config.parse(f)
                logger.debug(f"SSH config file loaded: {config_path}")
            except FileNotFoundError:
                logger.warning(f"SSH config file not found: {config_path}")

            if ssh_host_config := ssh_config.lookup(host):
                logger.debug(f"SSH config file found for {host}")
                # Merge SSH config settings with our parameters
                # SSH config values take precedence for connection settings
                # Only override with SSH config if the setting exists there
                if "hostname" in ssh_host_config:
                    connect_params["hostname"] = ssh_host_config["hostname"]
                if "user" in ssh_host_config:
                    connect_params["username"] = ssh_host_config["user"]
                if "port" in ssh_host_config:
                    connect_params["port"] = int(ssh_host_config["port"])
                if "identityfile" in ssh_host_config:
                    connect_params["key_filename"] = ssh_host_config[
                        "identityfile"
                    ]
            else:
                logger.debug(
                    f"SSH config file not found for {host}, using Mailtrace config settings."
                )

        client.connect(**connect_params)
        return client

    def _execute_command(
        self, host: str, command: str, sudo: bool = False
    ) -> tuple[str, str]:
        """
        Execute a command on the remote host via SSH.

        Args:
            host: The hostname to execute the command on
            command: The command to execute
            sudo: Whether to run the command with sudo privileges

        Returns:
            A tuple containing (stdout_content, stderr_content)
        """

        run_with_sudo = sudo or self.ssh_config.sudo
        if run_with_sudo:
            command = f"sudo -S -p '' {command}"
        logger.debug(f"Executing command on {host}: {command}")
        client = self._clients[host]
        stdin, stdout, stderr = client.exec_command(command)
        if run_with_sudo:
            stdin.write(self.ssh_config.sudo_pass + "\n")
            stdin.flush()
        stdout_content = stdout.read().decode()
        stderr_content = stderr.read().decode().strip()
        return stdout_content, stderr_content

    def _check_file_exists(self, host: str, file_path: str) -> bool:
        """
        Check if a file exists on the remote host.

        Args:
            host: The hostname to check
            file_path: Path to the file to check

        Returns:
            True if the file exists, False otherwise
        """

        command = f"stat {file_path}"
        stdout_content, _ = self._execute_command(host, command)
        return stdout_content != ""

    def _compose_read_command(self, host: str, query: LogQuery) -> str:
        """
        Compose the appropriate command to read log files based on query parameters.

        Args:
            host: The hostname to query
            query: LogQuery object containing time and time_range parameters

        Returns:
            Command string
        """

        host_config = self.ssh_config.get_host_config(host)

        if query.time and query.time_range:
            # get logs by time
            timestamp = datetime.datetime.strptime(
                query.time, "%Y-%m-%d %H:%M:%S"
            )
            time_range = time_range_to_timedelta(query.time_range)
            start_time = timestamp - time_range
            end_time = timestamp + time_range
            start_time_str = start_time.strftime(host_config.time_format)
            end_time_str = end_time.strftime(host_config.time_format)
            awk_command = f'{{if ($0 >= "{start_time_str}" && $0 <= "{end_time_str}") {{ print $0 }} }}'
            command = f"awk '{awk_command}'"
        else:
            command = "cat"
        return command

    @staticmethod
    def _compose_keyword_command(keywords: list[str]) -> str:
        """
        Compose grep commands to filter logs by keywords.

        Args:
            keywords: List of keywords to search for

        Returns:
            String containing chained grep commands or empty string if no keywords
        """

        if not keywords:
            return ""
        return "".join(f"| grep -iE {keyword}" for keyword in keywords)

    def query_by(self, query: LogQuery) -> list[LogEntry]:
        """
        Query log files based on the provided query parameters.

        Queries all resolved hosts and aggregates the results.

        Args:
            query: LogQuery object containing search parameters

        Returns:
            List of LogEntry objects matching the query criteria

        Raises:
            ValueError: If there's an error executing the command on any remote host
        """

        all_parsed_logs: list[LogEntry] = []

        for host in self.hosts:
            logs: str = ""
            host_config = self.ssh_config.get_host_config(host)
            command = self._compose_read_command(host, query)

            for log_file in host_config.log_files:
                if not self._check_file_exists(host, log_file):
                    continue
                complete_command = " ".join(
                    [
                        command,
                        log_file,
                        self._compose_keyword_command(query.keywords),
                    ]
                )
                stdout, stderr = self._execute_command(host, complete_command)
                if stderr:
                    raise ValueError(
                        f"Error executing command on {host}: {stderr}"
                    )
                logs += stdout

            parser = PARSERS[host_config.log_parser]()
            parsed_logs = [
                parser.parse_with_enrichment(line)
                for line in logs.splitlines()
                if line
            ]

            if query.mail_id:
                parsed_logs = [
                    log for log in parsed_logs if log.mail_id == query.mail_id
                ]

            all_parsed_logs.extend(parsed_logs)

        return all_parsed_logs
