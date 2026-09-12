#!/usr/bin/env python3
"""
Host ablation robustness benchmark for full-log trace generation.

The workflow is:
  1. Send test emails and retrieve complete logs from OpenSearch.
  2. Generate baseline trace signatures from the full logs.
  3. Remove logs from one host at a time.
  4. Generate partial trace signatures from the remaining logs.
  5. Compare partial signatures with the full and projected baseline signatures.
"""

from __future__ import annotations

import argparse
import json
import logging
import statistics
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import NamedTuple

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_PROJECT_ROOT))

from mailtrace.config import load_config  # noqa: E402
from mailtrace.tracing.delay_parser import (  # noqa: E402
    DelayInfo,
    detect_mta_from_entries,
    get_parser_for_mta,
)
from mailtrace.tracing.query import (  # noqa: E402
    group_logs_by_hosts,
    group_logs_by_message_id,
    query_all_logs,
)

DEFAULT_CONFIG = "demo/docker/config.yaml"
DEFAULT_SCRIPT = "demo/docker/send_bulk_emails.sh"
DEFAULT_SIZE = 100
DEFAULT_WAIT = 15
DEFAULT_MAX_RETRIES = 3
DEFAULT_TRAFFIC_PATH = "random"

logging.getLogger("mailtrace").setLevel(logging.WARNING)


class HostSignature(NamedTuple):
    """Span structure for one generated host."""

    stages: tuple[str, ...]


TraceSignature = dict[str, HostSignature]
TraceSignatures = dict[str, TraceSignature]


class TraceClassCounts(NamedTuple):
    """Per-host ablation trace classification counts."""

    expected: int
    unchanged: int
    only_dropped_host_missing: int
    other_hosts_changed: int
    trace_disappeared: int


class RecallSummary(NamedTuple):
    """Mean retained structure recall for one dropped host."""

    retained_span_recall_mean: float
    retained_stage_recall_mean: float
    retained_edge_recall_mean: float


class HostAblationResult(NamedTuple):
    """Aggregated result for one dropped host."""

    dropped_host: str
    counts: TraceClassCounts
    recalls: RecallSummary


def resolve_path(path: str) -> Path:
    """Resolve a path relative to the project root."""
    resolved = Path(path)
    if not resolved.is_absolute():
        resolved = _PROJECT_ROOT / resolved
    return resolved.resolve()


def send_mailpolicy_dual_email() -> bool:
    """Send one email through the mailpolicy dual-recipient path."""
    proc = subprocess.run(
        [
            "swaks",
            "--to",
            "user1@example.com,user1@example2.com",
            "--from",
            "me@siriuskoan.one",
            "--helo",
            "siriuskoan.one",
            "--server",
            "127.0.0.1",
            "--port",
            "20025",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    return proc.returncode == 0


def send_mx_mailbox_email() -> bool:
    """Send one email through the mx to mailbox path."""
    proc = subprocess.run(
        [
            "swaks",
            "--to",
            "user2@example.com",
            "--from",
            "user1@example.com",
            "--server",
            "127.0.0.1",
            "--port",
            "10025",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    return proc.returncode == 0


def send_emails(script: Path, n: int, traffic_path: str) -> None:
    """Send demo emails for the selected traffic path."""
    if traffic_path == "random":
        proc = subprocess.run(
            ["bash", str(script), str(n)],
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            tail = (proc.stderr or proc.stdout or "")[-800:].strip()
            raise RuntimeError(
                f"{script.name} exited {proc.returncode}\n{tail}"
            )
        return

    if traffic_path == "mailpolicy-dual":
        failures = 0
        for _ in range(n):
            if not send_mailpolicy_dual_email():
                failures += 1
            time.sleep(0.1)
        if failures:
            raise RuntimeError(
                f"mailpolicy-dual sender failed {failures}/{n} email(s)"
            )
        return

    if traffic_path == "mx-mailbox":
        failures = 0
        for _ in range(n):
            if not send_mx_mailbox_email():
                failures += 1
            time.sleep(0.1)
        if failures:
            raise RuntimeError(
                f"mx-mailbox sender failed {failures}/{n} email(s)"
            )
        return

    raise ValueError(f"unsupported traffic path: {traffic_path}")


def generate_trace_signatures(logs) -> TraceSignatures:
    """Generate trace signatures from logs using the production parsing logic."""
    signatures: TraceSignatures = {}
    logs_by_message_id = group_logs_by_message_id(logs)

    for message_id, message_id_logs in logs_by_message_id.items():
        hosts_logs = group_logs_by_hosts(message_id_logs)
        host_signatures: TraceSignature = {}

        for host, host_logs in hosts_logs.items():
            mta = detect_mta_from_entries(host_logs)
            parser = get_parser_for_mta(mta)
            delay_info = DelayInfo()
            for log in host_logs:
                delay_info |= parser.parse(log.message)
            host_signatures[host] = HostSignature(
                stages=tuple(delay_info.get_delay_values().keys())
            )

        if host_signatures:
            signatures[message_id] = host_signatures

    return signatures


def project_trace(
    trace_signature: TraceSignature, dropped_host: str
) -> TraceSignature:
    """Remove one host from a baseline trace signature."""
    return {
        host: signature
        for host, signature in trace_signature.items()
        if host != dropped_host
    }


def span_set(
    trace_signature: TraceSignature,
) -> set[tuple[str, str, str | None]]:
    """Return host and stage spans as comparable identifiers."""
    spans: set[tuple[str, str, str | None]] = set()
    for host, signature in trace_signature.items():
        spans.add(("host", host, None))
        for stage in signature.stages:
            spans.add(("stage", host, stage))
    return spans


def stage_set(trace_signature: TraceSignature) -> set[tuple[str, str]]:
    """Return delay stage spans as comparable identifiers."""
    return {
        (host, stage)
        for host, signature in trace_signature.items()
        for stage in signature.stages
    }


def edge_set(
    trace_signature: TraceSignature,
) -> set[tuple[str, str, str | None]]:
    """Return root-host and host-stage edges as comparable identifiers."""
    edges: set[tuple[str, str, str | None]] = set()
    for host, signature in trace_signature.items():
        edges.add(("root", host, None))
        for stage in signature.stages:
            edges.add((host, "stage", stage))
    return edges


def recall(expected: set, observed: set) -> float:
    """Return recall over expected identifiers."""
    if not expected:
        return 1.0
    return len(expected & observed) / len(expected)


def classify_for_host(
    dropped_host: str,
    full_signatures: TraceSignatures,
    partial_signatures: TraceSignatures,
) -> HostAblationResult:
    """Classify all traces after dropping one host."""
    unchanged = 0
    only_dropped_host_missing = 0
    other_hosts_changed = 0
    trace_disappeared = 0
    span_recalls: list[float] = []
    stage_recalls: list[float] = []
    edge_recalls: list[float] = []

    for message_id, full_trace in full_signatures.items():
        projected = project_trace(full_trace, dropped_host)
        partial_trace = partial_signatures.get(message_id)

        if partial_trace is None:
            trace_disappeared += 1
            partial_for_recall: TraceSignature = {}
        elif partial_trace == full_trace:
            unchanged += 1
            partial_for_recall = partial_trace
        elif partial_trace == projected:
            only_dropped_host_missing += 1
            partial_for_recall = partial_trace
        else:
            other_hosts_changed += 1
            partial_for_recall = partial_trace

        span_recalls.append(
            recall(span_set(projected), span_set(partial_for_recall))
        )
        stage_recalls.append(
            recall(stage_set(projected), stage_set(partial_for_recall))
        )
        edge_recalls.append(
            recall(edge_set(projected), edge_set(partial_for_recall))
        )

    return HostAblationResult(
        dropped_host=dropped_host,
        counts=TraceClassCounts(
            expected=len(full_signatures),
            unchanged=unchanged,
            only_dropped_host_missing=only_dropped_host_missing,
            other_hosts_changed=other_hosts_changed,
            trace_disappeared=trace_disappeared,
        ),
        recalls=RecallSummary(
            retained_span_recall_mean=(
                statistics.mean(span_recalls) if span_recalls else 0.0
            ),
            retained_stage_recall_mean=(
                statistics.mean(stage_recalls) if stage_recalls else 0.0
            ),
            retained_edge_recall_mean=(
                statistics.mean(edge_recalls) if edge_recalls else 0.0
            ),
        ),
    )


def filter_signatures_by_hosts(
    signatures: TraceSignatures,
    required_hosts: set[str] | None,
) -> TraceSignatures:
    """Keep only traces whose host set equals the required host set."""
    if required_hosts is None:
        return signatures
    return {
        message_id: trace_signature
        for message_id, trace_signature in signatures.items()
        if set(trace_signature) == required_hosts
    }


def filter_logs_by_message_ids(logs, message_ids: set[str]):
    """Keep logs that belong to selected message IDs."""
    grouped_logs = group_logs_by_message_id(logs)
    selected_logs = []
    for message_id in message_ids:
        selected_logs.extend(grouped_logs.get(message_id, []))
    return selected_logs


def result_to_json(result: HostAblationResult) -> dict[str, int | float | str]:
    """Return a JSON-serialisable host ablation result."""
    counts = result.counts
    recalls = result.recalls
    expected = counts.expected
    return {
        "dropped_host": result.dropped_host,
        "expected": expected,
        "unchanged": counts.unchanged,
        "only_dropped_host_missing": counts.only_dropped_host_missing,
        "other_hosts_changed": counts.other_hosts_changed,
        "trace_disappeared": counts.trace_disappeared,
        "unchanged_pct": (
            round(counts.unchanged / expected * 100, 3) if expected else 0.0
        ),
        "only_dropped_host_missing_pct": (
            round(counts.only_dropped_host_missing / expected * 100, 3)
            if expected
            else 0.0
        ),
        "other_hosts_changed_pct": (
            round(counts.other_hosts_changed / expected * 100, 3)
            if expected
            else 0.0
        ),
        "trace_disappeared_pct": (
            round(counts.trace_disappeared / expected * 100, 3)
            if expected
            else 0.0
        ),
        "retained_span_recall_mean": round(
            recalls.retained_span_recall_mean, 5
        ),
        "retained_stage_recall_mean": round(
            recalls.retained_stage_recall_mean, 5
        ),
        "retained_edge_recall_mean": round(
            recalls.retained_edge_recall_mean, 5
        ),
    }


def print_table(results: list[HostAblationResult]) -> None:
    """Print host ablation results."""
    headers = [
        "dropped_host",
        "expected",
        "unchanged",
        "only_dropped_host_missing",
        "other_hosts_changed",
        "trace_disappeared",
        "span_recall",
        "stage_recall",
        "edge_recall",
    ]
    rows = []
    for result in results:
        rows.append(
            [
                result.dropped_host,
                str(result.counts.expected),
                str(result.counts.unchanged),
                str(result.counts.only_dropped_host_missing),
                str(result.counts.other_hosts_changed),
                str(result.counts.trace_disappeared),
                f"{result.recalls.retained_span_recall_mean:.5f}",
                f"{result.recalls.retained_stage_recall_mean:.5f}",
                f"{result.recalls.retained_edge_recall_mean:.5f}",
            ]
        )

    widths = [
        max(len(header), *(len(row[i]) for row in rows))
        for i, header in enumerate(headers)
    ]
    print(
        " | ".join(header.rjust(widths[i]) for i, header in enumerate(headers))
    )
    print("-+-".join("-" * width for width in widths))
    for row in rows:
        print(" | ".join(cell.rjust(widths[i]) for i, cell in enumerate(row)))


def collect_complete_logs(
    config,
    script_path: Path,
    size: int,
    wait_seconds: int,
    max_retries: int,
    skip_send: bool,
    skip_window: int,
    traffic_path: str,
    required_hosts: set[str] | None,
):
    """Collect a full-log window with the requested number of traces."""
    last_logs = []
    last_signatures: TraceSignatures = {}
    for attempt in range(1, max_retries + 1):
        if skip_send:
            end_dt = datetime.now(UTC)
            start_dt = end_dt - timedelta(minutes=skip_window)
        else:
            start_dt = datetime.now(UTC)
            send_emails(script_path, size, traffic_path)
            send_end_dt = datetime.now(UTC)
            time.sleep(wait_seconds)
            end_dt = send_end_dt + timedelta(seconds=wait_seconds + 5)

        logs = query_all_logs(config, start_dt, end_dt)
        signatures = generate_trace_signatures(logs)
        signatures = filter_signatures_by_hosts(signatures, required_hosts)
        last_logs = logs
        last_signatures = signatures
        if skip_send or len(signatures) == size:
            return filter_logs_by_message_ids(logs, set(signatures))

        print(
            f"Attempt {attempt}/{max_retries}: expected {size}, "
            f"got {len(signatures)}; retrying",
            flush=True,
        )

    return filter_logs_by_message_ids(last_logs, set(last_signatures))


def build_parser() -> argparse.ArgumentParser:
    """Build CLI parser."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=DEFAULT_CONFIG)
    parser.add_argument("--script", default=DEFAULT_SCRIPT)
    parser.add_argument("--size", type=int, default=DEFAULT_SIZE)
    parser.add_argument("--wait", type=int, default=DEFAULT_WAIT)
    parser.add_argument("--max-retries", type=int, default=DEFAULT_MAX_RETRIES)
    parser.add_argument("--skip-send", action="store_true")
    parser.add_argument("--skip-window", type=int, default=60)
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--traffic-path",
        choices=["random", "mailpolicy-dual", "mx-mailbox"],
        default=DEFAULT_TRAFFIC_PATH,
        help="Email traffic path to generate",
    )
    parser.add_argument(
        "--required-hosts",
        nargs="+",
        default=None,
        help="Keep only baseline traces whose host set exactly matches these hosts",
    )
    parser.add_argument(
        "--hosts",
        nargs="+",
        default=None,
        help="Limit ablation to these hosts",
    )
    return parser


def main() -> int:
    """Run host ablation robustness benchmark."""
    args = build_parser().parse_args()
    logging.basicConfig(level=logging.WARNING)

    config_path = resolve_path(args.config)
    script_path = resolve_path(args.script)
    if not config_path.exists():
        print(f"ERROR: config not found: {config_path}", file=sys.stderr)
        return 1
    if not args.skip_send and not script_path.exists():
        print(f"ERROR: script not found: {script_path}", file=sys.stderr)
        return 1

    config = load_config(str(config_path))
    required_hosts = set(args.required_hosts) if args.required_hosts else None
    logs = collect_complete_logs(
        config=config,
        script_path=script_path,
        size=args.size,
        wait_seconds=args.wait,
        max_retries=args.max_retries,
        skip_send=args.skip_send,
        skip_window=args.skip_window,
        traffic_path=args.traffic_path,
        required_hosts=required_hosts,
    )
    full_signatures = generate_trace_signatures(logs)
    if not full_signatures:
        print("ERROR: no baseline traces generated", file=sys.stderr)
        return 1

    all_hosts = sorted({log.hostname for log in logs})
    selected_hosts = args.hosts if args.hosts is not None else all_hosts
    results: list[HostAblationResult] = []

    print(
        f"Baseline traces={len(full_signatures)}, logs={len(logs)}, "
        f"hosts={','.join(all_hosts)}",
        flush=True,
    )

    for dropped_host in selected_hosts:
        partial_logs = [log for log in logs if log.hostname != dropped_host]
        partial_signatures = generate_trace_signatures(partial_logs)
        results.append(
            classify_for_host(
                dropped_host=dropped_host,
                full_signatures=full_signatures,
                partial_signatures=partial_signatures,
            )
        )

    print()
    if args.json:
        print(json.dumps([result_to_json(result) for result in results]))
    else:
        print_table(results)

    return 0


if __name__ == "__main__":
    sys.exit(main())
