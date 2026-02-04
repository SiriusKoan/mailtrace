"""Cluster flow conservation check."""

import datetime as dt
import logging
import re
from dataclasses import asdict, dataclass
from enum import Enum

from mailtrace.aggregator import (
    _RELAY_SERVICES,
    _parse_relay_info,
    extract_message_ids,
)
from mailtrace.aggregator.base import LogAggregator
from mailtrace.config import Config
from mailtrace.models import LogEntry, LogQuery
from mailtrace.utils import RelayResult, time_range_to_timedelta

logger = logging.getLogger("mailtrace")


class FlowStatus(Enum):
    COMPLETE = "complete"
    PROBLEMATIC = "problematic"


@dataclass
class MailFlow:
    inbound_mail_id: str
    inbound_host: str
    source: str
    status: FlowStatus
    terminal_state: str
    branches: int = 1
    last_seen_host: str | None = None
    last_seen_mail_id: str | None = None
    expanded_branches: int | None = None
    incomplete_branches: int | None = None

    def to_dict(self) -> dict:
        d = asdict(self)
        d["status"] = self.status.value
        return d


@dataclass
class FlowCheckResult:
    cluster: str
    time: str
    time_range: str
    keywords: list[str] | None
    out_of_window_mail_ids: list[str]
    summary: dict
    complete_flows: list[MailFlow]
    problematic_flows: list[MailFlow]

    def to_dict(self) -> dict:
        return {
            "cluster": self.cluster,
            "time": self.time,
            "time_range": self.time_range,
            "keywords": self.keywords,
            "out_of_window_mail_ids": self.out_of_window_mail_ids,
            "summary": self.summary,
            "complete_flows": [f.to_dict() for f in self.complete_flows],
            "problematic_flows": [f.to_dict() for f in self.problematic_flows],
        }


# --- Inbound mail detection ---

_CLIENT_RE = re.compile(r"client=(?P<hostname>[^\[]+)\[(?P<ip>[^\]]+)\]")
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


def is_inbound_mail(message: str, cluster_hosts: list[str]) -> bool:
    """Check if a smtpd log message is from outside the cluster."""
    match = _CLIENT_RE.search(message)
    if not match:
        return False

    client_hostname = match.group("hostname")
    client_ip = match.group("ip")

    if client_hostname in _LOCAL_HOSTS or client_ip in _LOCAL_HOSTS:
        return False

    cluster_set = set(cluster_hosts)
    if client_hostname in cluster_set or client_ip in cluster_set:
        return False

    return True


def find_inbound_mails(
    log_entries: list[LogEntry],
    cluster_hosts: list[str],
) -> dict[str, dict]:
    """Find inbound mail IDs from log entries.

    Returns dict mapping mail_id to {host, source, logs}.
    """
    inbound: dict[str, dict] = {}

    for entry in log_entries:
        if entry.service != "postfix/smtpd":
            continue
        if entry.mail_id is None:
            continue
        if not is_inbound_mail(entry.message, cluster_hosts):
            continue

        match = _CLIENT_RE.search(entry.message)
        source = match.group("hostname") if match else "unknown"
        inbound[entry.mail_id] = {
            "host": entry.hostname,
            "source": source,
            "logs": [],
        }

    # Collect all log entries for each inbound mail_id
    for entry in log_entries:
        if entry.mail_id in inbound:
            inbound[entry.mail_id]["logs"].append(entry)

    return inbound


# --- Multi-branch tracing ---


def do_trace_all(mail_id: str, aggregator: LogAggregator) -> list[RelayResult]:
    """Collect ALL relay results for a mail_id (not just first)."""
    log_entries = aggregator.query_by(LogQuery(mail_id=mail_id))
    results: list[RelayResult] = []
    for entry in log_entries:
        if entry.service not in _RELAY_SERVICES:
            continue
        result = _parse_relay_info(entry)
        if result:
            results.append(result)
    return results


# --- Terminal state classification ---

_LOCAL_DELIVERY_SERVICES = {"postfix/local", "postfix/virtual"}
_DELIVERED_RE = re.compile(r"status=(sent|delivered)")
_FLOW_RELAY_RE = re.compile(r"relay=(?P<host>[^\[]+)\[(?P<ip>[^\]]+)\]")


def classify_terminal_state(
    log_entries: list[LogEntry],
    cluster_hosts: list[str],
) -> tuple[FlowStatus, str]:
    """Classify terminal state of a mail from its logs.

    Returns (status, reason).
    """
    cluster_set = set(cluster_hosts)

    for entry in log_entries:
        if entry.service == "postfix/bounce":
            return (FlowStatus.PROBLEMATIC, "bounced")

        if entry.service in _LOCAL_DELIVERY_SERVICES:
            if _DELIVERED_RE.search(entry.message):
                return (FlowStatus.COMPLETE, "delivered_locally")

        if entry.service in _RELAY_SERVICES:
            if _DELIVERED_RE.search(entry.message):
                relay_match = _FLOW_RELAY_RE.search(entry.message)
                if relay_match:
                    rhost = relay_match.group("host")
                    rip = relay_match.group("ip")
                    if rhost not in cluster_set and rip not in cluster_set:
                        return (FlowStatus.COMPLETE, "relayed_out")
                    return (FlowStatus.PROBLEMATIC, "internal_relay")

    return (FlowStatus.PROBLEMATIC, "incomplete")


# --- Out-of-window detection ---


def is_out_of_time_window(
    entry_time: str,
    center_time: str,
    time_range: str,
) -> bool:
    """Check if a log entry timestamp falls outside the time window."""
    try:
        center = dt.datetime.strptime(center_time, "%Y-%m-%d %H:%M:%S")
        delta = time_range_to_timedelta(time_range)
        window_start = center - delta
        window_end = center + delta

        entry_str = entry_time.split("+")[0].split("Z")[0]
        try:
            entry = dt.datetime.fromisoformat(entry_str)
        except ValueError:
            entry = dt.datetime.strptime(entry_str, "%Y-%m-%dT%H:%M:%S")

        return entry < window_start or entry > window_end
    except (ValueError, TypeError):
        return True


# --- Helper for MailFlow creation ---


def _create_mail_flow(
    mail_id: str,
    host: str,
    source: str,
    status: FlowStatus,
    terminal_state: str,
    branches: int = 1,
) -> MailFlow:
    """Create a MailFlow with appropriate fields based on status."""
    if status == FlowStatus.COMPLETE:
        return MailFlow(
            inbound_mail_id=mail_id,
            inbound_host=host,
            source=source,
            status=status,
            terminal_state=terminal_state,
            branches=branches,
        )
    return MailFlow(
        inbound_mail_id=mail_id,
        inbound_host=host,
        source=source,
        status=status,
        terminal_state=terminal_state,
        last_seen_host=host,
        last_seen_mail_id=mail_id,
    )


# --- Message-ID based batch classification ---


def _classify_flow_from_batch(
    inbound_mail_id: str,
    all_logs_for_message: list[LogEntry],
    cluster_hosts: list[str],
) -> tuple[FlowStatus, str]:
    """Classify terminal state from batch logs fetched by message_id.

    Groups logs by queue_id and checks each group for terminal state.
    Only considers logs from hosts within the cluster to determine
    the terminal state from the cluster's perspective.
    """
    cluster_set = set(cluster_hosts)

    # Filter logs to only those from cluster hosts, then group by queue_id
    logs_by_qid: dict[str, list[LogEntry]] = {}
    for entry in all_logs_for_message:
        if entry.mail_id and entry.hostname in cluster_set:
            logs_by_qid.setdefault(entry.mail_id, []).append(entry)

    # Check each queue_id group for terminal state
    has_bounce = False
    for qid, entries in logs_by_qid.items():
        status, reason = classify_terminal_state(entries, cluster_hosts)
        if status == FlowStatus.COMPLETE:
            return (FlowStatus.COMPLETE, reason)
        if reason == "bounced":
            has_bounce = True

    if has_bounce:
        return (FlowStatus.PROBLEMATIC, "bounced")

    return (FlowStatus.PROBLEMATIC, "incomplete")


# --- Core orchestrator ---


def _get_queryable_hosts(cluster_hosts: list[str]) -> list[str]:
    """Filter cluster hosts to only FQDNs worth querying.

    Removes short-name duplicates (e.g., keeps 'mx1.example.com'
    but skips 'mx1' since that's the same host) and IPs.
    """
    fqdn_shorts = {h.split(".")[0] for h in cluster_hosts if "." in h}
    return [h for h in cluster_hosts if "." in h or h not in fqdn_shorts]


def _trace_to_terminal(
    mail_id: str,
    host: str,
    aggregator_class: type[LogAggregator],
    config: Config,
    cluster_hosts: list[str],
    time: str,
    time_range: str,
    traced_ids: list[str],
    out_of_window: list[str],
    max_depth: int = 10,
) -> tuple[FlowStatus, str | None, str | None, str | None]:
    """Recursively trace a mail through the cluster.

    Returns (status, reason, last_host, last_mail_id).
    """
    if max_depth <= 0:
        return (FlowStatus.PROBLEMATIC, "max_depth", host, mail_id)

    aggregator = aggregator_class(host, config)
    logs = aggregator.query_by(LogQuery(mail_id=mail_id))

    # Check out-of-window
    for entry in logs:
        if is_out_of_time_window(entry.datetime, time, time_range):
            if mail_id not in out_of_window:
                out_of_window.append(mail_id)

    status, reason = classify_terminal_state(logs, cluster_hosts)

    if status == FlowStatus.COMPLETE:
        return (status, reason, host, mail_id)

    if reason == "bounced":
        return (status, "bounced", host, mail_id)

    # If internal_relay or incomplete, try following relays
    if reason in ("internal_relay", "incomplete"):
        relays = do_trace_all(mail_id, aggregator)
        cluster_set = set(cluster_hosts)
        for relay in relays:
            if relay.relay_host in cluster_set or (
                relay.relay_ip and relay.relay_ip in cluster_set
            ):
                if relay.mail_id and relay.mail_id not in traced_ids:
                    traced_ids.append(relay.mail_id)
                    return _trace_to_terminal(
                        relay.mail_id,
                        relay.relay_host,
                        aggregator_class,
                        config,
                        cluster_hosts,
                        time,
                        time_range,
                        traced_ids,
                        out_of_window,
                        max_depth - 1,
                    )
            else:
                # Relay to external = complete
                return (FlowStatus.COMPLETE, "relayed_out", host, mail_id)

    return (status, reason, host, mail_id)


def _check_cluster_flow_with_message_id(
    config: Config,
    aggregator_class: type[LogAggregator],
    cluster_hosts: list[str],
    all_logs: list[LogEntry],
    inbound: dict[str, dict],
    time: str,
    time_range: str,
) -> tuple[list[MailFlow], list[MailFlow], list[str]]:
    """Optimized flow check using message_id batch queries."""
    complete_flows: list[MailFlow] = []
    problematic_flows: list[MailFlow] = []
    out_of_window: list[str] = []

    # Collect message_ids from inbound mail logs
    inbound_message_ids: dict[str, str] = {}  # message_id -> inbound_mail_id
    for mail_id, info in inbound.items():
        logs = info["logs"]
        mids = extract_message_ids(logs)
        for mid in mids:
            inbound_message_ids[mid] = mail_id

    # Use a single aggregator without hostname filter
    aggregator = aggregator_class("", config)

    # Step 1: Query by message_id to discover all queue_ids in the flow
    mid_qids_cache: dict[str, set[str]] = {}  # message_id -> set of queue_ids
    for mid in inbound_message_ids:
        if mid not in mid_qids_cache:
            mid_logs = aggregator.query_by(LogQuery(message_id=mid))
            mid_qids_cache[mid] = {e.mail_id for e in mid_logs if e.mail_id}

    # Step 2: Query full logs for each discovered queue_id
    qid_logs_cache: dict[str, list[LogEntry]] = {}  # queue_id -> full logs
    all_qids = set()
    for qids in mid_qids_cache.values():
        all_qids.update(qids)
    for qid in all_qids:
        if qid not in qid_logs_cache:
            qid_logs_cache[qid] = aggregator.query_by(LogQuery(mail_id=qid))

    for mail_id, info in inbound.items():
        host = info["host"]
        source = info["source"]
        logs = info["logs"]

        # Find message_ids for this inbound mail
        mids = extract_message_ids(logs)

        if mids:
            # Collect all queue_ids related to this message
            related_qids: set[str] = set()
            for mid in mids:
                related_qids.update(mid_qids_cache.get(mid, set()))

            # Merge full logs for all related queue_ids
            batch_logs: list[LogEntry] = []
            for qid in related_qids:
                batch_logs.extend(qid_logs_cache.get(qid, []))

            # Check out-of-window
            for entry in batch_logs:
                if is_out_of_time_window(entry.datetime, time, time_range):
                    if entry.mail_id and entry.mail_id not in out_of_window:
                        out_of_window.append(entry.mail_id)

            status, reason = _classify_flow_from_batch(
                mail_id, batch_logs, cluster_hosts
            )
        else:
            # No message_id available, classify from initial logs only
            status, reason = classify_terminal_state(logs, cluster_hosts)

        flow = _create_mail_flow(mail_id, host, source, status, reason)
        if status == FlowStatus.COMPLETE:
            complete_flows.append(flow)
        else:
            problematic_flows.append(flow)

    return complete_flows, problematic_flows, out_of_window


def check_cluster_flow(
    config: Config,
    aggregator_class: type[LogAggregator],
    cluster: str,
    time: str,
    time_range: str,
    keywords: list[str] | None = None,
) -> FlowCheckResult:
    """Check flow conservation for a mail cluster."""
    cluster_hosts = config.cluster_to_hosts(cluster)
    if cluster_hosts is None:
        raise ValueError(f"Cluster '{cluster}' not found in config")

    # Query all cluster hosts for logs
    all_logs: list[LogEntry] = []
    queryable = _get_queryable_hosts(cluster_hosts)
    for host in dict.fromkeys(queryable):  # deduplicate
        try:
            agg = aggregator_class(host, config)
            logs = agg.query_by(
                LogQuery(
                    keywords=keywords or [],
                    time=time,
                    time_range=time_range,
                )
            )
            all_logs.extend(logs)
        except Exception as e:
            logger.warning("Failed to query %s: %s", host, e)

    # Find inbound mails
    inbound = find_inbound_mails(all_logs, cluster_hosts)

    # Try message_id optimization for OpenSearch
    from mailtrace.config import Method

    if config.method == Method.OPENSEARCH:
        complete_flows, problematic_flows, out_of_window = (
            _check_cluster_flow_with_message_id(
                config,
                aggregator_class,
                cluster_hosts,
                all_logs,
                inbound,
                time,
                time_range,
            )
        )
    else:
        # Fallback: original per-hop tracing
        complete_flows, problematic_flows, out_of_window = _check_cluster_flow_per_hop(
            config,
            aggregator_class,
            cluster_hosts,
            inbound,
            time,
            time_range,
        )

    total = len(inbound)
    return FlowCheckResult(
        cluster=cluster,
        time=time,
        time_range=time_range,
        keywords=keywords,
        out_of_window_mail_ids=out_of_window,
        summary={
            "total_inbound": total,
            "complete": len(complete_flows),
            "problematic": len(problematic_flows),
        },
        complete_flows=complete_flows,
        problematic_flows=problematic_flows,
    )


def _check_cluster_flow_per_hop(
    config: Config,
    aggregator_class: type[LogAggregator],
    cluster_hosts: list[str],
    inbound: dict[str, dict],
    time: str,
    time_range: str,
) -> tuple[list[MailFlow], list[MailFlow], list[str]]:
    """Original per-hop flow check (for SSH backend)."""
    complete_flows: list[MailFlow] = []
    problematic_flows: list[MailFlow] = []
    out_of_window: list[str] = []

    for mail_id, info in inbound.items():
        host = info["host"]
        source = info["source"]
        logs = info["logs"]
        traced_ids = [mail_id]

        status, reason = classify_terminal_state(logs, cluster_hosts)

        if status == FlowStatus.COMPLETE:
            complete_flows.append(
                _create_mail_flow(mail_id, host, source, status, reason)
            )
            continue

        # Need to trace further for internal_relay
        if reason == "internal_relay":
            relays = do_trace_all(mail_id, aggregator_class(host, config))
            resolved = False
            for relay in relays:
                if relay.mail_id:
                    traced_ids.append(relay.mail_id)
                    t_status, _, _, _ = _trace_to_terminal(
                        relay.mail_id,
                        relay.relay_host,
                        aggregator_class,
                        config,
                        cluster_hosts,
                        time,
                        time_range,
                        traced_ids,
                        out_of_window,
                    )
                    if t_status == FlowStatus.COMPLETE:
                        complete_flows.append(
                            MailFlow(
                                inbound_mail_id=mail_id,
                                inbound_host=host,
                                source=source,
                                status=FlowStatus.COMPLETE,
                                terminal_state="relayed_out",
                                branches=len(relays),
                            )
                        )
                        resolved = True
                        break
            if not resolved:
                problematic_flows.append(
                    MailFlow(
                        inbound_mail_id=mail_id,
                        inbound_host=host,
                        source=source,
                        status=FlowStatus.PROBLEMATIC,
                        terminal_state=reason or "incomplete",
                        last_seen_host=host,
                        last_seen_mail_id=traced_ids[-1],
                    )
                )
        else:
            # Handle bounced and incomplete cases uniformly
            terminal_state = "bounced" if reason == "bounced" else "incomplete"
            problematic_flows.append(
                _create_mail_flow(
                    mail_id,
                    host,
                    source,
                    FlowStatus.PROBLEMATIC,
                    terminal_state,
                )
            )

    return complete_flows, problematic_flows, out_of_window
