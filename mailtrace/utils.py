import datetime
from dataclasses import dataclass, field
from enum import Enum

import paramiko

from .config import Config


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
    time: str | None = None
    time_range: str | None = None


@dataclass
class PostfixLogEntry:
    datetime: str
    hostname: str
    service: str
    message: str


class PostfixLogParser:
    def __init__(self): ...

    def parse(self, log: str) -> PostfixLogEntry:
        datetime, hostname, service, message = log.split(" ", 3)
        service = service.split("[")[0]
        return PostfixLogEntry(datetime, hostname, service, message)


class SSHSession:
    def __init__(self, host, config: Config):
        self.host = host
        self.config: Config = config
        self.ssh_config = config.ssh_config
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

    @staticmethod
    def get_mail_id(logs: list[PostfixLogEntry]) -> list[str]:
        ids = set()
        for entry in logs:
            possible_id = entry.message.split()[0].strip()
            if possible_id[-1] != ":":
                continue
            possible_id = possible_id[:-1]
            if all(char.isdigit() or char.isupper() for char in possible_id):
                ids.add(possible_id)
        return list(ids)

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

    def query_by(self, query: LogQuery) -> list[PostfixLogEntry]:
        logs: str = ""
        # get logs by time
        if query.time and query.time_range:
            timestamp = datetime.datetime.strptime(
                query.time, "%Y-%m-%d %H:%M:%S"
            )
            time_range = time_range_to_timedelta(query.time_range)
            start_time = timestamp - time_range
            end_time = timestamp + time_range
            start_time_str = start_time.strftime(
                self.config.host_config.time_format
            )
            end_time_str = end_time.strftime(
                self.config.host_config.time_format
            )
            awk_command = f'{{if ($1 >= "{start_time_str}" && $1 <= "{end_time_str}") {{ print $0 }} }}'
            for log_file in self.config.host_config.log_files:
                command = f"awk '{awk_command}' {log_file}"
                for keyword in query.keywords:
                    command += f" | grep -iE '{keyword}'"
                stdout, stderr = self._execute_command(command)
                if stderr:
                    raise ValueError(f"Error executing command: {stderr}")
                logs += stdout
        else:
            for log_file in self.config.host_config.log_files:
                command = f"cat {log_file}"
                for keyword in query.keywords:
                    command += f" | grep -iE '{keyword}'"
                stdout, stderr = self._execute_command(command)
                if stderr:
                    raise ValueError(f"Error executing command: {stderr}")
                logs += stdout
        parser = PostfixLogParser()
        return [parser.parse(line) for line in logs.splitlines() if line]

    def close(self):
        self.client.close()
