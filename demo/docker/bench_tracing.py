#!/usr/bin/env python3
"""
Performance benchmark for the mailtrace.tracing module.

Sends N emails via send_bulk_emails.sh, waits for OpenSearch indexing,
then runs one tracing cycle — identical to the EmailTracesGenerator loop —
measuring the three phases:

  - query_logs:    fetch log entries from OpenSearch
  - create_spans:  parse delays and build OTLP span tree
  - flush_traces:  export spans to the OTLP collector

It also compares the trace structure generated from the full log window
against the structure expected from the same complete log set:

  - exact_match:   generated trace has the expected host/stage spans
  - malformed:     trace exists but its host/stage structure differs
  - missing_event: expected trace was not generated at all

Results are printed as a formatted summary table.

Usage (from the project root):
    python demo/docker/bench_tracing.py [options]

Quick start (demo stack must be running):
    python demo/docker/bench_tracing.py \\
        --config   demo/docker/config.yaml \\
        --script   demo/docker/send_bulk_emails.sh \\
        --endpoint http://localhost:4317 \\
        --wait     15

Use --skip-send to benchmark against data already in OpenSearch:
    python demo/docker/bench_tracing.py --skip-send --skip-window 120
"""

from __future__ import annotations

import argparse
import logging
import re
import statistics
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import perf_counter
from typing import NamedTuple, Optional

# ---------------------------------------------------------------------------
# Make the package importable regardless of the working directory.
# ---------------------------------------------------------------------------
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_PROJECT_ROOT))

from opentelemetry import trace  # noqa: E402  (needs sys.path fix above)

from mailtrace.config import load_config  # noqa: E402
from mailtrace.tracing.delay_parser import (  # noqa: E402
    DelayInfo,
    EXIM_DELAY_STAGES,
    POSTFIX_DELAY_STAGES,
    detect_mta_from_entries,
    get_parser_for_mta,
)
from mailtrace.tracing.otel import (  # noqa: E402
    create_delay_spans,
    create_host_span,
    create_root_span,
    dt_to_ns,
    flush_traces,
    init_exporter,
)
from mailtrace.tracing.query import (  # noqa: E402
    group_logs_by_hosts,
    group_logs_by_message_id,
    query_all_logs,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
DEFAULT_SIZES: list[int] = [10, 50, 100, 1000]
DEFAULT_CONFIG = "demo/docker/config.yaml"
DEFAULT_SCRIPT = "demo/docker/send_bulk_emails.sh"
DEFAULT_ENDPOINT = "http://localhost:4317"
DEFAULT_WAIT = 15  # seconds to wait for OpenSearch indexing
DEFAULT_RUNS = 1  # measurement repetitions per size
DEFAULT_SKIP_WINDOW = 60  # minutes to look back when --skip-send is used
DEFAULT_MAX_RETRIES = 3  # max re-send attempts for incomplete full-log windows
_EXIM_DELIVERY_RECIPIENT_RE = re.compile(
    r"\s(?:=>|->|\*\*)\s+([^\s<>:]+@[^\s<>:]+)"
)

# Silence mailtrace's own loggers so benchmark output stays clean.
logging.getLogger("mailtrace").setLevel(logging.WARNING)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------
class RunResult(NamedTuple):
    """Timing result for a single benchmark run."""

    n_emails: int
    n_expected: int
    n_traces: int
    exact_matches: int
    malformed_traces: int
    missing_events: int
    exact_match_rate: float
    query_time: float  # seconds
    spans_time: float  # seconds
    flush_time: float  # seconds
    total_time: float  # seconds
    avg_per_trace: float  # seconds / trace


class HostSignature(NamedTuple):
    """Span structure for one expected or generated host."""

    stages: tuple[str, ...]


TraceSignature = dict[str, HostSignature]
TraceSignatures = dict[str, TraceSignature]


class CorrectnessResult(NamedTuple):
    """Structural comparison between full logs and generated traces."""

    expected: int
    generated: int
    exact: int
    malformed: int
    missing: int

    @property
    def exact_rate(self) -> float:
        """Return the exact-match rate."""
        return self.exact / self.expected if self.expected else 0.0


# ---------------------------------------------------------------------------
# Email sending
# ---------------------------------------------------------------------------
def send_emails(script: Path, n: int) -> bool:
    """Invoke send_bulk_emails.sh to create *n* test emails.

    Returns True on success, False if the script exited with an error.
    """
    print(f"  Sending {n} email(s) via {script.name} ...", flush=True)
    proc = subprocess.run(
        ["bash", str(script), str(n)],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "")[-600:].strip()
        print(f"  WARNING: {script.name} exited {proc.returncode}.")
        if tail:
            print(f"  --- script output (tail) ---\n{tail}\n  ---")
        return False
    return True


# ---------------------------------------------------------------------------
# Core benchmark logic  (mirrors EmailTracesGenerator.run() exactly)
# ---------------------------------------------------------------------------
def _extract_sender_recipient(
    logs,
) -> tuple[Optional[str], Optional[list[str]]]:
    """Extract sender and recipients from email logs.

    Args:
        logs: List of log entries for an email trace.

    Returns:
        Tuple of (sender, recipients), sender may be None.
    """
    sender: Optional[str] = None
    recipients: list[str] = []
    seen_recipients = set()

    for log in logs:
        if not sender:
            from_match = re.search(r"from=<([^>]*)>", log.message)
            if from_match:
                sender = from_match.group(1)

        # Extract all recipients from this log
        to_match = re.search(r"to=<([^>]*)>", log.message)
        if to_match:
            recipient = to_match.group(1)
            if recipient not in seen_recipients:
                recipients.append(recipient)
                seen_recipients.add(recipient)

        exim_delivery_match = _EXIM_DELIVERY_RECIPIENT_RE.search(log.message)
        if exim_delivery_match:
            recipient = exim_delivery_match.group(1)
            if recipient not in seen_recipients:
                recipients.append(recipient)
                seen_recipients.add(recipient)

    return sender, recipients if recipients else None


def _expected_stages_from_logs(logs) -> tuple[str, ...]:
    """Infer expected delay span names from raw log signals."""
    postfix_delay_pattern = (
        r"\bdelays=\d+(?:\.\d+)?/\d+(?:\.\d+)?/" r"\d+(?:\.\d+)?/\d+(?:\.\d+)?"
    )
    has_postfix_delay = any(
        re.search(postfix_delay_pattern, log.message) for log in logs
    )
    if has_postfix_delay:
        return tuple(POSTFIX_DELAY_STAGES)

    has_qt = any(
        re.search(r"\bQT=\d+(?:\.\d+)?s?", log.message) for log in logs
    )
    has_rt = any(
        re.search(r"\bRT=\d+(?:\.\d+)?s?", log.message) for log in logs
    )
    has_dt = any(
        re.search(r"\bDT=\d+(?:\.\d+)?s?", log.message) for log in logs
    )
    if has_qt and has_rt and has_dt:
        return tuple(EXIM_DELAY_STAGES)

    return ()


def _build_expected_signatures(logs_by_message_id) -> TraceSignatures:
    """Build expected trace structures from the complete log set."""
    signatures: TraceSignatures = {}
    for message_id, message_id_logs in logs_by_message_id.items():
        hosts_logs = group_logs_by_hosts(message_id_logs)
        host_signatures: TraceSignature = {}
        for host, host_logs in hosts_logs.items():
            host_signatures[host] = HostSignature(
                stages=_expected_stages_from_logs(host_logs)
            )
        if host_signatures:
            signatures[message_id] = host_signatures
    return signatures


def _compare_trace_signatures(
    expected: TraceSignatures, generated: TraceSignatures
) -> CorrectnessResult:
    """Compare expected and generated trace structures."""
    exact = 0
    malformed = 0
    missing = 0

    for message_id, expected_trace in expected.items():
        generated_trace = generated.get(message_id)
        if generated_trace is None:
            missing += 1
        elif generated_trace == expected_trace:
            exact += 1
        else:
            malformed += 1

    return CorrectnessResult(
        expected=len(expected),
        generated=len(generated),
        exact=exact,
        malformed=malformed,
        missing=missing,
    )


def _run_tracing_cycle(
    config, start_dt: datetime, end_dt: datetime
) -> RunResult | None:
    """Execute one full tracing cycle and return per-phase timings.

    The implementation deliberately mirrors ``EmailTracesGenerator.run()``
    step-for-step so that the measured times are representative of the
    real workload.

    Returns None if OpenSearch returned no log entries for the window.
    """
    # ------------------------------------------------------------------
    # Phase 1 — query logs
    # ------------------------------------------------------------------
    t0 = perf_counter()
    logs = query_all_logs(config, start_dt, end_dt)
    t1 = perf_counter()
    query_time = t1 - t0

    if not logs:
        return None

    # ------------------------------------------------------------------
    # Phase 2 — group, parse delays, create OTLP spans
    # ------------------------------------------------------------------
    t2 = perf_counter()
    trace_count = 0

    logs_by_message_id = group_logs_by_message_id(logs)
    expected_signatures = _build_expected_signatures(logs_by_message_id)
    generated_signatures: TraceSignatures = {}

    for message_id, message_id_logs in logs_by_message_id.items():
        hosts_logs = group_logs_by_hosts(message_id_logs)

        host_info: dict[str, tuple[DelayInfo, datetime, datetime]] = {}
        generated_hosts: TraceSignature = {}

        for host, host_logs in hosts_logs.items():
            mta = detect_mta_from_entries(host_logs)
            parser = get_parser_for_mta(mta)
            delay_info = DelayInfo()
            for log in host_logs:
                delay_info |= parser.parse(log.message)
            delay_values = delay_info.get_delay_values()
            generated_hosts[host] = HostSignature(
                stages=tuple(delay_values.keys())
            )

            host_start = min(
                datetime.fromisoformat(log.datetime.replace("Z", "+00:00"))
                for log in host_logs
            )
            host_end = host_start + timedelta(seconds=delay_info.total_delay)
            host_info[host] = (delay_info, host_start, host_end)

        if not host_info:
            continue

        trace_count += 1
        generated_signatures[message_id] = generated_hosts

        root_start = min(info[1] for info in host_info.values())
        root_end = max(info[2] for info in host_info.values())

        # Extract sender and recipients from logs
        sender, recipients = _extract_sender_recipient(message_id_logs)

        # Create root span with sender and recipients attributes
        root_span = create_root_span(
            message_id, root_start, sender=sender, recipients=recipients
        )
        root_ctx = trace.set_span_in_context(root_span)

        for host, (delays, host_start, host_end) in host_info.items():
            # Extract sender and recipients specific to this host
            host_sender, host_recipients = _extract_sender_recipient(
                hosts_logs[host]
            )
            # Extract the first queue ID for this host from the logs
            host_queue_id = next(
                (log.mail_id for log in hosts_logs[host] if log.mail_id), None
            )
            host_next_host = next(
                (log.relay_host for log in hosts_logs[host] if log.relay_host),
                None,
            )
            host_span = create_host_span(
                host,
                host_start,
                root_ctx,
                message_id=message_id,
                sender=host_sender,
                recipients=host_recipients,
                queue_id=host_queue_id,
                next_host=host_next_host,
            )
            host_ctx = trace.set_span_in_context(host_span)
            create_delay_spans(delays, host, host_start, host_ctx)
            host_span.end(end_time=dt_to_ns(host_end))

        root_span.end(end_time=int(root_end.timestamp() * 1e9))

    t3 = perf_counter()
    spans_time = t3 - t2
    correctness = _compare_trace_signatures(
        expected_signatures, generated_signatures
    )

    # ------------------------------------------------------------------
    # Phase 3 — flush traces to OTLP collector
    # ------------------------------------------------------------------
    t4 = perf_counter()
    flush_traces()
    t5 = perf_counter()
    flush_time = t5 - t4

    total = query_time + spans_time + flush_time
    avg = total / trace_count if trace_count > 0 else 0.0

    return RunResult(
        n_emails=0,  # filled in by the caller
        n_expected=correctness.expected,
        n_traces=trace_count,
        exact_matches=correctness.exact,
        malformed_traces=correctness.malformed,
        missing_events=correctness.missing,
        exact_match_rate=correctness.exact_rate,
        query_time=query_time,
        spans_time=spans_time,
        flush_time=flush_time,
        total_time=total,
        avg_per_trace=avg,
    )


# ---------------------------------------------------------------------------
# Aggregation helpers
# ---------------------------------------------------------------------------
def _average_result(n_emails: int, runs: list[RunResult]) -> RunResult:
    """Return a RunResult whose timings are the average (mean) of *runs*."""

    def avg(field: str) -> float:
        return statistics.mean(getattr(r, field) for r in runs)

    n_expected = round(statistics.mean(r.n_expected for r in runs))
    n_traces = round(statistics.mean(r.n_traces for r in runs))
    exact_matches = round(statistics.mean(r.exact_matches for r in runs))
    malformed_traces = round(statistics.mean(r.malformed_traces for r in runs))
    missing_events = round(statistics.mean(r.missing_events for r in runs))
    total = avg("total_time")
    avg_per_trace = total / n_traces if n_traces > 0 else 0.0
    return RunResult(
        n_emails=n_emails,
        n_expected=n_expected,
        n_traces=n_traces,
        exact_matches=exact_matches,
        malformed_traces=malformed_traces,
        missing_events=missing_events,
        exact_match_rate=avg("exact_match_rate"),
        query_time=avg("query_time"),
        spans_time=avg("spans_time"),
        flush_time=avg("flush_time"),
        total_time=total,
        avg_per_trace=avg_per_trace,
    )


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------
_COL_LABELS = [
    "Emails",
    "Expected",
    "Traces",
    "Exact",
    "Malformed",
    "Missing",
    "Exact %",
    "query_logs (s)",
    "create_spans (s)",
    "flush_traces (s)",
    "Total (s)",
    "Avg / trace (s)",
]
# minimum width for each column (grows if content is wider)
_COL_MIN_W = [8, 8, 8, 8, 10, 8, 8, 14, 16, 16, 10, 16]


def _row_cells(r: RunResult) -> list[str]:
    return [
        str(r.n_emails),
        str(r.n_expected),
        str(r.n_traces),
        str(r.exact_matches),
        str(r.malformed_traces),
        str(r.missing_events),
        f"{r.exact_match_rate * 100:.2f}",
        f"{r.query_time:.4f}",
        f"{r.spans_time:.4f}",
        f"{r.flush_time:.4f}",
        f"{r.total_time:.4f}",
        f"{r.avg_per_trace:.6f}",
    ]


def _compute_widths(results: list[RunResult]) -> list[int]:
    widths = [max(w, len(h)) for w, h in zip(_COL_MIN_W, _COL_LABELS)]
    for r in results:
        for i, cell in enumerate(_row_cells(r)):
            widths[i] = max(widths[i], len(cell))
    return widths


def _hline(
    widths: list[int], left="+-", mid="-+-", right="-+", fill="-"
) -> str:
    return left + mid.join(fill * w for w in widths) + right


def _data_row(cells: list[str], widths: list[int]) -> str:
    return "| " + " | ".join(c.rjust(w) for c, w in zip(cells, widths)) + " |"


def print_results_table(results: list[RunResult], runs: int) -> None:
    """Render a formatted table of benchmark results to stdout."""
    widths = _compute_widths(results)
    total_w = sum(widths) + 3 * len(widths) + 1

    heading = "TRACING MODULE PERFORMANCE BENCHMARK"
    if runs > 1:
        heading += f"  (average of {runs} runs)"

    print()
    print("=" * total_w)
    print(f"{heading:^{total_w}}")
    print("=" * total_w)
    print(_hline(widths))
    print(_data_row(_COL_LABELS, widths))
    print(_hline(widths, left="+=", mid="=+=", right="=+", fill="="))

    for r in results:
        print(_data_row(_row_cells(r), widths))
        print(_hline(widths))

    print()

    # Percentage breakdown for the largest run
    if results:
        last = results[-1]
        if last.total_time > 0:
            print("Phase breakdown (largest workload):")
            for label, val in [
                ("query_logs", last.query_time),
                ("create_spans", last.spans_time),
                ("flush_traces", last.flush_time),
            ]:
                pct = val / last.total_time * 100
                bar = "#" * int(pct / 2)
                print(f"  {label:<16} {val:>8.4f}s  {pct:>5.1f}%  {bar}")
        print()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "-c",
        "--config",
        default=DEFAULT_CONFIG,
        metavar="PATH",
        help=f"Path to mailtrace config.yaml (default: {DEFAULT_CONFIG})",
    )
    p.add_argument(
        "--endpoint",
        default=DEFAULT_ENDPOINT,
        metavar="URL",
        help=f"OTLP gRPC endpoint (default: {DEFAULT_ENDPOINT})",
    )
    p.add_argument(
        "--script",
        default=DEFAULT_SCRIPT,
        metavar="PATH",
        help=f"Path to send_bulk_emails.sh (default: {DEFAULT_SCRIPT})",
    )
    p.add_argument(
        "--wait",
        type=int,
        default=DEFAULT_WAIT,
        metavar="SEC",
        help=(
            f"Seconds to wait after sending for OpenSearch to finish "
            f"indexing the logs (default: {DEFAULT_WAIT})"
        ),
    )
    p.add_argument(
        "--sizes",
        nargs="+",
        type=int,
        default=DEFAULT_SIZES,
        metavar="N",
        help=f"Email counts to benchmark (default: {DEFAULT_SIZES})",
    )
    p.add_argument(
        "--runs",
        type=int,
        default=DEFAULT_RUNS,
        metavar="N",
        help=(
            f"Number of measurement repetitions per size; "
            f"the median is reported (default: {DEFAULT_RUNS})"
        ),
    )
    p.add_argument(
        "--max-retries",
        type=int,
        default=DEFAULT_MAX_RETRIES,
        metavar="N",
        help=(
            "Maximum number of times to re-send emails and re-run the "
            "tracing cycle when the observed expected trace count does not match "
            f"the expected count (default: {DEFAULT_MAX_RETRIES})"
        ),
    )
    p.add_argument(
        "--skip-send",
        action="store_true",
        help=(
            "Skip email sending and benchmark against data already in "
            "OpenSearch (useful for iterating without re-sending)."
        ),
    )
    p.add_argument(
        "--skip-window",
        type=int,
        default=DEFAULT_SKIP_WINDOW,
        metavar="MIN",
        help=(
            "Look-back window in minutes when --skip-send is set "
            f"(default: {DEFAULT_SKIP_WINDOW})"
        ),
    )
    p.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable DEBUG logging from mailtrace (noisy).",
    )
    return p


def main() -> int:  # noqa: C901
    args = build_parser().parse_args()

    if args.verbose:
        logging.basicConfig(
            level=logging.DEBUG,
            format="%(asctime)s %(name)s %(levelname)s %(message)s",
        )
        logging.getLogger("mailtrace").setLevel(logging.DEBUG)
    else:
        logging.basicConfig(level=logging.WARNING)

    # ------------------------------------------------------------------
    # Resolve paths relative to the project root so the script works
    # regardless of where it is invoked from.
    # ------------------------------------------------------------------
    config_path = Path(args.config)
    if not config_path.is_absolute():
        config_path = _PROJECT_ROOT / config_path
    config_path = config_path.resolve()

    script_path = Path(args.script)
    if not script_path.is_absolute():
        script_path = _PROJECT_ROOT / script_path
    script_path = script_path.resolve()

    if not config_path.exists():
        print(f"ERROR: config not found: {config_path}", file=sys.stderr)
        return 1
    if not args.skip_send and not script_path.exists():
        print(f"ERROR: script not found: {script_path}", file=sys.stderr)
        return 1

    # ------------------------------------------------------------------
    # Bootstrap
    # ------------------------------------------------------------------
    print(f"Config:        {config_path}")
    print(f"OTLP endpoint: {args.endpoint}")
    if not args.skip_send:
        print(f"Send script:   {script_path}")
        print(f"Index wait:    {args.wait}s")
    print(f"Sizes:         {args.sizes}")
    print(f"Runs/size:     {args.runs}")

    print("\nLoading config ...", flush=True)
    try:
        config = load_config(str(config_path))
    except Exception as exc:
        print(f"ERROR loading config: {exc}", file=sys.stderr)
        return 1

    print(f"Initialising OTLP exporter -> {args.endpoint} ...", flush=True)
    init_exporter(args.endpoint)

    # ------------------------------------------------------------------
    # Main benchmark loop
    # ------------------------------------------------------------------
    final_results: list[RunResult] = []

    for n in args.sizes:
        divider = "=" * 60
        print(f"\n{divider}")
        print(f"  Benchmark: {n:>5} email(s)  |  {args.runs} run(s)")
        print(divider)

        run_results: list[RunResult] = []

        for run_idx in range(1, args.runs + 1):
            if args.runs > 1:
                print(f"\n  -- Run {run_idx}/{args.runs} --")

            # ---- retry loop: re-send + re-trace until complete logs are present ----
            result: RunResult | None = None

            for attempt in range(1, args.max_retries + 1):
                if attempt > 1:
                    print(f"  -- Retry {attempt}/{args.max_retries} --")

                # ---- determine time window ----------------------------
                if args.skip_send:
                    end_dt = datetime.now(UTC)
                    start_dt = end_dt - timedelta(minutes=args.skip_window)
                    print(
                        f"  [skip-send] window: "
                        f"{start_dt.strftime('%H:%M:%S')} -> "
                        f"{end_dt.strftime('%H:%M:%S')} UTC",
                        flush=True,
                    )
                else:
                    start_dt = datetime.now(UTC)
                    ok = send_emails(script_path, n)
                    if not ok:
                        print(
                            f"  WARNING: Send errors on attempt {attempt}; retrying."
                        )
                        continue
                    send_end_dt = datetime.now(UTC)

                    print(
                        f"  Waiting {args.wait}s for OpenSearch indexing ...",
                        flush=True,
                    )
                    time.sleep(args.wait)

                    # Add a generous buffer so all log entries land in the window.
                    end_dt = send_end_dt + timedelta(seconds=args.wait + 5)

                # ---- run the tracing cycle ---------------------------
                print("  Running tracing cycle ...", flush=True)
                candidate = _run_tracing_cycle(config, start_dt, end_dt)

                if candidate is None:
                    print(
                        f"  WARNING: No log entries returned on attempt {attempt}. "
                        "Check that the demo stack is running and logs are being "
                        "shipped to OpenSearch."
                        + (
                            " Skipping (--skip-send prevents re-sending)."
                            if args.skip_send
                            else " Retrying."
                        )
                    )
                    if args.skip_send:
                        break
                    continue

                if not args.skip_send and candidate.n_expected != n:
                    print(
                        f"  WARNING: Expected {n} trace(s) but got "
                        f"{candidate.n_expected} expected trace(s) from full "
                        f"logs on attempt {attempt}. Retrying."
                    )
                    continue

                # Full logs are present; preserve generated trace errors in metrics.
                result = candidate._replace(n_emails=n)
                break

            else:
                # Exhausted all retry attempts without a complete full-log set.
                print(
                    f"  WARNING: Could not obtain exactly {n} expected trace(s) after "
                    f"{args.max_retries} attempt(s). Skipping run {run_idx}."
                )

            if result is None:
                continue

            run_results.append(result)

            print(
                f"  expected={result.n_expected:>5}  "
                f"  traces={result.n_traces:>5}  "
                f"exact={result.exact_matches:>5}  "
                f"malformed={result.malformed_traces:>5}  "
                f"missing={result.missing_events:>5}  "
                f"exact_rate={result.exact_match_rate * 100:>6.2f}%  "
                f"query={result.query_time:.4f}s  "
                f"spans={result.spans_time:.4f}s  "
                f"flush={result.flush_time:.4f}s  "
                f"total={result.total_time:.4f}s  "
                f"avg={result.avg_per_trace:.6f}s/trace"
            )

        # ---- aggregate across runs ------------------------------------
        if not run_results:
            print(f"  No valid results for n={n}; skipping.")
            continue

        agg = (
            _average_result(n, run_results)
            if len(run_results) > 1
            else run_results[0]
        )
        final_results.append(agg)

    # ------------------------------------------------------------------
    # Summary table
    # ------------------------------------------------------------------
    if final_results:
        print_results_table(final_results, args.runs)
    else:
        print("\nNo results collected. Nothing to display.", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
