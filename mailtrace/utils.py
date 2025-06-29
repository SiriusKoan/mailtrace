import datetime
import re
from dataclasses import dataclass, field
from enum import Enum

import paramiko

from .config import Config
from .parser import PARSERS, LogEntry


def time_range_to_timedelta(time_range: str) -> datetime.timedelta:
    if time_range.endswith("d"):
        return datetime.timedelta(days=int(time_range[:-1]))
    if time_range.endswith("h"):
        return datetime.timedelta(hours=int(time_range[:-1]))
    if time_range.endswith("m"):
        return datetime.timedelta(minutes=int(time_range[:-1]))
    raise ValueError("Invalid time range")


class PostfixServiceType(Enum):
    SMTP = "postfix/smtp"
    LMTP = "postfix/lmtp"
    SMTPD = "postfix/smtpd"
    QMGR = "postfix/qmgr"
    CLEANUP = "postfix/cleanup"


@dataclass
class LogQuery:
    keywords: list[str] = field(default_factory=list)
    mail_id: str | None = None
    time: str | None = None
    time_range: str | None = None


class SSHSession:
    def __init__(self, host: str, config: Config):
        self.host = host
        self.config: Config = config
        self.ssh_config = config.ssh_config
        self.host_config = config.get_host_config(host)
        self.client = paramiko.SSHClient()
        self.client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        if self.ssh_config.private_key:
            self.client.connect(
                hostname=self.host,
                username=self.ssh_config.username,
                key_filename=self.ssh_config.private_key,
            )
        else:
            self.client.connect(
                hostname=self.host,
                username=self.ssh_config.username,
                password=self.ssh_config.password,
            )

    def _execute_command(
        self, command: str, sudo: bool = False
    ) -> tuple[str, str]:
        run_with_sudo = sudo or self.ssh_config.sudo
        if run_with_sudo:
            command = f"sudo -S -p '' {command}"
        print(f"Executing command: {command}")
        stdin, stdout, stderr = self.client.exec_command(command)
        if run_with_sudo:
            stdin.write(self.ssh_config.sudo_pass + "\n")
            stdin.flush()
        stdout_content = stdout.read().decode()
        stderr_content = stderr.read().decode().strip()
        return stdout_content, stderr_content

    def _check_file_exists(self, file_path: str) -> bool:
        command = f"stat {file_path}"
        stdout_content, stderr_content = self._execute_command(command)
        return stdout_content != ""

    def _get_read_command(self, query: LogQuery) -> str:
        if query.time and query.time_range:
            # get logs by time
            timestamp = datetime.datetime.strptime(
                query.time, "%Y-%m-%d %H:%M:%S"
            )
            time_range = time_range_to_timedelta(query.time_range)
            start_time = timestamp - time_range
            end_time = timestamp + time_range
            start_time_str = start_time.strftime(
                self.host_config.time_format
            )
            end_time_str = end_time.strftime(
                self.host_config.time_format
            )
            awk_command = f'{{if ($0 >= "{start_time_str}" && $0 <= "{end_time_str}") {{ print $0 }} }}'
            command = f"awk '{awk_command}'"
        else:
            command = "cat"
        return command

    def _get_keyword_command(self, keywords: list[str]) -> str:
        if not keywords:
            return ""
        command = ""
        for keyword in keywords:
            command += f"| grep -iE {keyword}"
        return command

    def query_by(self, query: LogQuery) -> list[LogEntry]:
        logs: str = ""
        command = self._get_read_command(query)
        for log_file in self.host_config.log_files:
            if not self._check_file_exists(log_file):
                continue
            complete_command = " ".join(
                [command, log_file, self._get_keyword_command(query.keywords)]
            )
            stdout, stderr = self._execute_command(complete_command)
            if stderr:
                raise ValueError(f"Error executing command: {stderr}")
            logs += stdout
        parser = PARSERS[self.host_config.log_parser]()
        parsed_logs = [
            parser.parse(line) for line in logs.splitlines() if line
        ]
        if query.mail_id:
            return [log for log in parsed_logs if log.mail_id == query.mail_id]
        else:
            return parsed_logs

    def close(self):
        self.client.close()


def do_trace(mail_id: str, session: SSHSession) -> tuple[str, str]:
    print(f"Tracing mail ID: {mail_id}")
    log = session.query_by(LogQuery(mail_id=mail_id))
    next_hop: str = ""
    next_mail_id: str = ""
    for entry in log:
        if (entry.service == PostfixServiceType.SMTP.value) or (
            entry.service == PostfixServiceType.LMTP.value
        ):
            msg = entry.message
            match = re.search(r".*([0-9]{3})\s2\.0\.0.*", msg)
            if match:
                code = int(match.group(1))
                if code == 250:
                    mail_id_match = re.search(
                        r"250.*queued as ([0-9A-Z]+).*", msg
                    )
                    if mail_id_match:
                        next_mail_id = mail_id_match.group(1)
                        print(f"Queued as mail ID: {next_mail_id}")
                    relay_match = re.search(
                        r".*relay=([^\s]+)\[([^\]]+)\]:([0-9]+).*", msg
                    )
                    if relay_match:
                        domain = relay_match.group(1)
                        ip = relay_match.group(2)
                        port = relay_match.group(3)
                        print(f"Relay host: {domain}, {ip}, {port}")
                        next_hop = domain
    return next_mail_id, next_hop
