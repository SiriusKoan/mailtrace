"""SSH-based log aggregator for the mailtrace application.

This module provides functionality to connect to remote hosts via SSH,
execute log queries, and retrieve log entries for email tracing.
"""

import datetime

import paramiko

from mailtrace.aggregator.base import LogAggregator
from mailtrace.config import Config
from mailtrace.exceptions import SSHCommandError, SSHConnectionError
from mailtrace.log import logger
from mailtrace.models import LogEntry, LogQuery
from mailtrace.parser import PARSERS
from mailtrace.utils import time_range_to_timedelta


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
        self.client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        try:
            if self.ssh_config.private_key:
                logger.debug(f"Connecting to {host} using private key")
                self.client.connect(
                    hostname=self.host,
                    username=self.ssh_config.username,
                    key_filename=self.ssh_config.private_key,
                )
            else:
                logger.debug(f"Connecting to {host} using password")
                self.client.connect(
                    hostname=self.host,
                    username=self.ssh_config.username,
                    password=self.ssh_config.password,
                )
            logger.info(f"Successfully connected to {host}")
        except paramiko.AuthenticationException as e:
            raise SSHConnectionError(
                f"Authentication failed for {host}",
                "Check your SSH credentials (username, password, or private key path)",
            ) from e
        except paramiko.SSHException as e:
            raise SSHConnectionError(
                f"SSH connection failed to {host}: {e}",
                "Verify the host is reachable and SSH service is running",
            ) from e
        except FileNotFoundError as e:
            raise SSHConnectionError(
                f"SSH private key file not found: {self.ssh_config.private_key}",
                "Check the private_key path in your configuration",
            ) from e
        except Exception as e:
            raise SSHConnectionError(
                f"Failed to connect to {host}: {e}",
                "Check network connectivity and SSH configuration",
            ) from e

    def _execute_command(
        self, command: str, sudo: bool = False
    ) -> tuple[str, str]:
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

        command = f"stat {file_path}"
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
            timestamp = datetime.datetime.strptime(
                query.time, "%Y-%m-%d %H:%M:%S"
            )
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
        """

        if not keywords:
            return ""
        return "".join(f"| grep -iE {keyword}" for keyword in keywords)

    def query_by(self, query: LogQuery) -> list[LogEntry]:
        """
        Query log files based on the provided query parameters.

        Args:
            query: LogQuery object containing search parameters

        Returns:
            List of LogEntry objects matching the query criteria
        """
        logs: str = ""
        command = self._compose_read_command(query)

        files_checked = 0
        files_found = 0

        for log_file in self.host_config.log_files:
            files_checked += 1
            if not self._check_file_exists(log_file):
                logger.warning(
                    f"Log file not found on {self.host}: {log_file}"
                )
                continue

            files_found += 1
            complete_command = " ".join(
                [
                    command,
                    log_file,
                    self._compose_keyword_command(query.keywords),
                ]
            )
            try:
                stdout, stderr = self._execute_command(complete_command)
                if stderr:
                    # Some commands may output warnings to stderr that aren't fatal
                    logger.debug(f"Command stderr output: {stderr}")
                    # Only raise if it looks like a real error
                    if "permission denied" in stderr.lower():
                        raise SSHCommandError(
                            f"Permission denied accessing log file: {log_file}",
                            "Try enabling sudo in your SSH configuration or check file permissions",
                        )
                    elif "no such file" in stderr.lower():
                        logger.warning(
                            f"Log file disappeared during query: {log_file}"
                        )
                        continue
                logs += stdout
            except SSHCommandError:
                raise
            except Exception as e:
                raise SSHCommandError(
                    f"Error executing command on {self.host}: {e}",
                    "Check SSH connection and remote system status",
                ) from e

        if files_checked > 0 and files_found == 0:
            logger.warning(
                f"None of the configured log files were found on {self.host}. "
                f"💡 Check the log_files configuration for this host."
            )

        if not logs:
            logger.info(
                f"No logs found matching the query criteria on {self.host}"
            )
            return []

        try:
            parser = PARSERS[self.host_config.log_parser]()
            parsed_logs = [
                parser.parse(line) for line in logs.splitlines() if line
            ]
        except Exception as e:
            raise SSHCommandError(
                f"Failed to parse log entries from {self.host}",
                f"Verify that the log_parser '{self.host_config.log_parser}' is correct for your log format",
            ) from e

        if query.mail_id:
            return [log for log in parsed_logs if log.mail_id == query.mail_id]
        return parsed_logs
