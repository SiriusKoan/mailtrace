#!/usr/bin/env python3
"""
Random missing-log correctness benchmark for full-log reconstruction.

For each trial, the script sends demo emails, queries the complete OpenSearch
log window, builds a full-log baseline, randomly removes a configured fraction
of log entries, reconstructs traces from the remaining logs, and compares the
partial reconstruction with the full-log baseline.
"""

from __future__ import annotations

import argparse
import json
import logging
import random
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
DEFAULT_TRIALS = 10
DEFAULT_WAIT = 15
DEFAULT_MAX_RETRIES = 3
DEFAULT_MISSING_RATIOS = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7]

logging.getLogger("mailtrace").setLevel(logging.WARNING)


class HostSignature(NamedTuple):
    """Span structure for one reconstructed host."""

    stages: tuple[str, ...]


TraceSignature = dict[str, HostSignature]
TraceSignatures = dict[str, TraceSignature]


class CorrectnessResult(NamedTuple):
    """Correctness metrics for one missing-log sample."""

    expected: int
    generated: int
    exact: int
    malformed: int
    missing: int
    span_recall: float
    stage_recall: float
    edge_recall: float

    @property
    def exact_rate(self) -> float:
        """Return exact-match rate over baseline traces."""
        return self.exact / self.expected if self.expected else 0.0


class RatioSummary(NamedTuple):
    """Average correctness metrics for one missing-log ratio."""

    missing_ratio: float
    trials: int
    expected_mean: float
    logs_mean: float
    removed_logs_mean: float
    exact_pct_mean: float
    malformed_pct_mean: float
    missing_pct_mean: float
    span_recall_mean: float
    delay_recall_mean: float
    edge_recall_mean: float


def resolve_path(path: str) -> Path:
    """Resolve a path relative to the project root."""
    resolved = Path(path)
    if not resolved.is_absolute():
        resolved = _PROJECT_ROOT / resolved
    return resolved.resolve()


def send_emails(script: Path, n: int) -> None:
    """Run the demo email sender."""
    proc = subprocess.run(
        ["bash", str(script), str(n)],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "")[-800:].strip()
        raise RuntimeError(f"{script.name} exited {proc.returncode}\n{tail}")


def generate_trace_signatures(logs) -> TraceSignatures:
    """Generate trace signatures from logs using production parsing logic."""
    signatures: TraceSignatures = {}
    logs_by_message_id = group_logs_by_message_id(logs)

    for message_id, message_id_logs in logs_by_message_id.items():
        host_signatures: TraceSignature = {}
        for host, host_logs in group_logs_by_hosts(message_id_logs).items():
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


def filter_logs_by_message_ids(logs, message_ids: set[str]):
    """Keep logs that belong to selected message IDs."""
    grouped_logs = group_logs_by_message_id(logs)
    selected_logs = []
    for message_id in message_ids:
        selected_logs.extend(grouped_logs.get(message_id, []))
    return selected_logs


def collect_complete_logs(
    config,
    script_path: Path,
    size: int,
    wait_seconds: int,
    max_retries: int,
    skip_send: bool,
    skip_window: int,
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
            send_emails(script_path, size)
            send_end_dt = datetime.now(UTC)
            time.sleep(wait_seconds)
            end_dt = send_end_dt + timedelta(seconds=wait_seconds + 5)

        logs = query_all_logs(config, start_dt, end_dt)
        signatures = generate_trace_signatures(logs)
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


def remove_random_logs(logs, missing_ratio: float, rng: random.Random):
    """Return logs after randomly removing the configured fraction."""
    if not 0 <= missing_ratio <= 1:
        raise ValueError("missing_ratio must be between 0 and 1")

    remove_count = round(len(logs) * missing_ratio)
    if remove_count <= 0:
        return list(logs), 0

    removed_indexes = set(rng.sample(range(len(logs)), remove_count))
    return [
        log for index, log in enumerate(logs) if index not in removed_indexes
    ], remove_count


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


def mean_recall(
    full_signatures: TraceSignatures,
    partial_signatures: TraceSignatures,
    set_builder,
) -> float:
    """Return mean structure recall across baseline traces."""
    recalls = []
    for message_id, full_trace in full_signatures.items():
        partial_trace = partial_signatures.get(message_id, {})
        recalls.append(
            recall(set_builder(full_trace), set_builder(partial_trace))
        )
    return statistics.mean(recalls) if recalls else 0.0


def compare_signatures(
    full_signatures: TraceSignatures,
    partial_signatures: TraceSignatures,
) -> CorrectnessResult:
    """Compare partial reconstruction with full-log baseline."""
    exact = 0
    malformed = 0
    missing = 0

    for message_id, full_trace in full_signatures.items():
        partial_trace = partial_signatures.get(message_id)
        if partial_trace is None:
            missing += 1
        elif partial_trace == full_trace:
            exact += 1
        else:
            malformed += 1

    return CorrectnessResult(
        expected=len(full_signatures),
        generated=len(partial_signatures),
        exact=exact,
        malformed=malformed,
        missing=missing,
        span_recall=mean_recall(full_signatures, partial_signatures, span_set),
        stage_recall=mean_recall(
            full_signatures, partial_signatures, stage_set
        ),
        edge_recall=mean_recall(full_signatures, partial_signatures, edge_set),
    )


def summarise_ratio(
    missing_ratio: float,
    rows: list[dict[str, float]],
) -> RatioSummary:
    """Return mean metrics for one missing-log ratio."""
    return RatioSummary(
        missing_ratio=missing_ratio,
        trials=len(rows),
        expected_mean=statistics.mean(row["expected"] for row in rows),
        logs_mean=statistics.mean(row["logs"] for row in rows),
        removed_logs_mean=statistics.mean(row["removed_logs"] for row in rows),
        exact_pct_mean=statistics.mean(row["exact_pct"] for row in rows),
        malformed_pct_mean=statistics.mean(
            row["malformed_pct"] for row in rows
        ),
        missing_pct_mean=statistics.mean(row["missing_pct"] for row in rows),
        span_recall_mean=statistics.mean(row["span_recall"] for row in rows),
        delay_recall_mean=statistics.mean(row["delay_recall"] for row in rows),
        edge_recall_mean=statistics.mean(row["edge_recall"] for row in rows),
    )


def summary_to_json(summary: RatioSummary) -> dict[str, int | float]:
    """Return a JSON-serialisable summary."""
    return {
        "missing_ratio": summary.missing_ratio,
        "trials": summary.trials,
        "expected_mean": round(summary.expected_mean, 3),
        "logs_mean": round(summary.logs_mean, 3),
        "removed_logs_mean": round(summary.removed_logs_mean, 3),
        "exact_pct_mean": round(summary.exact_pct_mean, 3),
        "malformed_pct_mean": round(summary.malformed_pct_mean, 3),
        "missing_pct_mean": round(summary.missing_pct_mean, 3),
        "span_recall_mean": round(summary.span_recall_mean, 5),
        "delay_recall_mean": round(summary.delay_recall_mean, 5),
        "edge_recall_mean": round(summary.edge_recall_mean, 5),
    }


def print_table(summaries: list[RatioSummary]) -> None:
    """Print averaged random missing-log benchmark results."""
    headers = [
        "missing_pct",
        "trials",
        "expected",
        "logs",
        "removed",
        "exact_pct",
        "malformed_pct",
        "missing_pct",
        "span_recall",
        "delay_recall",
        "edge_recall",
    ]
    rows = [
        [
            f"{summary.missing_ratio * 100:.0f}",
            str(summary.trials),
            f"{summary.expected_mean:.2f}",
            f"{summary.logs_mean:.2f}",
            f"{summary.removed_logs_mean:.2f}",
            f"{summary.exact_pct_mean:.2f}",
            f"{summary.malformed_pct_mean:.2f}",
            f"{summary.missing_pct_mean:.2f}",
            f"{summary.span_recall_mean:.5f}",
            f"{summary.delay_recall_mean:.5f}",
            f"{summary.edge_recall_mean:.5f}",
        ]
        for summary in summaries
    ]
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


def build_parser() -> argparse.ArgumentParser:
    """Build CLI parser."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=DEFAULT_CONFIG)
    parser.add_argument("--script", default=DEFAULT_SCRIPT)
    parser.add_argument("--size", type=int, default=DEFAULT_SIZE)
    parser.add_argument("--trials", type=int, default=DEFAULT_TRIALS)
    parser.add_argument("--wait", type=int, default=DEFAULT_WAIT)
    parser.add_argument("--max-retries", type=int, default=DEFAULT_MAX_RETRIES)
    parser.add_argument("--skip-send", action="store_true")
    parser.add_argument("--skip-window", type=int, default=60)
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--missing-ratios",
        nargs="+",
        type=float,
        default=DEFAULT_MISSING_RATIOS,
    )
    return parser


def main() -> int:
    """Run random missing-log correctness benchmark."""
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
    rng = random.Random(args.seed)
    rows_by_ratio: dict[float, list[dict[str, float]]] = {
        ratio: [] for ratio in args.missing_ratios
    }

    for trial in range(1, args.trials + 1):
        logs = collect_complete_logs(
            config=config,
            script_path=script_path,
            size=args.size,
            wait_seconds=args.wait,
            max_retries=args.max_retries,
            skip_send=args.skip_send,
            skip_window=args.skip_window,
        )
        full_signatures = generate_trace_signatures(logs)
        if not full_signatures:
            print("ERROR: no baseline traces generated", file=sys.stderr)
            return 1

        print(
            f"Trial {trial}/{args.trials}: "
            f"baseline_traces={len(full_signatures)}, logs={len(logs)}",
            flush=True,
        )

        for missing_ratio in args.missing_ratios:
            partial_logs, removed_count = remove_random_logs(
                logs, missing_ratio, rng
            )
            partial_signatures = generate_trace_signatures(partial_logs)
            result = compare_signatures(full_signatures, partial_signatures)
            expected = float(result.expected)
            rows_by_ratio[missing_ratio].append(
                {
                    "expected": expected,
                    "logs": float(len(logs)),
                    "removed_logs": float(removed_count),
                    "exact_pct": result.exact_rate * 100,
                    "malformed_pct": (
                        result.malformed / expected * 100 if expected else 0.0
                    ),
                    "missing_pct": (
                        result.missing / expected * 100 if expected else 0.0
                    ),
                    "span_recall": result.span_recall,
                    "delay_recall": result.stage_recall,
                    "edge_recall": result.edge_recall,
                }
            )

    summaries = [
        summarise_ratio(missing_ratio, rows)
        for missing_ratio, rows in rows_by_ratio.items()
    ]

    print()
    if args.json:
        print(json.dumps([summary_to_json(summary) for summary in summaries]))
    else:
        print_table(summaries)

    return 0


if __name__ == "__main__":
    sys.exit(main())
