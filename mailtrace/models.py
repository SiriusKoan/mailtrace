from dataclasses import dataclass, field
from enum import Enum


@dataclass
class LogEntry:
    datetime: str
    hostname: str
    service: str
    mail_id: str | None
    message: str

    def __str__(self) -> str:
        return f"{self.datetime} {self.hostname} {self.service}: {self.mail_id}: {self.message}"


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
