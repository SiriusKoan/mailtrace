#!/usr/bin/env python3
"""Compare current handoff topology with a timestamp-only topology."""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import perf_counter
from typing import Any

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_PROJECT_ROOT))

from mailtrace.config import Config, load_config  # noqa: E402
from mailtrace.models import LogEntry  # noqa: E402
from mailtrace.parser import extract_next_mail_id  # noqa: E402
from mailtrace.tracing.builder import (  # noqa: E402
    _extract_sender_recipients,
    _plan_hops,
    _prepare_hops,
)
from mailtrace.tracing.delay_parser import (  # noqa: E402
    EximDelayParser,
    PostfixDelayParser,
)
from mailtrace.tracing.lifecycle import (  # noqa: E402
    PendingTrace,
    should_export_trace,
)
from mailtrace.tracing.otel import get_effective_total_delay  # noqa: E402
from mailtrace.tracing.query import (  # noqa: E402
    _extract_message_id_from_log,
    _normalize_hostname,
    group_logs_by_hops,
    group_logs_by_message_id,
    query_all_logs,
)

DEFAULT_CONFIG = _PROJECT_ROOT / "config.yaml"
DEFAULT_RESULT = (
    _PROJECT_ROOT / "demo/docker/bench_handoff_timestamp_result.json"
)
EXPECTED_LOG_COUNT = 8_090_952
UTC_START = datetime(2026, 9, 23, 16, tzinfo=UTC)
UTC_END = datetime(2026, 9, 24, 16, tzinfo=UTC)
ROOT = "<root>"
PROGRESS_INTERVAL = 1_000_000

HopKey = tuple[str, str]
HostEdge = tuple[str, str]
MessageIds = str | frozenset[str]

_POSTFIX_PARSER = PostfixDelayParser()
_EXIM_PARSER = EximDelayParser()

METRIC_DEFINITIONS = {
    "complete_host_order_match_pct": (
        "Exact per-trace host-order match; every position must match."
    ),
    "complete_topology_match_pct": (
        "Exact per-trace host-edge-set match, including root-to-first-host "
        "and host-to-host handoffs. Any missing, extra, or incorrect edge "
        "makes the trace a mismatch."
    ),
    "edge_precision_pct": (
        "Fraction of timestamp edges also present in the current baseline; "
        "TP / (TP + FP)."
    ),
    "edge_recall_pct": (
        "Fraction of current-baseline edges recovered by timestamp; "
        "TP / (TP + FN)."
    ),
    "edge_f1_pct": (
        "Harmonic mean of edge precision and edge recall; 2PR / (P + R)."
    ),
}


@dataclass(slots=True)
class EdgeSummary:
    """Minimal data retained for one queue handoff edge."""

    order: int
    propagates_message_id: bool
    handoff_count: int = 1
    handoff_recipients: set[str] = field(default_factory=set)


@dataclass(slots=True)
class HopSummary:
    """Queue-hop summary that does not retain raw log messages."""

    first_timestamp: datetime
    last_timestamp: datetime
    first_order: int
    detected_mta: str | None = None
    postfix_start: datetime | None = None
    postfix_end: datetime | None = None
    exim_start: datetime | None = None
    exim_end: datetime | None = None
    explicit_message_ids: MessageIds | None = None
    relay_targets: dict[HopKey, EdgeSummary] | None = None
    delivery_recipients: set[str | None] | None = None

    def record_timestamp(self, timestamp: datetime) -> None:
        self.first_timestamp = min(self.first_timestamp, timestamp)
        self.last_timestamp = max(self.last_timestamp, timestamp)

    def record_mta(self, service: str | None) -> None:
        if self.detected_mta is not None or not isinstance(service, str):
            return
        service = service.lower()
        if "postfix" in service:
            self.detected_mta = "postfix"
        elif "exim" in service:
            self.detected_mta = "exim"

    def record_delay(self, mta: str, start: datetime, end: datetime) -> None:
        start_attr = f"{mta}_start"
        end_attr = f"{mta}_end"
        current_start = getattr(self, start_attr)
        current_end = getattr(self, end_attr)
        setattr(
            self,
            start_attr,
            start if current_start is None else min(current_start, start),
        )
        setattr(
            self,
            end_attr,
            end if current_end is None else max(current_end, end),
        )

    def raw_bounds(self) -> tuple[datetime, datetime]:
        mta = "exim" if self.detected_mta == "exim" else "postfix"
        start = getattr(self, f"{mta}_start")
        end = getattr(self, f"{mta}_end")
        if start is not None and end is not None:
            return start, end
        return self.first_timestamp, self.last_timestamp


@dataclass(frozen=True, slots=True)
class HostPlan:
    """Host-level order and topology after queue-hop projection."""

    order: tuple[str, ...]
    edges: frozenset[HostEdge]


def _merge_message_ids(
    current: MessageIds | None, incoming: MessageIds
) -> MessageIds:
    """Merge queue mappings while preserving every conflicting value."""
    if current is None:
        return incoming
    if current == incoming:
        return current
    current_values = {current} if isinstance(current, str) else set(current)
    incoming_values = (
        {incoming} if isinstance(incoming, str) else set(incoming)
    )
    return frozenset(current_values | incoming_values)


def _parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class CompactAccumulator:
    """Stream OpenSearch logs into queue-hop summaries."""

    def __init__(self) -> None:
        self.hops: dict[HopKey, HopSummary] = {}
        self.log_count = 0
        self.logs_without_queue_id = 0

    def add(self, log: LogEntry) -> None:
        order = self.log_count
        self.log_count += 1
        if not log.hostname or not log.mail_id:
            self.logs_without_queue_id += 1
            return

        timestamp = _parse_timestamp(log.datetime)
        hop = (_normalize_hostname(log.hostname), log.mail_id)
        summary = self.hops.get(hop)
        if summary is None:
            summary = HopSummary(timestamp, timestamp, order)
            self.hops[hop] = summary
        else:
            summary.record_timestamp(timestamp)

        summary.record_mta(log.service)
        if self._record_delay(summary, log.message, timestamp):
            _, recipients = _extract_sender_recipients([log])
            if summary.delivery_recipients is None:
                summary.delivery_recipients = set()
            summary.delivery_recipients.add(
                recipients[0] if recipients else None
            )

        message_id = _extract_message_id_from_log(log)
        if message_id:
            summary.explicit_message_ids = _merge_message_ids(
                summary.explicit_message_ids, message_id
            )

        next_queue_id = extract_next_mail_id(log)
        if not next_queue_id:
            return
        target_host = _normalize_hostname(log.relay_host or log.hostname)
        target = (target_host, next_queue_id)
        propagates = bool(log.relay_host and log.queued_as)
        if summary.relay_targets is None:
            summary.relay_targets = {}
        edge = summary.relay_targets.get(target)
        _, handoff_recipients = _extract_sender_recipients([log])
        if edge is None:
            summary.relay_targets[target] = EdgeSummary(
                order,
                propagates,
                handoff_recipients=set(handoff_recipients or ()),
            )
        else:
            edge.handoff_count += 1
            edge.handoff_recipients.update(handoff_recipients or ())
            if propagates:
                edge.propagates_message_id = True

    @staticmethod
    def _record_delay(
        summary: HopSummary, message: str, timestamp: datetime
    ) -> bool:
        recorded = False
        if "delays=" in message:
            delays = _POSTFIX_PARSER.parse(message)
            if delays.get_delay_values():
                recorded = True
                duration = get_effective_total_delay(delays)
                summary.record_delay(
                    "postfix",
                    timestamp - timedelta(seconds=duration),
                    timestamp,
                )
        if "RT=" in message and "DT=" in message:
            delays = _EXIM_PARSER.parse(message)
            if delays.get_delay_values():
                recorded = True
                duration = get_effective_total_delay(delays)
                summary.record_delay(
                    "exim",
                    timestamp - timedelta(seconds=duration),
                    timestamp,
                )
        return recorded

    def resolve_message_ids(self) -> dict[HopKey, MessageIds]:
        """Compute the current forward-propagation closure with a worklist."""
        mapping: dict[HopKey, MessageIds] = {}
        pending: deque[HopKey] = deque()

        for hop, summary in self.hops.items():
            if summary.explicit_message_ids is not None:
                mapping[hop] = summary.explicit_message_ids
                pending.append(hop)

        while pending:
            source = pending.popleft()
            source_ids = mapping[source]
            summary = self.hops.get(source)
            if summary is None or summary.relay_targets is None:
                continue
            for target, edge in summary.relay_targets.items():
                if not edge.propagates_message_id:
                    continue
                merged = _merge_message_ids(mapping.get(target), source_ids)
                if mapping.get(target) != merged:
                    mapping[target] = merged
                    pending.append(target)

        return mapping

    def resolved_hops(
        self, mapping: dict[HopKey, MessageIds]
    ) -> dict[str, list[HopKey]]:
        """Group observed queue hops by unambiguous Message-ID."""
        grouped: dict[str, list[HopKey]] = defaultdict(list)
        for hop, summary in self.hops.items():
            message_ids = mapping.get(hop)
            if isinstance(message_ids, str):
                grouped[message_ids].append(hop)
        for hops in grouped.values():
            hops.sort(key=lambda hop: self.hops[hop].first_order)
        return dict(grouped)


def _project_plan(
    ordered_hops: list[HopKey],
    placement_sources: dict[HopKey, tuple[HopKey, ...]],
) -> HostPlan:
    host_order: list[str] = []
    seen_hosts: set[str] = set()
    edges: set[HostEdge] = set()

    for hop in ordered_hops:
        host = _normalize_hostname(hop[0])
        if host not in seen_hosts:
            seen_hosts.add(host)
            host_order.append(host)

        sources = placement_sources[hop]
        if not sources:
            edges.add((ROOT, host))
            continue
        for source in sources:
            source_host = _normalize_hostname(source[0])
            if source_host != host:
                edges.add((source_host, host))

    return HostPlan(tuple(host_order), frozenset(edges))


def plan_current_compact(
    accumulator: CompactAccumulator, hops: list[HopKey]
) -> tuple[HostPlan, bool]:
    """Reproduce `_plan_hops()` ordering and placement from summaries."""
    hop_set = set(hops)
    incoming: dict[HopKey, list[tuple[HopKey, EdgeSummary]]] = defaultdict(
        list
    )
    for source in hops:
        targets = accumulator.hops[source].relay_targets or {}
        for target, edge in sorted(
            targets.items(), key=lambda item: item[1].order
        ):
            if target in hop_set and target != source:
                incoming[target].append((source, edge))

    dependencies = {
        hop: {source for source, _ in incoming.get(hop, ())} for hop in hops
    }
    pending = list(hops)
    ordered: list[HopKey] = []
    detached: set[HopKey] = set()

    while pending:
        ready = [
            hop
            for hop in pending
            if not dependencies[hop].intersection(pending)
        ]
        if not ready:
            detached.update(pending)
            for hop in pending:
                dependencies[hop].clear()
            ready = pending

        def sort_key(hop: HopKey) -> tuple[datetime, datetime, HopKey]:
            raw_start, raw_end = accumulator.hops[hop].raw_bounds()
            return raw_start, raw_end, hop

        next_hop = min(ready, key=sort_key)
        pending.remove(next_hop)
        ordered.append(next_hop)

    hop_order = {hop: index for index, hop in enumerate(ordered)}
    hops_by_host: dict[str, list[HopKey]] = defaultdict(list)
    for hop in hops:
        hops_by_host[_normalize_hostname(hop[0])].append(hop)

    placements: dict[HopKey, tuple[HopKey, ...]] = {}
    placement_handoffs: dict[HopKey, tuple[EdgeSummary, ...]] = {}
    for host_hops in hops_by_host.values():
        host_hops.sort(key=hop_order.__getitem__)
        previous: HopKey | None = None
        for hop in host_hops:
            if hop in detached:
                placements[hop] = ()
                placement_handoffs[hop] = ()
            elif incoming.get(hop):
                placements[hop] = tuple(source for source, _ in incoming[hop])
                placement_handoffs[hop] = tuple(
                    edge for _, edge in incoming[hop]
                )
            elif previous is not None:
                placements[hop] = placements[previous]
                placement_handoffs[hop] = placement_handoffs[previous]
            else:
                placements[hop] = ()
                placement_handoffs[hop] = ()
            previous = hop

    ambiguous = False
    for hop, handoffs in placement_handoffs.items():
        if sum(edge.handoff_count for edge in handoffs) <= 1:
            continue
        handoff_recipients = {
            recipient
            for edge in handoffs
            for recipient in edge.handoff_recipients
        }
        delivery_recipients = accumulator.hops[hop].delivery_recipients or {
            None
        }
        if any(
            recipient not in handoff_recipients
            for recipient in delivery_recipients
        ):
            ambiguous = True
            break

    return _project_plan(ordered, placements), ambiguous


def plan_timestamp(
    accumulator: CompactAccumulator, hops: list[HopKey]
) -> HostPlan:
    """Build a linear queue-hop topology from raw timestamps."""
    ordered_hops = sorted(
        hops,
        key=lambda hop: (accumulator.hops[hop].first_timestamp, hop),
    )
    placements = {
        hop: (() if index == 0 else (ordered_hops[index - 1],))
        for index, hop in enumerate(ordered_hops)
    }
    return _project_plan(ordered_hops, placements)


@dataclass(slots=True)
class PendingCompactTrace:
    accumulator: CompactAccumulator
    lifecycle: PendingTrace
    seen_log_keys: set[tuple]


@dataclass(slots=True)
class ComparisonTotals:
    unique_message_ids: set[str] = field(default_factory=set)
    exported_traces: int = 0
    queue_hops: int = 0
    relay_edges: int = 0
    multi_host_candidates: int = 0
    ambiguous_handoff_traces: int = 0
    multi_host: int = 0
    exact_order: int = 0
    exact_topology: int = 0
    true_positive_edges: int = 0
    current_edges: int = 0
    timestamp_edges: int = 0
    branch_topologies: int = 0
    repeated_host_hops: int = 0

    def add_trace(
        self, message_id: str, accumulator: CompactAccumulator
    ) -> None:
        hops = list(accumulator.hops)
        if not hops:
            return

        self.unique_message_ids.add(message_id)
        self.exported_traces += 1
        self.queue_hops += len(hops)
        self.relay_edges += sum(
            len(summary.relay_targets or {})
            for summary in accumulator.hops.values()
        )

        host_count = len({hop[0] for hop in hops})
        if len(hops) > host_count:
            self.repeated_host_hops += 1
        if host_count < 2:
            return

        self.multi_host_candidates += 1
        current, ambiguous = plan_current_compact(accumulator, hops)
        if ambiguous:
            self.ambiguous_handoff_traces += 1
            return

        self.multi_host += 1
        timestamp = plan_timestamp(accumulator, hops)
        self.exact_order += current.order == timestamp.order
        self.exact_topology += current.edges == timestamp.edges
        self.true_positive_edges += len(current.edges & timestamp.edges)
        self.current_edges += len(current.edges)
        self.timestamp_edges += len(timestamp.edges)

        outgoing: dict[str, int] = defaultdict(int)
        for source, _ in current.edges:
            if source != ROOT:
                outgoing[source] += 1
        self.branch_topologies += any(count > 1 for count in outgoing.values())

    def result(self) -> tuple[dict[str, object], dict[str, int]]:
        precision = _percentage(self.true_positive_edges, self.timestamp_edges)
        recall = _percentage(self.true_positive_edges, self.current_edges)
        f1 = (
            round(2 * precision * recall / (precision + recall), 6)
            if precision + recall
            else 0.0
        )
        timestamp_metrics = {
            "complete_host_order_match_pct": _percentage(
                self.exact_order, self.multi_host
            ),
            "complete_topology_match_pct": _percentage(
                self.exact_topology, self.multi_host
            ),
            "edge_precision_pct": precision,
            "edge_recall_pct": recall,
            "edge_f1_pct": f1,
        }
        baseline = {key: 100.0 for key in timestamp_metrics}
        comparison = {
            "scope": "multi_host_traces_without_ambiguous_handoffs",
            "current_baseline": baseline,
            "timestamp": timestamp_metrics,
            "difference_percentage_points": {
                key: round(timestamp_metrics[key] - baseline[key], 6)
                for key in timestamp_metrics
            },
        }
        counts = {
            "message_ids": len(self.unique_message_ids),
            "exported_traces": self.exported_traces,
            "multi_host_traces_before_ambiguous_exclusion": (
                self.multi_host_candidates
            ),
            "ambiguous_handoff_traces_excluded": (
                self.ambiguous_handoff_traces
            ),
            "multi_host_traces": self.multi_host,
            "branch_topology_traces": self.branch_topologies,
            "repeated_host_hop_traces": self.repeated_host_hops,
        }
        return comparison, counts


class LifecycleBenchmark:
    """Replay sorted logs with the production trace-buffer lifecycle."""

    def __init__(
        self,
        sleep_seconds: int,
        hold_rounds: int,
        go_back_seconds: int,
        max_trace_age_seconds: int,
    ) -> None:
        self.sleep_seconds = sleep_seconds
        self.hold_rounds = hold_rounds
        self.go_back_seconds = go_back_seconds
        self.max_trace_age_seconds = max_trace_age_seconds
        self.pending: dict[str, PendingCompactTrace] = {}
        self.queue_mapping: dict[HopKey, str] = {}
        self.comparison = ComparisonTotals()
        self.log_count = 0
        self.queried_log_count = 0
        self.logs_without_queue_id = 0
        self.seen_queue_hops: set[HopKey] = set()
        self.resolved_queue_hops: set[HopKey] = set()
        self.mapping_reassignments = 0
        self.mapping_reassignment_sample: list[dict[str, str]] = []
        self.max_pending_traces = 0
        self.max_queue_mappings = 0
        self.current_round = 0

    @property
    def active_queue_hops(self) -> int:
        return sum(
            len(pending.accumulator.hops) for pending in self.pending.values()
        )

    @staticmethod
    def _log_key(log: LogEntry) -> tuple:
        return (log.datetime, log.hostname, log.service, log.message)

    def process_round(
        self, logs: list[LogEntry], new_range_start: datetime
    ) -> None:
        self.current_round += 1
        self.queried_log_count += len(logs)
        for log in logs:
            if _parse_timestamp(log.datetime) < new_range_start:
                continue
            self.log_count += 1
            if log.hostname and log.mail_id:
                self.seen_queue_hops.add(
                    (_normalize_hostname(log.hostname), log.mail_id)
                )
            else:
                self.logs_without_queue_id += 1

        previous_mapping = dict(self.queue_mapping)
        grouped = group_logs_by_message_id(logs, self.queue_mapping)
        self._record_mapping_reassignments(previous_mapping)

        for message_id, message_logs in grouped.items():
            pending = self.pending.get(message_id)
            if pending is None:
                additions = message_logs
                pending = PendingCompactTrace(
                    accumulator=CompactAccumulator(),
                    lifecycle=PendingTrace(
                        logs=[],
                        first_seen_round=self.current_round,
                        last_seen_round=self.current_round,
                    ),
                    seen_log_keys={self._log_key(log) for log in additions},
                )
                self.pending[message_id] = pending
            else:
                seen = pending.seen_log_keys
                additions = [
                    log
                    for log in message_logs
                    if self._log_key(log) not in seen
                ]
                if additions:
                    seen.update(self._log_key(log) for log in additions)
                    pending.lifecycle.last_seen_round = self.current_round

            for log in additions:
                pending.accumulator.add(log)
                if log.hostname and log.mail_id:
                    self.resolved_queue_hops.add(
                        (_normalize_hostname(log.hostname), log.mail_id)
                    )

        self.max_pending_traces = max(
            self.max_pending_traces, len(self.pending)
        )
        self.max_queue_mappings = max(
            self.max_queue_mappings, len(self.queue_mapping)
        )
        self._collect_ready()

    def finish(self) -> None:
        while self.pending:
            self.current_round += 1
            self._collect_ready()

    def _record_mapping_reassignments(
        self, previous_mapping: dict[HopKey, str]
    ) -> None:
        for hop, message_id in self.queue_mapping.items():
            previous = previous_mapping.get(hop)
            if previous is None or previous == message_id:
                continue
            self.mapping_reassignments += 1
            if len(self.mapping_reassignment_sample) < 20:
                self.mapping_reassignment_sample.append(
                    {
                        "hostname": hop[0],
                        "queue_id": hop[1],
                        "previous_message_id": previous,
                        "message_id": message_id,
                    }
                )

    def _collect_ready(self) -> None:
        ready_ids = [
            message_id
            for message_id, pending in self.pending.items()
            if should_export_trace(
                pending.lifecycle,
                self.current_round,
                self.sleep_seconds,
                self.hold_rounds,
                self.max_trace_age_seconds,
            )
        ]
        for message_id in ready_ids:
            pending = self.pending.pop(message_id)
            self.comparison.add_trace(message_id, pending.accumulator)

        ready = set(ready_ids)
        for hop, message_id in list(self.queue_mapping.items()):
            if message_id in ready:
                del self.queue_mapping[hop]


def _full_plans(logs: list[LogEntry]) -> dict[str, HostPlan]:
    """Run the production algorithm on full LogEntry objects for validation."""
    plans: dict[str, HostPlan] = {}
    for message_id, message_logs in group_logs_by_message_id(logs).items():
        prepared = _prepare_hops(group_logs_by_hops(message_logs))
        if not prepared:
            continue
        placements, _, _, ordered = _plan_hops(message_id, prepared)
        sources = {
            hop: tuple(source for source, _ in placement.handoffs)
            for hop, placement in placements.items()
        }
        plans[message_id] = _project_plan(ordered, sources)
    return plans


def _full_bounds(
    logs: list[LogEntry],
) -> dict[HopKey, tuple[datetime, datetime]]:
    """Return raw hop bounds calculated by the full algorithm."""
    bounds: dict[HopKey, tuple[datetime, datetime]] = {}
    for message_logs in group_logs_by_message_id(logs).values():
        prepared = _prepare_hops(group_logs_by_hops(message_logs))
        for hop, summary in prepared.items():
            normalized_hop = (_normalize_hostname(hop[0]), hop[1])
            bounds[normalized_hop] = (summary.raw_start, summary.raw_end)
    return bounds


def _synthetic_log(
    second: float,
    host: str,
    queue_id: str,
    message: str,
    *,
    service: str = "postfix/smtp",
    queued_as: str | None = None,
    relay_host: str | None = None,
) -> LogEntry:
    base = datetime(2026, 9, 24, tzinfo=UTC)
    return LogEntry(
        datetime=(base + timedelta(seconds=second)).isoformat(),
        hostname=host,
        service=service,
        mail_id=queue_id,
        message=message,
        queued_as=queued_as,
        relay_host=relay_host,
    )


def run_synthetic_validation() -> dict[str, object]:
    """Validate topology, propagation, and queue-mapping cleanup."""
    linear = [
        _synthetic_log(0, "linear-a", "LA", "message-id=<linear@example.com>"),
        _synthetic_log(
            1,
            "linear-a",
            "LA",
            "status=sent, delays=0.1/0/0.1/0.8",
            queued_as="LB",
            relay_host="linear-b",
        ),
        _synthetic_log(
            2,
            "linear-b",
            "LB",
            "status=sent, delays=0.1/0/0.1/0.8",
            queued_as="LC",
            relay_host="linear-c",
        ),
        _synthetic_log(3, "linear-c", "LC", "removed"),
    ]
    branch = [
        _synthetic_log(
            10, "branch-a", "BA", "message-id=<branch@example.com>"
        ),
        _synthetic_log(
            11,
            "branch-a",
            "BA",
            "status=sent",
            queued_as="BB",
            relay_host="branch-b",
        ),
        _synthetic_log(
            12,
            "branch-a",
            "BA",
            "status=sent",
            queued_as="BC",
            relay_host="branch-c",
        ),
        _synthetic_log(13, "branch-b", "BB", "removed"),
        _synthetic_log(14, "branch-c", "BC", "removed"),
    ]
    ambiguous_handoff = [
        _synthetic_log(
            20,
            "ambiguous-a",
            "AA",
            "message-id=<ambiguous@example.com>",
        ),
        _synthetic_log(
            21,
            "ambiguous-a",
            "AA",
            "to=<first@example.com>, status=sent, delays=0.1/0/0.1/0.8",
            queued_as="AB",
            relay_host="ambiguous-b",
        ),
        _synthetic_log(
            22,
            "ambiguous-a",
            "AA",
            "to=<second@example.com>, status=sent, delays=0.1/0/0.1/0.8",
            queued_as="AB",
            relay_host="ambiguous-b",
        ),
        _synthetic_log(
            23,
            "ambiguous-b",
            "AB",
            "to=<third@example.com>, status=sent, delays=0.1/0/0.1/0.8",
        ),
    ]
    order_conflict = [
        _synthetic_log(
            30,
            "order-a",
            "OA",
            "message-id=<order@example.com>",
        ),
        _synthetic_log(
            31,
            "order-a",
            "OA",
            "status=sent",
            queued_as="OB",
            relay_host="order-b",
        ),
        _synthetic_log(20, "order-b", "OB", "queue active"),
    ]
    repeated_host = [
        _synthetic_log(
            40, "repeat-a", "RA1", "message-id=<repeat@example.com>"
        ),
        _synthetic_log(
            41,
            "repeat-a",
            "RA1",
            "status=sent",
            queued_as="RB",
            relay_host="repeat-b",
        ),
        _synthetic_log(
            42,
            "repeat-b",
            "RB",
            "status=sent",
            queued_as="RA2",
            relay_host="repeat-a",
        ),
        _synthetic_log(43, "repeat-a", "RA2", "removed"),
    ]
    cross_page_queue_logs = [
        _synthetic_log(
            50,
            "page-a",
            "PA",
            "status=sent",
            queued_as="PB",
            relay_host="page-b",
        ),
        _synthetic_log(
            51,
            "page-b",
            "PB",
            "=> recipient@example.com QT=1s RT=0.2s DT=0.3s",
            service="exim",
            queued_as="PC",
            relay_host="page-c",
        ),
        _synthetic_log(52, "page-c", "PC", "removed"),
    ]
    cross_page_seed = _synthetic_log(
        53, "page-a", "PA", "message-id=<page@example.com>"
    )
    first_page = (
        linear
        + branch
        + ambiguous_handoff
        + order_conflict
        + repeated_host
        + cross_page_queue_logs
    )
    second_page = [cross_page_seed]
    logs = first_page + second_page

    accumulator = CompactAccumulator()
    for page in (first_page, second_page):
        for log in page:
            accumulator.add(log)
    mapping = accumulator.resolve_message_ids()
    compact_hops = accumulator.resolved_hops(mapping)
    full_plans = _full_plans(logs)
    full_bounds = _full_bounds(logs)

    cases = {
        "linear": "linear@example.com",
        "branch": "branch@example.com",
        "ambiguous_handoff": "ambiguous@example.com",
        "ordering_conflict": "order@example.com",
        "repeated_host": "repeat@example.com",
        "cross_page_propagation": "page@example.com",
    }
    for name, message_id in cases.items():
        compact, ambiguous = plan_current_compact(
            accumulator, compact_hops[message_id]
        )
        assert (
            compact == full_plans[message_id]
        ), f"{name}: compact={compact}, full={full_plans[message_id]}"
        assert ambiguous is (name == "ambiguous_handoff")
        for hop in compact_hops[message_id]:
            assert accumulator.hops[hop].raw_bounds() == full_bounds[hop], (
                f"{name}: compact bounds="
                f"{accumulator.hops[hop].raw_bounds()}, "
                f"full bounds={full_bounds[hop]}"
            )

    assert (
        plan_timestamp(accumulator, compact_hops["order@example.com"]).order
        != full_plans["order@example.com"].order
    )
    assert len(compact_hops["repeat@example.com"]) == 3
    assert len(full_plans["repeat@example.com"].order) == 2
    assert plan_timestamp(
        accumulator, compact_hops["repeat@example.com"]
    ).edges == frozenset(
        {
            (ROOT, "repeat-a"),
            ("repeat-a", "repeat-b"),
            ("repeat-b", "repeat-a"),
        }
    )
    assert mapping[("page-c", "PC")] == "page@example.com"

    conflict_accumulator = CompactAccumulator()
    conflict_accumulator.add(
        _synthetic_log(60, "conflict", "CQ", "message-id=<first@example.com>")
    )
    conflict_accumulator.add(
        _synthetic_log(61, "conflict", "CQ", "message-id=<second@example.com>")
    )
    conflicts = conflict_accumulator.resolve_message_ids()
    assert isinstance(conflicts[("conflict", "CQ")], frozenset)

    lifecycle = LifecycleBenchmark(
        sleep_seconds=10,
        hold_rounds=1,
        go_back_seconds=10,
        max_trace_age_seconds=60,
    )
    first_reuse_logs = [
        _synthetic_log(
            70,
            "reuse",
            "RQ",
            "message-id=<reuse-first@example.com>",
        ),
        _synthetic_log(71, "reuse", "RQ", "removed"),
    ]
    second_reuse_logs = [
        _synthetic_log(
            100,
            "reuse",
            "RQ",
            "message-id=<reuse-second@example.com>",
        ),
        _synthetic_log(101, "reuse", "RQ", "removed"),
    ]
    lifecycle.process_round(
        first_reuse_logs,
        _parse_timestamp(first_reuse_logs[0].datetime),
    )
    lifecycle.process_round([], datetime(2026, 9, 24, 0, 1, 20, tzinfo=UTC))
    lifecycle.process_round(
        second_reuse_logs,
        _parse_timestamp(second_reuse_logs[0].datetime),
    )
    lifecycle.finish()
    assert lifecycle.comparison.exported_traces == 2
    assert lifecycle.comparison.unique_message_ids == {
        "reuse-first@example.com",
        "reuse-second@example.com",
    }
    assert lifecycle.mapping_reassignments == 0
    assert not lifecycle.pending
    assert not lifecycle.queue_mapping

    overlap = LifecycleBenchmark(
        sleep_seconds=10,
        hold_rounds=2,
        go_back_seconds=10,
        max_trace_age_seconds=60,
    )
    overlap.process_round(
        first_reuse_logs,
        _parse_timestamp(first_reuse_logs[0].datetime),
    )
    overlap.process_round(
        first_reuse_logs,
        datetime(2026, 9, 24, 0, 1, 20, tzinfo=UTC),
    )
    assert (
        overlap.pending["reuse-first@example.com"].lifecycle.last_seen_round
        == 1
    )
    assert overlap.active_queue_hops == 1

    return {
        "passed": True,
        "compact_matches_full": True,
        "cases": list(cases),
        "ambiguous_handoff_detection": True,
        "message_id_conflict_detection": True,
        "queue_mapping_cleanup": True,
        "query_overlap_deduplication": True,
    }


def replay_production_queries(
    config: Config, accumulator: LifecycleBenchmark
) -> tuple[int, float]:
    """Replay the fixed interval with production query windows."""
    interval = timedelta(seconds=config.tracing.sleep_seconds)
    go_back = timedelta(seconds=config.tracing.go_back_seconds)
    last_query_time = UTC_START
    rounds = 0
    next_progress = PROGRESS_INTERVAL
    started = perf_counter()

    while last_query_time < UTC_END:
        query_end = min(last_query_time + interval, UTC_END)
        query_start = max(UTC_START, last_query_time - go_back)
        logs = query_all_logs(config, query_start, query_end)
        accumulator.process_round(logs, last_query_time)
        rounds += 1
        last_query_time = query_end

        if accumulator.log_count >= next_progress:
            elapsed = perf_counter() - started
            print(
                f"Processed {accumulator.log_count:,} unique logs; "
                f"queried {accumulator.queried_log_count:,} logs; "
                f"active traces {len(accumulator.pending):,}; "
                f"queue mappings {len(accumulator.queue_mapping):,}; "
                f"elapsed {elapsed:.1f} seconds",
                flush=True,
            )
            next_progress += PROGRESS_INTERVAL

    accumulator.finish()
    return rounds, perf_counter() - started


def _percentage(numerator: int, denominator: int) -> float:
    return round(numerator / denominator * 100, 6) if denominator else 0.0


def build_result(
    config_path: Path,
    accumulator: LifecycleBenchmark,
    query_rounds: int,
    query_seconds: float,
    synthetic: dict[str, object],
) -> dict[str, object]:
    comparison, message_counts = accumulator.comparison.result()
    unresolved = len(
        accumulator.seen_queue_hops - accumulator.resolved_queue_hops
    )

    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "query": {
            "local_day": "2026-09-24",
            "timezone": "Asia/Taipei (+08:00)",
            "utc_start_inclusive": UTC_START.isoformat(),
            "utc_end_exclusive": UTC_END.isoformat(),
            "config": str(config_path),
        },
        "validation": {
            "expected_log_count": EXPECTED_LOG_COUNT,
            "actual_log_count": accumulator.log_count,
            "log_count_matches": accumulator.log_count == EXPECTED_LOG_COUNT,
            "synthetic": synthetic,
        },
        "dataset": {
            "query_rounds": query_rounds,
            "query_seconds": round(query_seconds, 3),
            "queried_logs_with_overlap": accumulator.queried_log_count,
            "queue_hops": accumulator.comparison.queue_hops,
            "unique_queue_keys": len(accumulator.seen_queue_hops),
            "relay_edges": accumulator.comparison.relay_edges,
            **message_counts,
        },
        "lifecycle": {
            "sleep_seconds": accumulator.sleep_seconds,
            "hold_rounds": accumulator.hold_rounds,
            "go_back_seconds": accumulator.go_back_seconds,
            "max_trace_age_seconds": accumulator.max_trace_age_seconds,
            "max_pending_traces": accumulator.max_pending_traces,
            "max_queue_mappings": accumulator.max_queue_mappings,
            "remaining_pending_traces": len(accumulator.pending),
            "remaining_queue_mappings": len(accumulator.queue_mapping),
        },
        "anomalies": {
            "queue_mapping_reassignments": (accumulator.mapping_reassignments),
            "queue_mapping_reassignment_sample": (
                accumulator.mapping_reassignment_sample
            ),
            "unresolved_queue_hops": unresolved,
            "logs_without_queue_id": accumulator.logs_without_queue_id,
        },
        "comparison": comparison,
    }


def print_summary(result: dict[str, Any]) -> None:
    dataset = result["dataset"]
    anomalies = result["anomalies"]
    comparison = result["comparison"]
    timestamp = comparison["timestamp"]
    difference = comparison["difference_percentage_points"]
    print(
        f"Logs {result['validation']['actual_log_count']:,}; "
        f"queue hops {dataset['queue_hops']:,}; "
        f"Message-IDs {dataset['message_ids']:,}; "
        f"exported traces {dataset['exported_traces']:,}; "
        f"multi-host traces {dataset['multi_host_traces']:,}"
    )
    print(
        f"Multi-host traces before ambiguous exclusion "
        f"{dataset['multi_host_traces_before_ambiguous_exclusion']:,}; "
        f"ambiguous handoff traces excluded "
        f"{dataset['ambiguous_handoff_traces_excluded']:,}"
    )
    print(
        f"Branch topologies {dataset['branch_topology_traces']:,}; "
        f"repeated-host hops {dataset['repeated_host_hop_traces']:,}; "
        f"queue-map reassignments "
        f"{anomalies['queue_mapping_reassignments']:,}; "
        f"unresolved queues {anomalies['unresolved_queue_hops']:,}"
    )
    labels = {
        "complete_host_order_match_pct": "Complete host-order match",
        "complete_topology_match_pct": "Complete topology match",
        "edge_precision_pct": "edge precision",
        "edge_recall_pct": "edge recall",
        "edge_f1_pct": "edge F1",
    }
    for key, label in labels.items():
        print(
            f"{label}: {timestamp[key]:.6f}% "
            f"({difference[key]:+.6f} percentage points vs current baseline)"
        )
        print(f"  Definition: {METRIC_DEFINITIONS[key]}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--result", type=Path, default=DEFAULT_RESULT)
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="Run synthetic validation without querying OpenSearch",
    )
    parser.add_argument(
        "--print-result",
        action="store_true",
        help="Print an existing result file without querying OpenSearch",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.print_result:
        result = json.loads(args.result.resolve().read_text(encoding="utf-8"))
        print_summary(result)
        return 0

    synthetic = run_synthetic_validation()
    print(
        "Synthetic validation passed: six topology cases, ambiguous handoff "
        "detection, cross-page propagation, conflict detection, and "
        "queue-mapping cleanup",
        flush=True,
    )
    if args.self_test:
        return 0

    config_path = args.config.resolve()
    config = load_config(str(config_path))
    accumulator = LifecycleBenchmark(
        sleep_seconds=config.tracing.sleep_seconds,
        hold_rounds=config.tracing.hold_rounds,
        go_back_seconds=config.tracing.go_back_seconds,
        max_trace_age_seconds=config.tracing.max_trace_age_seconds,
    )
    query_rounds, query_seconds = replay_production_queries(
        config, accumulator
    )
    if accumulator.log_count != EXPECTED_LOG_COUNT:
        print(
            f"Error: scroll returned {accumulator.log_count:,} logs; "
            f"expected {EXPECTED_LOG_COUNT:,}; comparison aborted",
            file=sys.stderr,
        )
        return 1

    result = build_result(
        config_path,
        accumulator,
        query_rounds,
        query_seconds,
        synthetic,
    )
    result_path = args.result.resolve()
    result_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print_summary(result)
    print(f"Result file: {result_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
