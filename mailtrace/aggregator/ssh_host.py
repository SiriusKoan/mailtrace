import datetime
import logging
import os
import shlex

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
    keywords, and mail IDs.
    """

    def __init__(self, host: str, config: Config):
        """
        Initialize SSH connection to the specified host.

        Args:
            host: The hostname or IP address to connect to
            config: Configuration object
        """

        self.host = host
        self.config: Config = config
        self.ssh_config = config.ssh_config
        self.host_config = self.ssh_config.get_host_config(host)
        self.client = paramiko.SSHClient()

        # SECURITY: Configure SSH host key verification
        known_hosts_file = self.ssh_config.known_hosts_file
        if known_hosts_file:
            # Load known_hosts file for host key verification
            known_hosts_path = os.path.expanduser(known_hosts_file)
            if os.path.exists(known_hosts_path):
                self.client.load_host_keys(known_hosts_path)
                logger.debug("Loaded known_hosts from: %s", known_hosts_path)
            else:
                logger.warning(
                    "known_hosts file not found: %s. "
                    "Host key verification may fail.",
                    known_hosts_path,
                )
            # Reject unknown host keys by default (secure)
            self.client.set_missing_host_key_policy(paramiko.RejectPolicy())
        else:
            # SECURITY WARNING: Host key verification disabled
            logger.warning(
                "SSH host key verification is DISABLED (known_hosts_file is empty). "
                "This is INSECURE and not recommended for production use."
            )
            self.client.set_missing_host_key_policy(paramiko.WarningPolicy())

        # Prepare connection parameters
        connect_params = {
            "hostname": self.host,
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
            ssh_config = paramiko.SSHConfig()
            config_path = os.path.expanduser(self.ssh_config.ssh_config_file)
            try:
                with open(config_path) as f:
                    ssh_config.parse(f)
                logger.debug(f"SSH config file loaded: {config_path}")
            except FileNotFoundError:
                logger.warning(f"SSH config file not found: {config_path}")

            if ssh_host_config := ssh_config.lookup(self.host):
                logger.debug(f"SSH config file found for {self.host}")
                # Merge SSH config settings with our parameters
                # SSH config values take precedence for connection settings
                # ssh_host_config = ssh_config.lookup(self.host)
                # Only override with SSH config if the setting exists there
                if "hostname" in ssh_host_config:
                    connect_params["hostname"] = ssh_host_config["hostname"]
                if "user" in ssh_host_config:
                    connect_params["username"] = ssh_host_config["user"]
                if "port" in ssh_host_config:
                    connect_params["port"] = int(ssh_host_config["port"])
                if "identityfile" in ssh_host_config:
                    connect_params["key_filename"] = ssh_host_config["identityfile"]
            else:
                logger.debug(
                    f"SSH config file not found for {self.host}, using Mailtrace config settings."
                )

        self.client.connect(**connect_params)

    def _execute_command(self, command: str, sudo: bool = False) -> tuple[str, str]:
        """
        Execute a command on the remote host via SSH.

        Args:
            command: The command to execute
            sudo: Whether to run the command with sudo privileges

        Returns:
            A tuple containing (stdout_content, stderr_content)
        """

        run_with_sudo = sudo or self.ssh_config.sudo
        if run_with_sudo:
            command = f"sudo -S -p '' {command}"
        logger.debug(f"Executing command: {command}")
        stdin, stdout, stderr = self.client.exec_command(command)
        if run_with_sudo:
            stdin.write(self.ssh_config.sudo_pass + "\n")
            stdin.flush()
        stdout_content = stdout.read().decode()
        stderr_content = stderr.read().decode().strip()
        return stdout_content, stderr_content

    def _check_file_exists(self, file_path: str) -> bool:
        """
        Check if a file exists on the remote host.

        Args:
            file_path: Path to the file to check

        Returns:
            True if the file exists, False otherwise
        """
        # SECURITY: Escape file path to prevent command injection
        command = f"stat {shlex.quote(file_path)}"
        stdout_content, _ = self._execute_command(command)
        return stdout_content != ""

    def _compose_read_command(self, query: LogQuery) -> str:
        """
        Compose the appropriate command to read log files based on query parameters.

        Args:
            query: LogQuery object containing time and time_range parameters

        Returns:
            Command string
        """

        if query.time and query.time_range:
            # get logs by time
            timestamp = datetime.datetime.strptime(query.time, "%Y-%m-%d %H:%M:%S")
            time_range = time_range_to_timedelta(query.time_range)
            start_time = timestamp - time_range
            end_time = timestamp + time_range
            start_time_str = start_time.strftime(self.host_config.time_format)
            end_time_str = end_time.strftime(self.host_config.time_format)
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

        Note:
            Keywords are shell-escaped using shlex.quote() to prevent command injection.
        """
        if not keywords:
            return ""
        # Combine keywords into a single regex for an OR search, which is more efficient.
        patterns = "|".join(keywords)
        return f"| grep -iE {shlex.quote(patterns)}"

    def query_by(self, query: LogQuery) -> list[LogEntry]:
        """
        Query log files based on the provided query parameters.

        Args:
            query: LogQuery object containing search parameters

        Returns:
            List of LogEntry objects matching the query criteria

        Raises:
            ValueError: If there's an error executing the command on the remote host
        """

        logs: str = ""
        command = self._compose_read_command(query)
        for log_file in self.host_config.log_files:
            if not self._check_file_exists(log_file):
                continue
            # SECURITY: Escape file path to prevent command injection
            complete_command = " ".join(
                [
                    command,
                    shlex.quote(log_file),
                    self._compose_keyword_command(query.keywords),
                ]
            )
            stdout, stderr = self._execute_command(complete_command)
            if stderr:
                raise ValueError(f"Error executing command: {stderr}")
            logs += stdout
        parser = PARSERS[self.host_config.log_parser]()
        parsed_logs = [
            parser.parse_with_enrichment(line)
            for line in logs.splitlines()
            if line
        ]
        if query.mail_id:
            return [log for log in parsed_logs if log.mail_id == query.mail_id]
        return parsed_logs
