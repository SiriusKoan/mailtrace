"""Infer Mailman reinjections on explicitly configured hosts."""

import re
from datetime import datetime

from mailtrace.models import LogEntry
from mailtrace.tracing.query import HopKey

_BOUNCE_SENDER_RE = re.compile(
    r"\bfrom=<([^<>@]+)-bounces(?:\+[^<>@]*)?@([^<>@]+)>", re.I
)
_RECIPIENT_RE = re.compile(r"\bto=<([^>]*)>")
_SENT_RE = re.compile(r"\bstatus=sent\b")


def infer_mailman_sources(
    hops_logs: dict[HopKey, list[LogEntry]],
    starts: dict[HopKey, datetime],
) -> dict[HopKey, HopKey | None]:
    """Match bounce senders to earlier successful LMTP deliveries on one host.

    An absent key leaves generic inference in control. A None value means a
    Mailman sender was recognized but no unique source could be established.
    """
    sources: dict[HopKey, HopKey | None] = {}
    ordered = sorted(hops_logs, key=lambda hop: (starts[hop], hop))
    for index, hop in enumerate(ordered):
        sender = next(
            (
                match
                for log in hops_logs[hop]
                if (match := _BOUNCE_SENDER_RE.search(log.message))
            ),
            None,
        )
        if sender is None:
            continue
        name = sender[1].lower()
        domain = sender[2].lower().removeprefix("lists.")
        recipients = {
            f"{local}@{suffix}"
            for local in (name, f"{name}-owner")
            for suffix in (domain, f"lists.{domain}")
        }
        candidates: dict[HopKey, datetime] = {}
        for source in ordered[:index]:
            for log in hops_logs[source]:
                if (log.service or "").lower() != "postfix/lmtp":
                    continue
                if not _SENT_RE.search(log.message):
                    continue
                recipient = _RECIPIENT_RE.search(log.message)
                if recipient is None or recipient[1].lower() not in recipients:
                    continue
                completed = datetime.fromisoformat(
                    log.datetime.replace("Z", "+00:00")
                )
                if completed <= starts[hop]:
                    candidates[source] = max(
                        candidates.get(source, completed), completed
                    )
        latest = max(candidates.values(), default=None)
        nearest = [
            source
            for source, completed in candidates.items()
            if completed == latest
        ]
        # Tied candidates must not be resolved by input order or queue ID.
        sources[hop] = nearest[0] if len(nearest) == 1 else None
    return sources
