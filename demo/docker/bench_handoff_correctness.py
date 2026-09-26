#!/usr/bin/env python3
"""Validate export_traces handoffs using mailbox and raw OpenSearch data."""

from __future__ import annotations

import argparse
import imaplib
import json
import re
import smtplib
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email import policy
from email.message import EmailMessage, Message
from email.parser import BytesParser
from pathlib import Path
from typing import Any

import yaml
from opensearchpy import OpenSearch
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_PROJECT_ROOT))

from mailtrace.config import load_config  # noqa: E402
from mailtrace.tracing import otel  # noqa: E402
from mailtrace.tracing.builder import export_traces  # noqa: E402
from mailtrace.tracing.query import (  # noqa: E402
    group_logs_by_message_id,
    query_all_logs,
)

DEFAULT_CONFIG = "demo/docker/config.yaml"
DEFAULT_OUTPUT = "demo/docker/bench_handoff_correctness_result.json"
ALL_USERS = ("user1", "user2")
POSTFIX_QUEUE_ID_RE = re.compile(r"(?:^|\s)([A-F0-9]{5,}):")
EXIM_QUEUE_ID_RE = re.compile(
    r"(?:^|\s)([A-Za-z0-9]{6,}-[A-Za-z0-9]{6,}-[A-Za-z0-9]{2})(?=\s)"
)
MESSAGE_ID_RE = re.compile(r"(?:message-id|\bid)=<?([^>\s]+)>?", re.IGNORECASE)
NEXT_QUEUE_ID_RE = re.compile(
    r"(?:queued as\s+|\bOK id=)([A-Za-z0-9-]+)", re.IGNORECASE
)
POSTFIX_RELAY_RE = re.compile(r"\brelay=([^\s,\[]+)", re.IGNORECASE)
EXIM_RELAY_RE = re.compile(r"\bH=([^\s\[]+)")
RECEIVED_BY_RE = re.compile(r"\bby\s+([^\s(;]+)", re.IGNORECASE)
RECEIVED_ID_RE = re.compile(r"\bid\s+([A-Za-z0-9-]+)", re.IGNORECASE)

Hop = tuple[str, str]
Edge = tuple[Hop, Hop]


class InfrastructureError(RuntimeError):
    """Indicate that the test environment lacks required evidence."""


@dataclass(frozen=True)
class Scenario:
    name: str
    sender: str
    recipient: str
    smtp_port: int
    expected_routes: dict[str, tuple[str, ...]]

    @property
    def expected_users(self) -> tuple[str, ...]:
        return tuple(self.expected_routes)


@dataclass(frozen=True)
class SentMessage:
    scenario: Scenario
    message_id: str


@dataclass(frozen=True)
class RawLog:
    hostname: str
    service: str
    message: str
    timestamp: str
    queue_id: str | None


def normalize_host(hostname: str) -> str:
    """Normalize testbed short hostnames and fully qualified names."""
    normalized = hostname.strip("[]().").lower()
    if normalized and "." not in normalized:
        normalized = f"{normalized}.example.com"
    return normalized


def build_scenarios(mx_port: int, mailer_ports: list[int]) -> list[Scenario]:
    """Build the six fixed successful-delivery scenarios."""
    routes = {
        1: ("mailer1.example.com", "mailpolicy1.example.com"),
        2: ("mailer2.example.com", "mailpolicy2.example.com"),
        3: ("mailer3.example.com", "mailpolicy3.example.com"),
    }
    specs = (
        ("mx-1-single", 1, "single", True),
        ("mx-2-team", 2, "team", True),
        ("mx-3-single", 3, "single", True),
        ("mailer-1-team", 1, "team", False),
        ("mailer-2-single", 2, "single", False),
        ("mailer-3-team", 3, "team", False),
    )
    scenarios = []
    for name, domain_number, alias, through_mx in specs:
        mailer, mailpolicy = routes[domain_number]
        route_prefix = (
            ("mx.example.com", mailer, mailpolicy)
            if through_mx
            else (mailer, mailpolicy)
        )
        expected_routes = {"user1": route_prefix + ("mailbox.example.com",)}
        if alias == "team":
            next_number = domain_number % 3 + 1
            expected_routes["user2"] = route_prefix + (
                "mx.example.com",
                *routes[next_number],
                "mailbox.example.com",
            )
        scenarios.append(
            Scenario(
                name=name,
                sender=f"sender-{name}@sender.test",
                recipient=f"{alias}@{domain_number}.example.com",
                smtp_port=(
                    mx_port if through_mx else mailer_ports[domain_number - 1]
                ),
                expected_routes=expected_routes,
            )
        )
    return scenarios


def expected_graph_size(scenario: Scenario) -> tuple[int, int]:
    """Calculate queue graph size from shared expected-route prefixes."""
    prefixes = {
        route[:index]
        for route in scenario.expected_routes.values()
        for index in range(1, len(route) + 1)
    }
    return len(prefixes), max(0, len(prefixes) - 1)


def send_scenarios(
    scenarios: list[Scenario], smtp_host: str, run_id: str
) -> list[SentMessage]:
    """Send test messages and return their Message-ID manifest."""
    sent = []
    for scenario in scenarios:
        message_id = f"mailtrace-handoff-{run_id}-{scenario.name}@test.example"
        message = EmailMessage()
        message["From"] = scenario.sender
        message["To"] = scenario.recipient
        message["Subject"] = f"mailtrace handoff {scenario.name}"
        message["Message-ID"] = f"<{message_id}>"
        message["X-Mailtrace-Test-Case"] = scenario.name
        message.set_content(f"handoff correctness case: {scenario.name}\n")
        try:
            with smtplib.SMTP(
                smtp_host, scenario.smtp_port, timeout=15
            ) as smtp:
                smtp.send_message(
                    message,
                    from_addr=scenario.sender,
                    to_addrs=[scenario.recipient],
                )
        except (OSError, smtplib.SMTPException) as exc:
            raise InfrastructureError(
                f"{scenario.name} failed to send: {exc}"
            ) from exc
        sent.append(SentMessage(scenario, message_id))
    return sent


def fetch_imap_message(
    host: str, port: int, user: str, message_id: str
) -> bytes | None:
    """Fetch a raw message from a user's INBOX by Message-ID."""
    try:
        with imaplib.IMAP4(host, port, timeout=10) as client:
            client.login(user, user)
            status, _ = client.select("INBOX", readonly=True)
            if status != "OK":
                raise InfrastructureError(f"Cannot open {user}'s INBOX")
            status, data = client.uid(
                "search", None, "HEADER", "Message-ID", f"<{message_id}>"
            )
            if status != "OK" or not data or not data[0]:
                return None
            uid = data[0].split()[-1]
            status, fetched = client.uid("fetch", uid, "(RFC822)")
            if status != "OK":
                raise InfrastructureError(
                    f"Cannot read {message_id} from {user}"
                )
            for item in fetched:
                if isinstance(item, tuple) and isinstance(item[1], bytes):
                    return item[1]
    except (OSError, imaplib.IMAP4.error) as exc:
        raise InfrastructureError(f"IMAP read failed: {exc}") from exc
    return None


def wait_for_mailboxes(
    sent: list[SentMessage], host: str, port: int, timeout: float
) -> dict[str, dict[str, bytes]]:
    """Wait for every expected mailbox copy."""
    deadline = time.monotonic() + timeout
    found: dict[str, dict[str, bytes]] = {item.message_id: {} for item in sent}
    while time.monotonic() < deadline:
        complete = True
        for item in sent:
            copies = found[item.message_id]
            for user in item.scenario.expected_users:
                if user in copies:
                    continue
                raw = fetch_imap_message(host, port, user, item.message_id)
                if raw is not None:
                    copies[user] = raw
                else:
                    complete = False
        if complete:
            return found
        time.sleep(1)
    missing = [
        f"{item.scenario.name}:{user}"
        for item in sent
        for user in item.scenario.expected_users
        if user not in found[item.message_id]
    ]
    raise InfrastructureError(
        "Timed out waiting for mailbox: " + ", ".join(missing)
    )


def received_hops(raw_message: bytes) -> tuple[list[Hop], list[str]]:
    """Parse an SMTP Received chain from oldest to newest."""
    message: Message = BytesParser(policy=policy.default).parsebytes(
        raw_message
    )
    hops: list[Hop] = []
    errors: list[str] = []
    for value in reversed(message.get_all("Received", [])):
        text = str(value).replace("\n", " ")
        if re.search(r"\bwith\s+LMTP\b", text, re.IGNORECASE):
            continue
        by_match = RECEIVED_BY_RE.search(text)
        id_match = RECEIVED_ID_RE.search(text)
        if not by_match or not id_match:
            errors.append(text)
            continue
        hops.append((normalize_host(by_match.group(1)), id_match.group(1)))
    return hops, errors


def queue_id_from_message(message: str) -> str | None:
    """Extract a queue ID from an unparsed Postfix or Exim log message."""
    for pattern in (POSTFIX_QUEUE_ID_RE, EXIM_QUEUE_ID_RE):
        match = pattern.search(message)
        if match:
            return match.group(1)
    return None


def raw_log_from_hit(hit: dict[str, Any], mapping: dict[str, str]) -> RawLog:
    """Read an OpenSearch source document using only configured fields."""
    source = hit.get("_source", {})
    message = str(source.get(mapping["message"], ""))
    return RawLog(
        hostname=normalize_host(str(source.get(mapping["hostname"], ""))),
        service=str(source.get(mapping["service"], "")),
        message=message,
        timestamp=str(source.get(mapping["timestamp"], "")),
        queue_id=queue_id_from_message(message),
    )


def query_raw_logs(
    config_path: Path,
    start: datetime,
    end: datetime,
    port_override: int | None,
) -> list[RawLog]:
    """Query OpenSearch directly without using the mailtrace parser."""
    with config_path.open(encoding="utf-8") as config_file:
        data = yaml.safe_load(config_file)
    config = data["opensearch_config"]
    mapping = config["mapping"]
    client = OpenSearch(
        hosts=[
            {
                "host": config["host"],
                "port": port_override or config["port"],
            }
        ],
        http_auth=(config["username"], config["password"]),
        use_ssl=config.get("use_ssl", True),
        verify_certs=config.get("verify_certs", False),
        timeout=config.get("timeout", 10),
    )
    response = client.search(
        index=config["index"],
        body={
            "size": 10000,
            "query": {
                "range": {
                    mapping["timestamp"]: {
                        "gte": start.isoformat(),
                        "lte": end.isoformat(),
                    }
                }
            },
            "sort": [{mapping["timestamp"]: {"order": "asc"}}],
        },
    )
    return [raw_log_from_hit(hit, mapping) for hit in response["hits"]["hits"]]


def relay_host(message: str) -> str | None:
    """Extract the next SMTP host from a raw delivery log."""
    match = POSTFIX_RELAY_RE.search(message) or EXIM_RELAY_RE.search(message)
    return normalize_host(match.group(1)) if match else None


def build_raw_graphs(
    logs: list[RawLog], message_ids: set[str]
) -> dict[str, tuple[set[Hop], set[Edge]]]:
    """Build queue handoff graphs with independent regular expressions."""
    queue_to_message: dict[Hop, str] = {}
    parsed_edges: list[Edge] = []
    for log in logs:
        if not log.queue_id:
            continue
        hop = (log.hostname, log.queue_id)
        message_match = MESSAGE_ID_RE.search(log.message)
        if message_match and message_match.group(1) in message_ids:
            queue_to_message[hop] = message_match.group(1)
        next_match = NEXT_QUEUE_ID_RE.search(log.message)
        next_host = relay_host(log.message)
        if next_match and next_host:
            parsed_edges.append((hop, (next_host, next_match.group(1))))

    changed = True
    while changed:
        changed = False
        for source, target in parsed_edges:
            message_id = queue_to_message.get(source)
            if message_id and target not in queue_to_message:
                queue_to_message[target] = message_id
                changed = True

    graphs = {message_id: (set(), set()) for message_id in message_ids}
    for hop, message_id in queue_to_message.items():
        if message_id in graphs:
            graphs[message_id][0].add(hop)
    for source, target in parsed_edges:
        message_id = queue_to_message.get(source)
        if message_id in graphs and queue_to_message.get(target) == message_id:
            graphs[message_id][1].add((source, target))
    return graphs


def export_span_graphs(
    logs_by_message_id: dict[str, list[Any]],
) -> tuple[int, dict[str, tuple[set[Hop], set[Edge], list[dict[str, Any]]]]]:
    """Run export_traces and project finished spans into handoff graphs."""
    exporter = InMemorySpanExporter()
    otel._providers.clear()
    otel._exporter = exporter
    trace_count = export_traces(logs_by_message_id)
    otel.flush_traces()
    spans = list(exporter.get_finished_spans())

    spans_by_trace: dict[int, list[Any]] = {}
    for span in spans:
        spans_by_trace.setdefault(span.context.trace_id, []).append(span)

    graphs = {}
    for trace_spans in spans_by_trace.values():
        root = next(
            (
                span
                for span in trace_spans
                if span.name == "email.delivery"
                and "message.id" in span.attributes
            ),
            None,
        )
        if root is None:
            continue
        message_id = str(root.attributes["message.id"])
        span_by_id = {span.context.span_id: span for span in trace_spans}
        host_by_span_id: dict[int, Hop] = {}
        attributes = []
        for span in trace_spans:
            host = span.attributes.get("server.address")
            queue_id = span.attributes.get("email.queue_id")
            if host and queue_id:
                hop = (normalize_host(str(host)), str(queue_id))
                host_by_span_id[span.context.span_id] = hop
                attributes.append({"hop": hop, **dict(span.attributes)})

        edges: set[Edge] = set()
        for span_id, child_hop in host_by_span_id.items():
            parent = span_by_id[span_id].parent
            while parent is not None:
                parent_hop = host_by_span_id.get(parent.span_id)
                if parent_hop is not None:
                    if parent_hop != child_hop:
                        edges.add((parent_hop, child_hop))
                    break
                parent_span = span_by_id.get(parent.span_id)
                parent = (
                    parent_span.parent if parent_span is not None else None
                )
        graphs[message_id] = (set(host_by_span_id.values()), edges, attributes)
    otel._providers.clear()
    otel._exporter = None
    return trace_count, graphs


def wait_for_logs(
    config_path: Path,
    start: datetime,
    sent: list[SentMessage],
    timeout: float,
    opensearch_port: int | None,
) -> tuple[list[RawLog], dict[str, list[Any]]]:
    """Wait until raw and production queries contain all six messages."""
    message_ids = {item.message_id for item in sent}
    config = load_config(str(config_path))
    if opensearch_port is not None:
        config.opensearch_config.port = opensearch_port
    deadline = time.monotonic() + timeout
    raw_logs: list[RawLog] = []
    selected: dict[str, list[Any]] = {}
    while time.monotonic() < deadline:
        end = datetime.now(UTC) + timedelta(seconds=2)
        try:
            raw_logs = query_raw_logs(config_path, start, end, opensearch_port)
            production_logs = query_all_logs(config, start, end)
        except Exception as exc:
            last_error = exc
        else:
            grouped = group_logs_by_message_id(production_logs)
            selected = {
                message_id: grouped[message_id]
                for message_id in message_ids
                if message_id in grouped
            }
            raw_graphs = build_raw_graphs(raw_logs, message_ids)
            observed_raw_hops = {
                (log.hostname, log.queue_id)
                for log in raw_logs
                if log.queue_id is not None
            }
            production_hops = {
                message_id: {
                    (normalize_host(log.hostname), log.mail_id)
                    for log in message_logs
                    if log.mail_id is not None
                }
                for message_id, message_logs in selected.items()
            }
            if len(selected) == len(message_ids) and all(
                len(raw_graphs[item.message_id][0])
                >= expected_graph_size(item.scenario)[0]
                and len(raw_graphs[item.message_id][1])
                >= expected_graph_size(item.scenario)[1]
                and raw_graphs[item.message_id][0] <= observed_raw_hops
                and raw_graphs[item.message_id][0]
                <= production_hops[item.message_id]
                for item in sent
            ):
                return raw_logs, selected
        time.sleep(1)
    detail = f": {last_error}" if "last_error" in locals() else ""
    raise InfrastructureError(
        f"Timed out waiting for OpenSearch logs; found "
        f"{len(selected)}/{len(message_ids)} messages{detail}"
    )


def split_queue_ids(value: Any) -> set[str]:
    """Split next queue IDs merged by export_traces."""
    return {part for part in str(value or "").split(",") if part}


def evaluate_case(
    item: SentMessage,
    copies: dict[str, bytes],
    raw_graph: tuple[set[Hop], set[Edge]],
    span_graph: tuple[set[Hop], set[Edge], list[dict[str, Any]]] | None,
) -> dict[str, Any]:
    """Compare the manifest, mailbox, raw logs, and exported spans."""
    received_by_user = {}
    parse_errors = {}
    for user, raw_message in copies.items():
        hops, errors = received_hops(raw_message)
        received_by_user[user] = hops
        if errors:
            parse_errors[user] = errors

    oracle_nodes = {hop for hops in received_by_user.values() for hop in hops}
    oracle_edges = {
        edge
        for hops in received_by_user.values()
        for edge in zip(hops, hops[1:])
    }
    routes = {
        user: [host for host, _ in hops]
        for user, hops in received_by_user.items()
    }
    route_ok = set(routes) == set(item.scenario.expected_routes) and all(
        tuple(route) == item.scenario.expected_routes[user]
        for user, route in routes.items()
    )
    unexpected_users = []
    for user in set(ALL_USERS) - set(item.scenario.expected_users):
        if user in copies:
            unexpected_users.append(user)

    raw_nodes, raw_edges = raw_graph
    raw_ok = oracle_nodes <= raw_nodes and oracle_edges <= raw_edges
    if span_graph is None:
        trace_nodes: set[Hop] = set()
        trace_edges: set[Edge] = set()
        attributes: list[dict[str, Any]] = []
    else:
        trace_nodes, trace_edges, attributes = span_graph

    nodes_ok = trace_nodes == oracle_nodes
    edges_ok = trace_edges == oracle_edges
    attributes_ok = True
    handoff_ok = True
    incoming_nodes = {target for _, target in oracle_edges}
    for hop in oracle_nodes:
        hop_attributes = [row for row in attributes if row["hop"] == hop]
        expected_handoff = "explicit" if hop in incoming_nodes else "none"
        if not hop_attributes or any(
            row.get("email.handoff") != expected_handoff
            for row in hop_attributes
        ):
            handoff_ok = False
    for source, (next_host, next_queue_id) in oracle_edges:
        source_attributes = [row for row in attributes if row["hop"] == source]
        if not any(
            normalize_host(str(row.get("email.next_host", ""))) == next_host
            and next_queue_id
            in split_queue_ids(row.get("email.next_queue_id"))
            for row in source_attributes
        ):
            attributes_ok = False

    checks = {
        "received_parse": not parse_errors,
        "expected_route": route_ok,
        "unexpected_mailbox_copy": not unexpected_users,
        "raw_log_handoffs": raw_ok,
        "trace_nodes": nodes_ok,
        "trace_edges": edges_ok,
        "trace_next_attributes": attributes_ok,
        "trace_handoff_attributes": handoff_ok,
    }
    return {
        "scenario": item.scenario.name,
        "message_id": item.message_id,
        "expected_routes": {
            user: list(route)
            for user, route in item.scenario.expected_routes.items()
        },
        "received_routes": routes,
        "oracle_nodes": sorted(oracle_nodes),
        "oracle_edges": sorted(oracle_edges),
        "raw_log_nodes": sorted(raw_nodes),
        "raw_log_edges": sorted(raw_edges),
        "trace_nodes": sorted(trace_nodes),
        "trace_edges": sorted(trace_edges),
        "trace_attributes": attributes,
        "parse_errors": parse_errors,
        "unexpected_users": unexpected_users,
        "checks": checks,
        "passed": all(checks.values()),
    }


def print_results(results: list[dict[str, Any]]) -> None:
    """Print a readable result for each scenario."""
    print("scenario        result  failed_checks")
    print("--------------  ------  -------------")
    for result in results:
        failed = [
            name for name, passed in result["checks"].items() if not passed
        ]
        print(
            f"{result['scenario']:<14}  "
            f"{'PASS' if result['passed'] else 'FAIL':<6}  "
            f"{','.join(failed) or '-'}"
        )


def resolve_path(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else _PROJECT_ROOT / path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=DEFAULT_CONFIG)
    parser.add_argument("--output", default=DEFAULT_OUTPUT)
    parser.add_argument("--smtp-host", default="127.0.0.1")
    parser.add_argument("--mx-port", type=int, default=10025)
    parser.add_argument(
        "--mailer-ports", type=int, nargs=3, default=[20025, 20026, 20027]
    )
    parser.add_argument("--imap-host", default="127.0.0.1")
    parser.add_argument("--imap-port", type=int, default=10143)
    parser.add_argument("--opensearch-port", type=int)
    parser.add_argument("--timeout", type=float, default=90)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config_path = resolve_path(args.config)
    output_path = resolve_path(args.output)
    if not config_path.is_file():
        print(
            f"Infrastructure error: config not found: {config_path}",
            file=sys.stderr,
        )
        return 1

    run_id = uuid.uuid4().hex[:12]
    scenarios = build_scenarios(args.mx_port, args.mailer_ports)
    started_at = datetime.now(UTC) - timedelta(seconds=5)
    try:
        sent = send_scenarios(scenarios, args.smtp_host, run_id)
        mailbox_messages = wait_for_mailboxes(
            sent, args.imap_host, args.imap_port, args.timeout
        )
        for item in sent:
            for user in set(ALL_USERS) - set(item.scenario.expected_users):
                unexpected = fetch_imap_message(
                    args.imap_host, args.imap_port, user, item.message_id
                )
                if unexpected is not None:
                    mailbox_messages[item.message_id][user] = unexpected
        raw_logs, production_groups = wait_for_logs(
            config_path,
            started_at,
            sent,
            args.timeout,
            args.opensearch_port,
        )
        raw_graphs = build_raw_graphs(
            raw_logs, {item.message_id for item in sent}
        )
        trace_count, span_graphs = export_span_graphs(production_groups)
    except InfrastructureError as exc:
        print(f"Infrastructure error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(
            f"Infrastructure error: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 1

    results = [
        evaluate_case(
            item,
            mailbox_messages[item.message_id],
            raw_graphs[item.message_id],
            span_graphs.get(item.message_id),
        )
        for item in sent
    ]
    report = {
        "run_id": run_id,
        "generated_trace_count": trace_count,
        "expected_trace_count": len(sent),
        "passed": all(result["passed"] for result in results),
        "results": results,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print_results(results)
    print(f"result_file={output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
