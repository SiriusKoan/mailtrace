#!/usr/bin/env python3
"""Measure Docker container CPU and memory at specified email rates."""

import argparse
import csv
import json
import math
import re
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from typing import Optional

DEFAULT_RATES = (10, 20, 50, 100, 200, 500)
DEFAULT_DURATION_SECONDS = 600.0
SCRIPT_DIR = Path(__file__).resolve().parent
RESOURCE_SCRIPT = SCRIPT_DIR / "bench_resources.py"
SENDER_SCRIPT = SCRIPT_DIR / "send_bulk_emails.py"
MONITOR_START_TIMEOUT_SECONDS = 60.0
MONITOR_STOP_TIMEOUT_SECONDS = 60.0
DOCKER_COMMAND_TIMEOUT_SECONDS = 300.0
DEFAULT_TRACE_POLL_INTERVAL_SECONDS = 120.0
TRACE_COUNT_PATTERN = re.compile(r"Traces generated\s+(\d+)\s*$", re.MULTILINE)


def write_cpu_usage_svg(csv_path: Path, output_path: Path, rate: int) -> None:
    """Write a CPU usage versus timestamp line chart as an SVG file."""
    with csv_path.open(newline="", encoding="utf-8") as csv_file:
        reader = csv.DictReader(csv_file)
        if not {"timestamp", "cpu_percent"}.issubset(reader.fieldnames or []):
            raise ValueError("resource CSV is missing CPU plot columns")
        samples = [
            (int(row["timestamp"]), float(row["cpu_percent"]))
            for row in reader
        ]
    if not samples:
        raise ValueError("resource CSV contains no samples")

    width, height = 1200, 675
    left, right, top, bottom = 85, 35, 55, 80
    plot_width = width - left - right
    plot_height = height - top - bottom
    start_time, end_time = samples[0][0], samples[-1][0]
    time_span = max(end_time - start_time, 1)
    y_limit = max(
        10.0,
        math.ceil(max(value for _, value in samples) / 10) * 10,
    )

    def x_position(timestamp: int) -> float:
        return left + (timestamp - start_time) / time_span * plot_width

    def y_position(cpu_percent: float) -> float:
        return top + (1 - cpu_percent / y_limit) * plot_height

    polyline = " ".join(
        f"{x_position(timestamp):.2f},{y_position(cpu_percent):.2f}"
        for timestamp, cpu_percent in samples
    )
    title = escape(f"CPU Usage at {rate} emails/s")
    elements = [
        '<svg xmlns="http://www.w3.org/2000/svg" '
        f'width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        '<rect width="100%" height="100%" fill="#ffffff"/>',
        f'<text x="{width / 2}" y="30" text-anchor="middle" '
        f'font-family="sans-serif" font-size="22">{title}</text>',
    ]
    for index in range(6):
        fraction = index / 5
        y = top + (1 - fraction) * plot_height
        value = fraction * y_limit
        elements.extend(
            [
                f'<line x1="{left}" y1="{y:.2f}" '
                f'x2="{width - right}" y2="{y:.2f}" '
                'stroke="#d9d9d9"/>',
                f'<text x="{left - 12}" y="{y + 5:.2f}" '
                'text-anchor="end" font-family="sans-serif" '
                f'font-size="14">{value:.0f}</text>',
            ]
        )
    for index in range(6):
        fraction = index / 5
        timestamp = round(start_time + fraction * time_span)
        x = left + fraction * plot_width
        label = datetime.fromtimestamp(timestamp, timezone.utc).strftime(
            "%H:%M:%S"
        )
        elements.extend(
            [
                f'<line x1="{x:.2f}" y1="{top}" x2="{x:.2f}" '
                f'y2="{height - bottom}" stroke="#eeeeee"/>',
                f'<text x="{x:.2f}" y="{height - bottom + 25}" '
                'text-anchor="middle" font-family="sans-serif" '
                f'font-size="14">{label}</text>',
            ]
        )
    elements.extend(
        [
            f'<line x1="{left}" y1="{top}" x2="{left}" '
            f'y2="{height - bottom}" stroke="#333333" stroke-width="2"/>',
            f'<line x1="{left}" y1="{height - bottom}" '
            f'x2="{width - right}" y2="{height - bottom}" '
            'stroke="#333333" stroke-width="2"/>',
            f'<polyline points="{polyline}" fill="none" '
            'stroke="#1f77b4" stroke-width="2" '
            'stroke-linejoin="round"/>',
            f'<text x="{width / 2}" y="{height - 20}" '
            'text-anchor="middle" font-family="sans-serif" '
            'font-size="16">Timestamp (UTC)</text>',
            f'<text x="22" y="{height / 2}" text-anchor="middle" '
            'font-family="sans-serif" font-size="16" '
            f'transform="rotate(-90 22 {height / 2})">CPU usage (%)</text>',
            "</svg>",
        ]
    )
    output_path.write_text("\n".join(elements) + "\n", encoding="utf-8")


def wait_for_monitor(
    process: subprocess.Popen[str],
    csv_path: Path,
    timeout: float = MONITOR_START_TIMEOUT_SECONDS,
) -> None:
    """Wait until the resource monitor creates the CSV header."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(
                f"resource monitor exited with status {process.returncode}"
            )
        if csv_path.exists() and csv_path.stat().st_size > 0:
            return
        time.sleep(0.05)
    raise RuntimeError("resource monitor did not start in time")


def stop_monitor(process: subprocess.Popen[str]) -> int:
    """Stop the monitor with SIGINT so its summary includes the final interval."""
    if process.poll() is None:
        process.send_signal(signal.SIGINT)
    try:
        return process.wait(timeout=MONITOR_STOP_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        process.terminate()
        return process.wait(timeout=MONITOR_STOP_TIMEOUT_SECONDS)


def count_generated_traces(log_output: str) -> int:
    """Sum trace counts reported by mailtrace benchmark intervals."""
    return sum(
        int(match.group(1))
        for match in TRACE_COUNT_PATTERN.finditer(log_output)
    )


class TraceLogFollower:
    """Stream container logs and count generated traces without rereading history."""

    def __init__(self, container: str, since: str, output_path: Path) -> None:
        self._trace_count = 0
        self._lock = threading.Lock()
        self._output = output_path.open("w", encoding="utf-8")
        self._process = subprocess.Popen(
            ["docker", "logs", "--follow", "--since", since, container],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        self._thread = threading.Thread(target=self._consume, daemon=True)
        self._thread.start()

    def _consume(self) -> None:
        assert self._process.stdout is not None
        for line in self._process.stdout:
            self._output.write(line)
            self._output.flush()
            increment = count_generated_traces(line)
            if increment:
                with self._lock:
                    self._trace_count += increment

    def trace_count(self) -> int:
        """Return the trace total observed by the log stream."""
        status = self._process.poll()
        if status not in (None, 0):
            raise RuntimeError(
                f"docker log follower exited with status {status}"
            )
        with self._lock:
            return self._trace_count

    def close(self) -> None:
        """Stop the Docker log stream and close its output file."""
        if self._process.poll() is None:
            self._process.terminate()
        try:
            self._process.wait(timeout=DOCKER_COMMAND_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            self._process.kill()
            self._process.wait(timeout=DOCKER_COMMAND_TIMEOUT_SECONDS)
        self._thread.join(timeout=DOCKER_COMMAND_TIMEOUT_SECONDS)
        self._output.close()


def postfix_queue_is_empty(container: str) -> bool:
    """Return whether a Postfix container has no queued message files."""
    result = subprocess.run(
        [
            "docker",
            "exec",
            container,
            "sh",
            "-c",
            "find /var/spool/postfix/active "
            "/var/spool/postfix/deferred "
            "/var/spool/postfix/hold "
            "/var/spool/postfix/incoming "
            "/var/spool/postfix/maildrop "
            "-type f -print -quit",
        ],
        capture_output=True,
        text=True,
        timeout=DOCKER_COMMAND_TIMEOUT_SECONDS,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"could not read the queue from {container}: {result.stderr.strip()}"
        )
    return not result.stdout.strip()


def nonempty_postfix_queues(containers: list[str]) -> list[str]:
    """Return the Postfix containers that still contain queued messages."""
    return [
        container
        for container in containers
        if not postfix_queue_is_empty(container)
    ]


def wait_for_trace_completion(
    trace_follower: TraceLogFollower,
    queue_containers: list[str],
    expected_trace_count: int,
    poll_interval: float,
) -> int:
    """Wait for exact trace parity and empty Postfix queues."""
    while True:
        trace_count = trace_follower.trace_count()
        print(
            f"Trace completion: {trace_count}/{expected_trace_count}",
            file=sys.stderr,
            flush=True,
        )
        if trace_count > expected_trace_count:
            raise RuntimeError(
                f"trace count exceeded submissions: "
                f"{trace_count} > {expected_trace_count}"
            )
        if trace_count == expected_trace_count:
            try:
                pending_queues = nonempty_postfix_queues(queue_containers)
            except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
                print(
                    f"Postfix queue check failed; retrying: {exc}",
                    file=sys.stderr,
                    flush=True,
                )
            else:
                print(
                    "Pending Postfix queues: "
                    + (", ".join(pending_queues) or "none"),
                    file=sys.stderr,
                    flush=True,
                )
                if not pending_queues:
                    return trace_count
        time.sleep(poll_interval)


def run_rate(
    container: str,
    output_dir: Path,
    rate: int,
    duration: float,
    *,
    trace_container: Optional[str] = None,
    queue_containers: Optional[list[str]] = None,
    trace_poll_interval: float = DEFAULT_TRACE_POLL_INTERVAL_SECONDS,
    sender_mx_port: int = 10025,
    sender_mailpolicy_port: int = 20025,
) -> tuple[dict[str, object], bool]:
    """Send email and measure resources at one requested rate."""
    stem = f"resources_{rate}_emails_per_sec"
    csv_path = output_dir / f"{stem}.csv"
    summary_path = output_dir / f"{stem}.json"
    monitor_error_path = output_dir / f"{stem}.stderr.log"
    sender_log_path = output_dir / f"sender_{rate}_emails_per_sec.log"
    trace_log_path = output_dir / f"trace_{rate}_emails_per_sec.log"
    cpu_plot_path = output_dir / f"cpu_usage_{rate}_emails_per_sec.svg"

    print(
        f"Measuring {rate} emails/s for {duration:g} seconds",
        file=sys.stderr,
        flush=True,
    )

    monitor: Optional[subprocess.Popen[str]] = None
    trace_follower: Optional[TraceLogFollower] = None
    sender_status = 1
    monitor_status = 1
    run_error: Optional[str] = None
    submitted_email_count = int(rate * duration)
    trace_count: Optional[int] = None
    case_started_at: Optional[str] = None
    postfix_containers = queue_containers or []

    with summary_path.open(
        "w", encoding="utf-8"
    ) as summary_file, monitor_error_path.open(
        "w", encoding="utf-8"
    ) as monitor_error_file, sender_log_path.open(
        "w", encoding="utf-8"
    ) as sender_log_file:
        try:
            monitor = subprocess.Popen(
                [
                    sys.executable,
                    str(RESOURCE_SCRIPT),
                    "--container",
                    container,
                    "--output",
                    str(csv_path),
                ],
                stdout=summary_file,
                stderr=monitor_error_file,
                text=True,
            )
            wait_for_monitor(monitor, csv_path)
            case_started_at = datetime.now(timezone.utc).isoformat()
            if trace_container is not None:
                trace_follower = TraceLogFollower(
                    trace_container, case_started_at, trace_log_path
                )
            sender_status = subprocess.run(
                [
                    sys.executable,
                    str(SENDER_SCRIPT),
                    str(rate),
                    str(duration),
                    "--mx-port",
                    str(sender_mx_port),
                    "--mailpolicy-port",
                    str(sender_mailpolicy_port),
                ],
                stdout=sender_log_file,
                stderr=subprocess.STDOUT,
                text=True,
                check=False,
            ).returncode
            if sender_status == 0 and trace_follower is not None:
                trace_count = wait_for_trace_completion(
                    trace_follower,
                    postfix_containers,
                    submitted_email_count,
                    trace_poll_interval,
                )
        except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
            run_error = str(exc)
        finally:
            if trace_follower is not None:
                trace_follower.close()
            if monitor is not None:
                monitor_status = stop_monitor(monitor)

    resource_summary: Optional[dict[str, object]] = None
    try:
        resource_summary = json.loads(summary_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        run_error = run_error or f"invalid resource summary: {exc}"
    try:
        write_cpu_usage_svg(csv_path, cpu_plot_path, rate)
    except (OSError, ValueError) as exc:
        run_error = run_error or f"could not create CPU plot: {exc}"

    success = (
        run_error is None
        and sender_status == 0
        and monitor_status == 0
        and resource_summary is not None
        and resource_summary.get("stop_reason") == "signal"
        and (trace_container is None or trace_count == submitted_email_count)
    )
    result = {
        "emails_per_second": rate,
        "duration_seconds": duration,
        "sender_status": sender_status,
        "resource_status": monitor_status,
        "resource_csv": str(csv_path),
        "resource_summary": str(summary_path),
        "cpu_plot": str(cpu_plot_path),
        "sender_log": str(sender_log_path),
        "trace_log": str(trace_log_path) if trace_container else None,
        "started_at": case_started_at,
        "submitted_email_count": submitted_email_count,
        "trace_count": trace_count,
        "trace_count_matches": (
            trace_count == submitted_email_count
            if trace_container is not None
            else None
        ),
        "error": run_error,
    }
    return result, success


def run(
    container: str,
    output_dir: Path,
    rates: list[int],
    duration: float,
    *,
    trace_container: Optional[str] = None,
    queue_containers: Optional[list[str]] = None,
    trace_poll_interval: float = DEFAULT_TRACE_POLL_INTERVAL_SECONDS,
    sender_mx_port: int = 10025,
    sender_mailpolicy_port: int = 20025,
) -> int:
    output_dir.mkdir(parents=True, exist_ok=True)
    results = []
    success = True
    for rate in rates:
        result, rate_success = run_rate(
            container,
            output_dir,
            rate,
            duration,
            trace_container=trace_container,
            queue_containers=queue_containers,
            trace_poll_interval=trace_poll_interval,
            sender_mx_port=sender_mx_port,
            sender_mailpolicy_port=sender_mailpolicy_port,
        )
        results.append(result)
        success = success and rate_success

    json.dump(
        {
            "container": container,
            "rates": rates,
            "duration_seconds": duration,
            "trace_container": trace_container,
            "runs": results,
        },
        sys.stdout,
        separators=(",", ":"),
    )
    sys.stdout.write("\n")
    return 0 if success else 1


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--container", required=True)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument(
        "--rates",
        nargs="+",
        type=int,
        default=list(DEFAULT_RATES),
        metavar="EMAILS_PER_SECOND",
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=DEFAULT_DURATION_SECONDS,
        metavar="SECONDS",
    )
    parser.add_argument("--trace-container")
    parser.add_argument(
        "--queue-container",
        action="append",
        default=[],
        help="Postfix container that must be empty before completion",
    )
    parser.add_argument(
        "--trace-poll-interval",
        type=float,
        default=DEFAULT_TRACE_POLL_INTERVAL_SECONDS,
        metavar="SECONDS",
    )
    parser.add_argument("--sender-mx-port", type=int, default=10025)
    parser.add_argument("--sender-mailpolicy-port", type=int, default=20025)
    args = parser.parse_args(argv)
    if any(rate <= 0 for rate in args.rates):
        parser.error("--rates values must be positive")
    if args.duration <= 0:
        parser.error("--duration must be positive")
    if args.trace_poll_interval <= 0:
        parser.error("--trace-poll-interval must be positive")
    if args.queue_container and not args.trace_container:
        parser.error("--queue-container requires --trace-container")
    return args


def main() -> int:
    args = parse_args()
    return run(
        args.container,
        args.output_dir,
        args.rates,
        args.duration,
        trace_container=args.trace_container,
        queue_containers=args.queue_container,
        trace_poll_interval=args.trace_poll_interval,
        sender_mx_port=args.sender_mx_port,
        sender_mailpolicy_port=args.sender_mailpolicy_port,
    )


if __name__ == "__main__":
    raise SystemExit(main())
