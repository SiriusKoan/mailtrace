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
import urllib.error
import urllib.request
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from typing import Optional

DEFAULT_RATES = (10, 20, 50, 100, 200, 500)
DEFAULT_DURATION_SECONDS = 600.0
SCRIPT_DIR = Path(__file__).resolve().parent
REPOSITORY_ROOT = SCRIPT_DIR.parent.parent
RESOURCE_SCRIPT = SCRIPT_DIR / "bench_resources.py"
SENDER_SCRIPT = SCRIPT_DIR / "send_bulk_emails.py"
COMPOSE_FILE = SCRIPT_DIR / "docker-compose.resource-benchmark.yml"
COMPOSE_PROJECT_NAME = "mailtrace-resource-benchmark"
COMPOSE_START_TIMEOUT_SECONDS = 300
SERVICE_READY_TIMEOUT_SECONDS = 300.0
TRACE_COMPLETION_TIMEOUT_SECONDS = 3600.0
BENCHMARK_MX_PORT = 11025
BENCHMARK_MAILPOLICY_PORT = 21025
MONITOR_START_TIMEOUT_SECONDS = 60.0
MONITOR_STOP_TIMEOUT_SECONDS = 60.0
DOCKER_COMMAND_TIMEOUT_SECONDS = 300.0
DEFAULT_TRACE_POLL_INTERVAL_SECONDS = 120.0
TRACE_COUNT_PATTERN = re.compile(r"Traces generated\s+(\d+)\s*$", re.MULTILINE)


def default_output_dir() -> Path:
    """回傳本次實驗使用的預設結果目錄。"""
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return REPOSITORY_ROOT / "results" / f"resource-{timestamp}"


def compose_command(*args: str) -> list[str]:
    """建立專用實驗環境的 Docker Compose 命令。"""
    return [
        "docker",
        "compose",
        "--project-name",
        COMPOSE_PROJECT_NAME,
        "--file",
        str(COMPOSE_FILE),
        *args,
    ]


def cleanup_environment() -> None:
    """移除實驗容器、網路、磁碟區及孤立容器。"""
    result = subprocess.run(
        compose_command("down", "--volumes", "--remove-orphans"),
        stdout=sys.stderr,
        stderr=sys.stderr,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"docker compose cleanup failed with status {result.returncode}"
        )


def wait_for_http_service(
    url: str, timeout: float = SERVICE_READY_TIMEOUT_SECONDS
) -> None:
    """等待 HTTP 服務回傳成功狀態。"""
    deadline = time.monotonic() + timeout
    last_error = "service did not respond"
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2.0) as response:
                if 200 <= response.status < 300:
                    return
                last_error = f"HTTP status {response.status}"
        except (OSError, urllib.error.URLError) as exc:
            last_error = str(exc)
        time.sleep(0.5)
    raise RuntimeError(f"service did not become ready at {url}: {last_error}")


def start_environment() -> None:
    """建置並啟動專用實驗環境，等待所有服務就緒。"""
    result = subprocess.run(
        compose_command(
            "up",
            "--build",
            "--wait",
            "--wait-timeout",
            str(COMPOSE_START_TIMEOUT_SECONDS),
        ),
        stdout=sys.stderr,
        stderr=sys.stderr,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"docker compose up failed with status {result.returncode}"
        )
    wait_for_http_service("http://127.0.0.1:13200/ready")


def compose_container_id(service: str) -> str:
    """取得專用實驗環境中指定服務的容器 ID。"""
    result = subprocess.run(
        compose_command("ps", "--quiet", service),
        capture_output=True,
        text=True,
        check=False,
    )
    container_ids = result.stdout.split()
    if result.returncode != 0 or len(container_ids) != 1:
        detail = result.stderr.strip() or "container is not running"
        raise RuntimeError(f"could not resolve {service} container: {detail}")
    return container_ids[0]


def capture_compose_logs(output_dir: Path) -> None:
    """在清理失敗環境前保留末尾容器日誌。"""
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "compose.stderr.log"
    try:
        with path.open("w", encoding="utf-8") as output:
            subprocess.run(
                compose_command("logs", "--no-color", "--tail", "200"),
                stdout=output,
                stderr=subprocess.STDOUT,
                text=True,
                check=False,
            )
    except OSError as exc:
        print(f"could not capture compose logs: {exc}", file=sys.stderr)


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


def exim_queue_is_empty(container: str) -> bool:
    """判斷 Exim 容器是否沒有待處理郵件。"""
    result = subprocess.run(
        ["docker", "exec", container, "exim4", "-bpc"],
        capture_output=True,
        text=True,
        timeout=DOCKER_COMMAND_TIMEOUT_SECONDS,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"could not read the queue from {container}: {result.stderr.strip()}"
        )
    try:
        return int(result.stdout.strip()) == 0
    except ValueError as exc:
        raise RuntimeError(
            f"invalid Exim queue count from {container}: {result.stdout.strip()}"
        ) from exc


def wait_for_trace_completion(
    trace_follower: TraceLogFollower,
    queue_containers: list[str],
    expected_trace_count: int,
    poll_interval: float,
    *,
    exim_queue_container: Optional[str] = None,
    timeout: float = TRACE_COMPLETION_TIMEOUT_SECONDS,
) -> int:
    """等待 trace 數量達標，並回報 Postfix 與 Exim 佇列狀態。"""
    deadline = time.monotonic() + timeout
    while True:
        trace_count = trace_follower.trace_count()
        print(
            f"Trace completion: {trace_count}/{expected_trace_count}",
            file=sys.stderr,
            flush=True,
        )
        if trace_count >= expected_trace_count:
            try:
                pending_queues = nonempty_postfix_queues(queue_containers)
                if (
                    exim_queue_container is not None
                    and not exim_queue_is_empty(exim_queue_container)
                ):
                    pending_queues.append(exim_queue_container)
            except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
                print(
                    f"WARNING: mail queue check failed: {exc}",
                    file=sys.stderr,
                    flush=True,
                )
            else:
                if pending_queues:
                    print(
                        "WARNING: pending mail queues: "
                        + ", ".join(pending_queues),
                        file=sys.stderr,
                        flush=True,
                    )
                else:
                    print(
                        "Pending mail queues: none",
                        file=sys.stderr,
                        flush=True,
                    )
            return trace_count
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError(
                "trace completion timed out: "
                f"observed {trace_count}/{expected_trace_count} traces"
            )
        time.sleep(min(poll_interval, remaining))


def run_rate(
    container: str,
    output_dir: Path,
    rate: int,
    duration: float,
    *,
    trace_container: Optional[str] = None,
    queue_containers: Optional[list[str]] = None,
    exim_queue_container: Optional[str] = None,
    trace_poll_interval: float = DEFAULT_TRACE_POLL_INTERVAL_SECONDS,
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
                    str(BENCHMARK_MX_PORT),
                    "--mailpolicy-port",
                    str(BENCHMARK_MAILPOLICY_PORT),
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
                    exim_queue_container=exim_queue_container,
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

    trace_requirement_met = trace_container is None or (
        trace_count is not None and trace_count >= submitted_email_count
    )
    success = (
        run_error is None
        and sender_status == 0
        and monitor_status == 0
        and resource_summary is not None
        and resource_summary.get("stop_reason") == "signal"
        and trace_requirement_met
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
            trace_requirement_met if trace_container is not None else None
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
    exim_queue_container: Optional[str] = None,
    trace_poll_interval: float = DEFAULT_TRACE_POLL_INTERVAL_SECONDS,
) -> tuple[dict[str, object], int]:
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
            exim_queue_container=exim_queue_container,
            trace_poll_interval=trace_poll_interval,
        )
        results.append(result)
        success = success and rate_success

    report = {
        "container": container,
        "rates": rates,
        "duration_seconds": duration,
        "trace_container": trace_container,
        "runs": results,
    }
    return report, 0 if success else 1


def run_managed_experiment(
    output_dir: Path,
    rates: list[int],
    duration: float,
    trace_poll_interval: float,
) -> tuple[dict[str, object], int]:
    """管理 Docker 環境並執行完整資源實驗。"""
    output_dir.mkdir(parents=True, exist_ok=True)
    report: dict[str, object] = {
        "container": None,
        "rates": rates,
        "duration_seconds": duration,
        "trace_container": None,
        "runs": [],
        "output_dir": str(output_dir),
        "error": None,
        "cleanup_error": None,
    }
    status = 1
    try:
        cleanup_environment()
        start_environment()
        containers = {
            service: compose_container_id(service)
            for service in (
                "mailtrace",
                "mx",
                "mailpolicy",
                "mailbox",
                "mailbox2",
                "mailer",
            )
        }
        report, status = run(
            containers["mailtrace"],
            output_dir,
            rates,
            duration,
            trace_container=containers["mailtrace"],
            queue_containers=[
                containers["mx"],
                containers["mailpolicy"],
                containers["mailbox"],
                containers["mailbox2"],
            ],
            exim_queue_container=containers["mailer"],
            trace_poll_interval=trace_poll_interval,
        )
        report.update(
            {
                "output_dir": str(output_dir),
                "error": (
                    None
                    if status == 0
                    else "one or more rate experiments failed"
                ),
                "cleanup_error": None,
            }
        )
    except KeyboardInterrupt:
        report["error"] = "experiment interrupted"
        status = 130
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        report["error"] = str(exc)
        status = 1
    finally:
        if status != 0:
            capture_compose_logs(output_dir)
        try:
            cleanup_environment()
        except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
            report["cleanup_error"] = str(exc)
            capture_compose_logs(output_dir)
            if status == 0:
                status = 1
    return report, status


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir", type=Path, default=default_output_dir()
    )
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
    parser.add_argument(
        "--trace-poll-interval",
        type=float,
        default=DEFAULT_TRACE_POLL_INTERVAL_SECONDS,
        metavar="SECONDS",
    )
    args = parser.parse_args(argv)
    if any(rate <= 0 for rate in args.rates):
        parser.error("--rates values must be positive")
    if args.duration <= 0:
        parser.error("--duration must be positive")
    if args.trace_poll_interval <= 0:
        parser.error("--trace-poll-interval must be positive")
    return args


def raise_keyboard_interrupt(_signum: int, _frame: object) -> None:
    """將終止訊號轉為可執行 finally 清理流程的中斷。"""
    raise KeyboardInterrupt


def main() -> int:
    args = parse_args()
    previous_sigterm_handler = signal.signal(
        signal.SIGTERM, raise_keyboard_interrupt
    )
    try:
        report, status = run_managed_experiment(
            args.output_dir,
            args.rates,
            args.duration,
            args.trace_poll_interval,
        )
    finally:
        signal.signal(signal.SIGTERM, previous_sigterm_handler)
    json.dump(report, sys.stdout, separators=(",", ":"))
    sys.stdout.write("\n")
    return status


if __name__ == "__main__":
    raise SystemExit(main())
