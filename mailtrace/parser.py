import re
from dataclasses import dataclass
from typing import Type


@dataclass
class LogEntry:
    datetime: str
    hostname: str
    service: str
    mail_id: str | None
    message: str

    def __str__(self) -> str:
        return f"{self.datetime} {self.hostname} {self.service}: {self.mail_id}: {self.message}"


def check_mail_id_valid(mail_id: str) -> bool:
    return bool(re.match(r"^[0-9A-Z]+$", mail_id))


class LogParser:
    def parse(self, log: str) -> LogEntry:
        raise NotImplementedError("Subclasses must implement parse method")


class NoSpaceInDatetimeParser(LogParser):
    """
    This parser is designed to handle log entries where the datetime does not contain any spaces.
    Example log format:
    2025-01-01T10:00:00.123456+08:00 mailer1 postfix/qmgr[123456]: A2DE917F931: from=<abc@example.com>, size=12345, nrcpt=1 (queue active)
    """

    def parse(self, log: str) -> LogEntry:
        log_split = log.split(" ", 4)
        datetime = log_split[0]
        hostname = log_split[1]
        service = log_split[2].split("[")[0]
        mail_id = (
            log_split[3][:-1]
            if check_mail_id_valid(log_split[3][:-1])
            else None
        )
        message = log_split[4]
        return LogEntry(datetime, hostname, service, mail_id, message)


class DayOfWeekParser(LogParser):
    """
    This parser is designed to handle log entries where the datetime includes the day of the week.
    Example log format:
    Feb 1 10:00:00 mailer1 postfix/qmgr[123456]: A2DE917F931: from=<abc@example.com>, size=12345, nrcpt=1 (queue active)
    """

    def parse(self, log: str) -> LogEntry:
        log_split = log.split(" ", 6)
        datetime = " ".join(log_split[:3])
        hostname = log_split[3]
        service = log_split[4].split("[")[0]
        mail_id = (
            log_split[5][:-1]
            if check_mail_id_valid(log_split[5][:-1])
            else None
        )
        message = log_split[6]
        return LogEntry(datetime, hostname, service, mail_id, message)


PARSERS: dict[str, Type[LogParser]] = {
    NoSpaceInDatetimeParser.__name__: NoSpaceInDatetimeParser,
    DayOfWeekParser.__name__: DayOfWeekParser,
}
