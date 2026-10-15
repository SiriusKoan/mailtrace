#!/usr/bin/env python3
"""
Correctness benchmark for the production tracing daemon.

Each parameter set starts the production CLI daemon and sends test emails.
Complete OpenSearch logs are passed to export_traces in one call to build
baseline traces, kept in memory for comparison with daemon traces from Tempo.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import logging
import os
import re
import shutil
import signal
import statistics
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from contextlib import ExitStack, nullcontext
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, NamedTuple
from uuid import uuid4

import yaml
from google.protobuf.json_format import MessageToDict
from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_PROJECT_ROOT))

from demo.docker.daemon_benchmark_progress import (  # noqa: E402
    Progress,
    compatibility_manifest,
    digest,
    write_json,
)
from demo.docker.daemon_benchmark_stack import DemoStack  # noqa: E402

from mailtrace.config import load_config  # noqa: E402
from mailtrace.tracing import otel  # noqa: E402
from mailtrace.tracing.builder import export_traces  # noqa: E402
from mailtrace.tracing.query import (  # noqa: E402
    group_logs_by_message_id,
    query_all_logs,
)

DEFAULT_CONFIG = "demo/docker/config.yaml"
DEFAULT_SCRIPT = "demo/docker/send_bulk_emails.sh"
DEFAULT_TEMPO_URL = "http://localhost:3200"
DEFAULT_OTEL_ENDPOINT = "http://localhost:14317"
DEFAULT_SIZE = 10
DEFAULT_WAIT = 15
DEFAULT_SLEEP_SECONDS = 10
DEFAULT_HOLD_VALUES = [0, 1, 2, 5, 10]
DEFAULT_GO_BACK_VALUES = [0, 3, 5, 10, 30, 60]

logging.getLogger("mailtrace").setLevel(logging.WARNING)


class HostSignature(NamedTuple):
    """Span structure for one expected or generated host."""

    stages: tuple[str, ...]


TraceSignature = dict[str, HostSignature]
TraceSignatures = dict[str, TraceSignature]


class ParamSet(NamedTuple):
    """Production daemon polling parameters."""

    sleep_seconds: int
    hold_rounds: int
    go_back_seconds: int


class CorrectnessResult(NamedTuple):
    """Comparison between expected logs and generated Tempo traces."""

    expected: int
    generated: int
    exact: int
    malformed: int
    missing: int

    @property
    def exact_rate(self) -> float:
        """Return exact-match rate."""
        return self.exact / self.expected if self.expected else 0.0


class RunResult(NamedTuple):
    """Result for one parameter set."""

    params: ParamSet
    expected: int
    generated: int
    exact: int
    malformed: int
    missing: int
    exact_rate: float
    duration: float


class AggregatedResult(NamedTuple):
    """Aggregated result for repeated runs of one parameter set."""

    params: ParamSet
    runs: int
    expected_mean: float
    expected_std: float
    generated_mean: float
    generated_std: float
    exact_mean: float
    exact_std: float
    malformed_mean: float
    malformed_std: float
    missing_mean: float
    missing_std: float
    exact_pct_mean: float
    exact_pct_std: float


def parse_param_set(raw: str) -> ParamSet:
    """Parse sleep,hold,go_back triples."""
    parts = raw.split(",")
    if len(parts) != 3:
        raise argparse.ArgumentTypeError(
            "parameter set must use sleep,hold,go_back"
        )
    try:
        sleep_seconds, hold_rounds, go_back_seconds = [
            int(part) for part in parts
        ]
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "parameter set values must be integers"
        ) from exc
    if sleep_seconds <= 0:
        raise argparse.ArgumentTypeError("sleep must be positive")
    if hold_rounds < 0:
        raise argparse.ArgumentTypeError("hold must be non-negative")
    if go_back_seconds < 0:
        raise argparse.ArgumentTypeError("go_back must be non-negative")
    return ParamSet(sleep_seconds, hold_rounds, go_back_seconds)


def build_param_grid(
    sleep_seconds: int,
    hold_values: list[int],
    go_back_values: list[int],
) -> list[ParamSet]:
    """Build deterministic go_back-major parameter grid."""
    return [
        ParamSet(
            sleep_seconds=sleep_seconds,
            hold_rounds=hold_rounds,
            go_back_seconds=go_back_seconds,
        )
        for go_back_seconds in go_back_values
        for hold_rounds in hold_values
    ]


def write_temp_config(source: Path, params: ParamSet, temp_dir: Path) -> Path:
    """Write a temporary production config with updated tracing parameters."""
    with source.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    tracing = data.setdefault("tracing", {})
    tracing["sleep_seconds"] = params.sleep_seconds
    tracing["hold_rounds"] = params.hold_rounds
    tracing["go_back_seconds"] = params.go_back_seconds

    output = temp_dir / (
        f"daemon_sleep{params.sleep_seconds}_hold{params.hold_rounds}_"
        f"goback{params.go_back_seconds}.yaml"
    )
    with output.open("w", encoding="utf-8") as fh:
        yaml.safe_dump(data, fh, sort_keys=False)
    return output


def start_production_daemon(
    config_path: Path,
    otel_endpoint: str,
    log_path: Path,
) -> subprocess.Popen:
    """Start the normal CLI tracing daemon."""
    cmd = [
        sys.executable,
        "-m",
        "mailtrace",
        "tracing",
        "--config-path",
        str(config_path),
        "--otel-endpoint",
        otel_endpoint,
    ]
    with log_path.open("w", encoding="utf-8") as log_file:
        return subprocess.Popen(
            cmd,
            cwd=_PROJECT_ROOT,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )


def stop_process(proc: subprocess.Popen, timeout: int = 10) -> None:
    """Terminate a process group."""
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGKILL):
        if proc.poll() is not None:
            return
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError:
            return
        try:
            proc.wait(timeout=timeout)
            return
        except subprocess.TimeoutExpired:
            if sig == signal.SIGKILL:
                raise


def process_identity(pid):
    """Check PID reuse before touching an orphaned benchmark daemon."""
    try:
        proc = Path(f"/proc/{pid}")
        fields = (proc / "stat").read_text().rsplit(")", 1)[1].split()
        if fields[0] == "Z":
            return None
        return {
            "pid": int(pid),
            "start_ticks": fields[19],
            "command": (proc / "cmdline")
            .read_bytes()
            .decode()
            .rstrip("\0")
            .split("\0"),
        }
    except (FileNotFoundError, ProcessLookupError):
        return None


def recover_daemons(progress):
    """Stop only identified children of uncommitted attempts before reset."""
    for unit in progress.data["units"]:
        if unit["state"] == "completed":
            continue
        for attempt in unit["attempts"]:
            path = (
                progress.output
                / attempt["artifact_dir"]
                / "daemon-process.json"
            ).resolve()
            if not path.is_relative_to(progress.output.resolve()):
                raise ValueError(
                    "Attempt artifacts escape the experiment output"
                )
            if not path.exists():
                continue
            identity = json.loads(path.read_text())
            pid = identity["pid"]
            if process_identity(pid) != identity:
                continue
            if (
                identity["command"][1:4] != ["-m", "mailtrace", "tracing"]
                or os.getpgid(pid) != pid
            ):
                raise RuntimeError(
                    "Recorded daemon ownership could not be verified"
                )
            for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGKILL):
                try:
                    os.killpg(pid, sig)
                except ProcessLookupError:
                    break
                deadline = time.monotonic() + 5
                while (
                    process_identity(pid) == identity
                    and time.monotonic() < deadline
                ):
                    time.sleep(0.1)
                if process_identity(pid) != identity:
                    break
            else:
                raise RuntimeError(
                    f"Orphaned benchmark daemon {pid} did not stop"
                )


def send_emails(script: Path, n: int, run_prefix: str, seed=None) -> None:
    """Run the demo email sender."""
    env = dict(os.environ, MAILTRACE_RUN_PREFIX=run_prefix)
    if seed is not None:
        env["MAILTRACE_SEED"] = str(seed)
    proc = subprocess.run(
        ["bash", str(script), str(n)],
        env=env,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "")[-800:].strip()
        raise RuntimeError(f"{script.name} exited {proc.returncode}\n{tail}")


def build_expected_signatures(logs_by_message_id) -> TraceSignatures:
    """Build a baseline through production export without writing to Tempo."""
    exporter = InMemorySpanExporter()
    previous_exporter = otel._exporter
    previous_providers = otel._providers
    otel._exporter = exporter
    otel._providers = {}
    try:
        export_traces(logs_by_message_id)
        otel.flush_traces()
        trace_obj = MessageToDict(encode_spans(exporter.get_finished_spans()))
        return build_generated_signatures([trace_obj], set(logs_by_message_id))
    finally:
        providers = otel._providers
        otel._exporter = previous_exporter
        otel._providers = previous_providers
        for provider in providers.values():
            provider.shutdown()


def attribute_value(attribute: dict[str, Any]) -> Any:
    """Return the scalar value from an OTLP JSON attribute."""
    value = attribute.get("value", {})
    for key in ("stringValue", "boolValue", "intValue", "doubleValue"):
        if key in value:
            return value[key]
    return None


def attribute_map(span: dict[str, Any]) -> dict[str, Any]:
    """Return OTLP span attributes as a dict."""
    return {
        attribute["key"]: attribute_value(attribute)
        for attribute in span.get("attributes", [])
    }


def trace_spans(trace_obj: dict[str, Any]) -> list[dict[str, Any]]:
    """Return all spans from an OTLP JSON trace."""
    spans = []
    resource_spans = trace_obj.get(
        "resourceSpans", trace_obj.get("batches", [])
    )
    for resource_span in resource_spans:
        scope_spans = resource_span.get(
            "scopeSpans", resource_span.get("instrumentationLibrarySpans", [])
        )
        for scope_span in scope_spans:
            spans.extend(scope_span.get("spans", []))
    return spans


def fetch_tempo_traces(
    tempo_url: str,
    start_dt: datetime,
    end_dt: datetime,
    limit: int,
) -> list[dict[str, Any]]:
    """Fetch OTLP JSON traces from the Tempo HTTP API."""
    params = urllib.parse.urlencode(
        {
            "tags": "service.name=mailtrace",
            "start": int(start_dt.timestamp()),
            "end": int(end_dt.timestamp()),
            "limit": limit,
        }
    )
    base_url = tempo_url.rstrip("/")
    with urllib.request.urlopen(
        f"{base_url}/api/search?{params}", timeout=30
    ) as response:
        payload = json.loads(response.read().decode("utf-8"))

    traces = []
    trace_params = urllib.parse.urlencode(
        {"start": int(start_dt.timestamp()), "end": int(end_dt.timestamp())}
    )
    for result in payload.get("traces", []):
        trace_id = result.get("traceID", result.get("traceId"))
        if not trace_id:
            continue
        url = f"{base_url}/api/traces/{trace_id}?{trace_params}"
        with urllib.request.urlopen(url, timeout=30) as response:
            traces.append(json.loads(response.read().decode("utf-8")))
    return traces


def build_generated_signatures(
    traces: list[dict[str, Any]],
    expected_message_ids: set[str],
) -> TraceSignatures:
    """Build generated trace structures from Tempo traces."""
    signatures: TraceSignatures = {}

    for trace_obj in traces:
        spans = trace_spans(trace_obj)
        host_spans: dict[str, dict[str, Any]] = {}
        child_spans: dict[str, list[dict[str, Any]]] = {}
        delays_by_host: dict[tuple[str, str], list[dict[str, Any]]] = {}

        for span in spans:
            attributes = attribute_map(span)
            span_id = span["spanId"]
            if "server.address" in attributes and "message.id" in attributes:
                host_spans[span_id] = span
            parent_span_id = span.get("parentSpanId")
            if parent_span_id:
                child_spans.setdefault(parent_span_id, []).append(span)

        for span_id, host_span in host_spans.items():
            attributes = attribute_map(host_span)
            message_id = str(attributes["message.id"])
            if message_id not in expected_message_ids:
                continue
            host = str(attributes["server.address"])
            delay_spans = delays_by_host.setdefault((message_id, host), [])
            for child in child_spans.get(span_id, []):
                # Only expand delivery wrappers to exclude downstream host stages.
                candidates = (
                    child_spans.get(child["spanId"], [])
                    if child["name"] == "delivery"
                    else [child]
                )
                for candidate in candidates:
                    if "delay.duration_seconds" in attribute_map(candidate):
                        delay_spans.append(candidate)

        for (message_id, host), delay_spans in delays_by_host.items():
            delay_spans.sort(
                key=lambda span: int(span.get("startTimeUnixNano", 0))
            )
            # The baseline records unique stage names per host, not delivery counts.
            signatures.setdefault(message_id, {})[host] = HostSignature(
                stages=tuple(
                    dict.fromkeys(span["name"] for span in delay_spans)
                )
            )

    return signatures


def compare_signatures(
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


def completed_rounds(log_path: Path, after: datetime) -> int:
    """Count successful daemon queries using the daemon's local log timezone."""
    count = 0
    for line in log_path.read_text().splitlines():
        if re.search(r"Found \d+ log entries from index", line):
            stamp = datetime.strptime(
                line[:23], "%Y-%m-%d %H:%M:%S,%f"
            ).astimezone(UTC)
            count += stamp >= after
    return count


def wait_for_rounds(
    proc, log_path: Path, after: datetime, required: int, timeout: int
) -> None:
    """Require observed successful queries instead of estimating elapsed rounds."""
    deadline = time.monotonic() + timeout
    while completed_rounds(log_path, after) < required:
        if proc.poll() is not None:
            raise RuntimeError(
                f"Production daemon exited with {proc.returncode}"
            )
        if time.monotonic() >= deadline:
            raise TimeoutError(
                f"Daemon did not complete {required} required queries"
            )
        time.sleep(0.25)


def delivery_audit(grouped, message_ids, relay_delay_seconds=0):
    messages = []
    pending = []
    for message_id in sorted(message_ids):
        logs = grouped.get(message_id, [])
        sender = re.search(
            r"sender-(?:mx|mailer)-[123]-(single|team)@sender\.test",
            " ".join(log.message for log in logs),
        )
        expected = {"user1@delivery.example.com"}
        if sender and sender.group(1) == "team":
            expected.add("user2@delivery.example.com")
        delivered = set()
        exim_queues = {}
        postfix_queues = {}
        for log in logs:
            if (
                log.hostname.startswith("mailbox")
                and log.service == "postfix/lmtp"
                and "status=sent" in log.message
            ):
                recipient = re.search(r"to=<([^>]+)>", log.message)
                if recipient:
                    delivered.add(recipient.group(1))
            if log.service in ("exim", "exim4") and log.mail_id:
                exim_queues.setdefault((log.hostname, log.mail_id), []).append(
                    log.message
                )
            if log.service.startswith("postfix/") and log.mail_id:
                postfix_queues.setdefault(
                    (log.hostname, log.mail_id), []
                ).append(log)
        incomplete_postfix = [
            list(queue)
            for queue, records in postfix_queues.items()
            if not any("status=sent" in log.message for log in records)
            or not any(
                log.service == "postfix/qmgr" and log.message == "removed"
                for log in records
            )
        ]
        incomplete = []
        delays = []
        for queue, records in exim_queues.items():
            values = [
                float(match.group(1))
                for record in records
                if " => " in record
                if (match := re.search(r"\bDT=([\d.]+)s?", record))
            ]
            if (
                not values
                or min(values) < relay_delay_seconds
                or not any(" <= " in record for record in records)
                or not any(
                    "Completed" in record and "QT=" in record
                    for record in records
                )
            ):
                incomplete.append(list(queue))
            delays.extend(values)
        complete = bool(
            sender
            and exim_queues
            and postfix_queues
            and not incomplete
            and not incomplete_postfix
            and expected <= delivered
        )
        item = {
            "message_id": message_id,
            "complete": complete,
            "expected_recipients": sorted(expected),
            "delivered_recipients": sorted(delivered),
            "completed_exim_queues": len(exim_queues) - len(incomplete),
            "incomplete_exim_queues": incomplete,
            "completed_postfix_queues": len(postfix_queues)
            - len(incomplete_postfix),
            "incomplete_postfix_queues": incomplete_postfix,
            "min_delivery_seconds": min(delays) if delays else None,
            "max_delivery_seconds": max(delays) if delays else None,
        }
        messages.append(item)
        if not complete:
            pending.append(message_id)
    return {
        "verified": not pending,
        "messages": messages,
        "pending_message_ids": pending,
    }


def run_one(
    base_config_path: Path,
    script_path: Path,
    temp_dir: Path,
    size: int,
    wait_seconds: int,
    daemon_start_wait: int,
    tempo_url: str,
    otel_endpoint: str,
    params: ParamSet,
    artifact_dir: Path | None = None,
    relay_delay_seconds: int = 0,
    seed=None,
    progress=None,
) -> RunResult:
    """Run one sidecar correctness experiment."""
    phase = progress or (lambda name: None)
    phase("daemon_startup")
    config_path = write_temp_config(base_config_path, params, temp_dir)
    log_path = temp_dir / (
        f"daemon_sleep{params.sleep_seconds}_hold{params.hold_rounds}_"
        f"goback{params.go_back_seconds}.log"
    )
    if artifact_dir is not None:
        artifact_dir.mkdir(parents=True, exist_ok=False)
        log_path = artifact_dir / "daemon.log"

    proc = start_production_daemon(config_path, otel_endpoint, log_path)
    run_start = time.perf_counter()
    try:
        identity = process_identity(proc.pid)
        if artifact_dir is not None and identity:
            write_json(artifact_dir / "daemon-process.json", identity)
        wait_for_rounds(
            proc, log_path, datetime.min.replace(tzinfo=UTC), 1, 180
        )
        daemon_ready_at = datetime.now(UTC)
        time.sleep(daemon_start_wait)
        run_prefix = f"mailtrace-daemon-{uuid4().hex}"
        message_ids = {
            f"{run_prefix}-{number}@daemon-benchmark.test"
            for number in range(1, size + 1)
        }
        if artifact_dir is not None:
            write_json(
                artifact_dir / "traffic_manifest.json",
                {"message_ids": sorted(message_ids), "seed": seed},
            )
        print(
            f"Daemon ready; sending {size} messages...",
            file=sys.stderr,
            flush=True,
        )
        phase("sending_messages")
        traffic_start = datetime.now(UTC)
        if seed is None:
            send_emails(script_path, size, run_prefix)
        else:
            send_emails(script_path, size, run_prefix, seed)
        traffic_end = datetime.now(UTC)

        if artifact_dir is not None:
            write_json(
                artifact_dir / "readiness.json",
                {
                    "daemon_ready_at": daemon_ready_at.isoformat(),
                    "traffic_start": traffic_start.isoformat(),
                    "verified": daemon_ready_at < traffic_start,
                },
            )
        print(
            f"Waiting at least {wait_seconds} seconds for delivery logs...",
            file=sys.stderr,
            flush=True,
        )
        phase("waiting_for_deliveries")
        time.sleep(wait_seconds)
        config = load_config(str(config_path))
        deadline = time.monotonic() + 900
        while True:
            expected_end = datetime.now(UTC)
            logs = query_all_logs(config, traffic_start, expected_end)
            grouped = {
                mid: entries
                for mid, entries in group_logs_by_message_id(logs).items()
                if mid in message_ids
            }
            audit = delivery_audit(grouped, message_ids, relay_delay_seconds)
            logs = [log for entries in grouped.values() for log in entries]
            if artifact_dir is not None:
                write_json(
                    artifact_dir / "logs.json", [vars(log) for log in logs]
                )
                write_json(artifact_dir / "input_completion.json", audit)
            if audit["verified"]:
                break
            if proc.poll() is not None:
                raise RuntimeError(
                    f"Production daemon exited with {proc.returncode}"
                )
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"Incomplete delivery logs for {audit['pending_message_ids']}"
                )
            time.sleep(5)
        input_ready_at = datetime.now(UTC)
        anchor = input_ready_at + timedelta(seconds=params.go_back_seconds)
        required_rounds = params.hold_rounds + 2
        print(
            f"All {size} deliveries are complete; observing {required_rounds} queries...",
            file=sys.stderr,
            flush=True,
        )
        phase("observing_queries")
        wait_for_rounds(
            proc,
            log_path,
            anchor,
            required_rounds,
            max(600, params.sleep_seconds * required_rounds * 6),
        )
        result_end = datetime.now(UTC)
        if artifact_dir is not None:
            write_json(
                artifact_dir / "observation.json",
                {
                    "input_ready_at": input_ready_at.isoformat(),
                    "anchor": anchor.isoformat(),
                    "required_rounds": required_rounds,
                    "completed_rounds": completed_rounds(log_path, anchor),
                    "verified": True,
                },
            )
        phase("scoring")
        expected = build_expected_signatures(grouped)
        if set(expected) != message_ids:
            raise RuntimeError(
                "The complete-log baseline does not contain every sent message"
            )

        traces = fetch_tempo_traces(
            tempo_url=tempo_url,
            start_dt=traffic_start - timedelta(seconds=5),
            end_dt=result_end + timedelta(seconds=5),
            limit=max(size * 5, 100),
        )
        generated = build_generated_signatures(
            traces,
            expected_message_ids=set(expected),
        )
        correctness = compare_signatures(expected, generated)

        result = RunResult(
            params=params,
            expected=correctness.expected,
            generated=correctness.generated,
            exact=correctness.exact,
            malformed=correctness.malformed,
            missing=correctness.missing,
            exact_rate=correctness.exact_rate,
            duration=time.perf_counter() - run_start,
        )
        if artifact_dir is not None:
            write_json(
                artifact_dir / "signatures.json",
                {
                    "expected": signatures_to_json(expected),
                    "generated": signatures_to_json(generated),
                    "mismatched_message_ids": [
                        message_id
                        for message_id, signature in expected.items()
                        if generated.get(message_id) != signature
                    ],
                },
            )
            write_json(
                artifact_dir / "tempo_traces.json",
                [
                    trace_obj
                    for trace_obj in traces
                    if any(
                        attribute_map(span).get("message.id") in expected
                        for span in trace_spans(trace_obj)
                    )
                ],
            )
            write_json(artifact_dir / "logs.json", [vars(log) for log in logs])
            write_json(
                artifact_dir / "result.json",
                {
                    **result_to_json(result),
                    "sleep_seconds": params.sleep_seconds,
                    "duration_seconds": result.duration,
                    "traffic_start": traffic_start.isoformat(),
                    "traffic_end": traffic_end.isoformat(),
                    "result_end": result_end.isoformat(),
                    "expected_end": expected_end.isoformat(),
                    "daemon_ready_at": daemon_ready_at.isoformat(),
                    "input_ready_at": input_ready_at.isoformat(),
                },
            )
        return result
    finally:
        stop_process(proc)


def signatures_to_json(signatures: TraceSignatures) -> dict[str, Any]:
    """Serialize host and stage names for direct comparison."""
    return {
        message_id: {
            host: {"stages": list(signature.stages)}
            for host, signature in hosts.items()
        }
        for message_id, hosts in signatures.items()
    }


def print_table(results: list[RunResult]) -> None:
    """Print sidecar correctness results."""
    headers = [
        "sleep",
        "hold",
        "go_back",
        "expected",
        "generated",
        "exact",
        "malformed",
        "missing",
        "exact_pct",
        "duration_s",
    ]
    rows = [
        [
            str(result.params.sleep_seconds),
            str(result.params.hold_rounds),
            str(result.params.go_back_seconds),
            str(result.expected),
            str(result.generated),
            str(result.exact),
            str(result.malformed),
            str(result.missing),
            f"{result.exact_rate * 100:.2f}",
            f"{result.duration:.2f}",
        ]
        for result in results
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


def result_to_json(result: RunResult) -> dict[str, int | float]:
    """Return JSON-serialisable result without sleep or duration."""
    return {
        "hold": result.params.hold_rounds,
        "go_back": result.params.go_back_seconds,
        "expected": result.expected,
        "generated": result.generated,
        "exact": result.exact,
        "malformed": result.malformed,
        "missing": result.missing,
        "exact_pct": round(result.exact_rate * 100, 2),
    }


def mean(values: list[float]) -> float:
    """Return arithmetic mean."""
    return statistics.mean(values)


def sample_std(values: list[float]) -> float:
    """Return sample standard deviation, or zero for one value."""
    return statistics.stdev(values) if len(values) > 1 else 0.0


def aggregate_results(
    params: ParamSet,
    results: list[RunResult],
) -> AggregatedResult:
    """Aggregate repeated runs for one parameter set."""
    expected = [float(result.expected) for result in results]
    generated = [float(result.generated) for result in results]
    exact = [float(result.exact) for result in results]
    malformed = [float(result.malformed) for result in results]
    missing = [float(result.missing) for result in results]
    exact_pct = [result.exact_rate * 100 for result in results]
    return AggregatedResult(
        params=params,
        runs=len(results),
        expected_mean=mean(expected),
        expected_std=sample_std(expected),
        generated_mean=mean(generated),
        generated_std=sample_std(generated),
        exact_mean=mean(exact),
        exact_std=sample_std(exact),
        malformed_mean=mean(malformed),
        malformed_std=sample_std(malformed),
        missing_mean=mean(missing),
        missing_std=sample_std(missing),
        exact_pct_mean=mean(exact_pct),
        exact_pct_std=sample_std(exact_pct),
    )


def aggregated_result_to_json(
    result: AggregatedResult,
) -> dict[str, int | float]:
    """Return JSON-serialisable aggregate without sleep or duration."""
    return {
        "\u03c1": result.params.hold_rounds,
        "\u03d5": result.params.go_back_seconds,
        "runs": result.runs,
        "expected_mean": round(result.expected_mean, 3),
        "expected_std": round(result.expected_std, 3),
        "generated_mean": round(result.generated_mean, 3),
        "generated_std": round(result.generated_std, 3),
        "exact_mean": round(result.exact_mean, 3),
        "exact_std": round(result.exact_std, 3),
        "malformed_mean": round(result.malformed_mean, 3),
        "malformed_std": round(result.malformed_std, 3),
        "missing_mean": round(result.missing_mean, 3),
        "missing_std": round(result.missing_std, 3),
        "exact_pct_mean": round(result.exact_pct_mean, 3),
        "exact_pct_std": round(result.exact_pct_std, 3),
    }


def print_aggregated_table(results: list[AggregatedResult]) -> None:
    """Print aggregate correctness results for completed parameter sets."""
    headers = [
        "sleep",
        "hold",
        "go_back",
        "runs",
        "expected_mean±std",
        "generated_mean±std",
        "exact_mean±std",
        "malformed_mean±std",
        "missing_mean±std",
        "exact_pct_mean±std",
    ]
    rows = [
        [
            str(result.params.sleep_seconds),
            str(result.params.hold_rounds),
            str(result.params.go_back_seconds),
            str(result.runs),
            f"{result.expected_mean:.3f}±{result.expected_std:.3f}",
            f"{result.generated_mean:.3f}±{result.generated_std:.3f}",
            f"{result.exact_mean:.3f}±{result.exact_std:.3f}",
            f"{result.malformed_mean:.3f}±{result.malformed_std:.3f}",
            f"{result.missing_mean:.3f}±{result.missing_std:.3f}",
            f"{result.exact_pct_mean:.3f}±{result.exact_pct_std:.3f}",
        ]
        for result in results
    ]
    widths = [
        max(len(header), *(len(row[index]) for row in rows))
        for index, header in enumerate(headers)
    ]
    print(
        " | ".join(
            header.rjust(widths[index]) for index, header in enumerate(headers)
        )
    )
    print("-+-".join("-" * width for width in widths))
    for row in rows:
        print(
            " | ".join(
                cell.rjust(widths[index]) for index, cell in enumerate(row)
            )
        )


def build_parser() -> argparse.ArgumentParser:
    """Build CLI parser."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=DEFAULT_CONFIG)
    parser.add_argument("--script", default=DEFAULT_SCRIPT)
    parser.add_argument("--tempo-url", default=DEFAULT_TEMPO_URL)
    parser.add_argument("--otel-endpoint", default=DEFAULT_OTEL_ENDPOINT)
    parser.add_argument("--size", type=int, default=DEFAULT_SIZE)
    parser.add_argument("--wait", type=int, default=DEFAULT_WAIT)
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--daemon-start-wait", type=int, default=5)
    parser.add_argument("--keep-temp", action="store_true")
    parser.add_argument(
        "--reset-environment",
        action="store_true",
        help="Build the demo images, then clean and recreate the demo stack for every repeat",
    )
    parser.add_argument(
        "--relay-delay-seconds",
        type=int,
        default=0,
        help="Outbound SMTP delay for the managed demo stack",
    )
    parser.add_argument(
        "--output-dir",
        help="Save per-run signatures, Tempo traces, logs, and aggregate results",
    )
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--resume",
        metavar="PROGRESS_JSON",
        help="Resume a compatible checkpoint without rerunning committed repeats",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=0,
        help="Base seed for repeatable delivery route selection",
    )
    parser.add_argument(
        "--sleep-seconds", type=int, default=DEFAULT_SLEEP_SECONDS
    )
    parser.add_argument(
        "--hold-values",
        nargs="+",
        type=int,
        default=DEFAULT_HOLD_VALUES,
    )
    parser.add_argument(
        "--go-back-values",
        nargs="+",
        type=int,
        default=DEFAULT_GO_BACK_VALUES,
    )
    parser.add_argument(
        "--params",
        nargs="+",
        type=parse_param_set,
        default=None,
        metavar="SLEEP,HOLD,GO_BACK",
        help="Override deterministic grid with explicit parameter triples",
    )
    return parser


def resolve_path(path: str) -> Path:
    """Resolve a path relative to the project root."""
    resolved = Path(path)
    if not resolved.is_absolute():
        resolved = _PROJECT_ROOT / resolved
    return resolved.resolve()


def publish_partial(progress):
    """Derive every summary from the same committed repeats."""
    records = progress.records()
    write_json(progress.output / "runs.json", records)
    grouped = {}
    for row in records:
        params = ParamSet(row["sleep_seconds"], row["hold"], row["go_back"])
        grouped.setdefault(params, []).append(
            RunResult(
                params,
                row["expected"],
                row["generated"],
                row["exact"],
                row["malformed"],
                row["missing"],
                row["exact"] / row["expected"],
                row["duration_seconds"],
            )
        )
    aggregates = [
        aggregate_results(params, rows) for params, rows in grouped.items()
    ]
    write_json(
        progress.output / "aggregate.json",
        [aggregated_result_to_json(row) for row in aggregates],
    )
    return aggregates


def main() -> int:
    """Run or explicitly resume one checkpointed daemon experiment."""
    parser = build_parser()
    args = parser.parse_args()
    saved = None
    if args.resume:
        if any(
            token.startswith("--")
            and token.split("=", 1)[0]
            not in ("--resume", "--json", "--keep-temp")
            for token in sys.argv[1:]
        ):
            parser.error(
                "--resume restores experiment options; only --json and --keep-temp may be supplied"
            )
        checkpoint = resolve_path(args.resume)
        if checkpoint.name != "progress.json":
            parser.error("--resume requires the experiment's progress.json")
        saved = json.loads(checkpoint.read_text())
        try:
            options = saved["manifest"]["options"]
            args = argparse.Namespace(**{**vars(args), **options})
        except (KeyError, TypeError) as error:
            raise ValueError(
                "Invalid checkpoint; legacy output without a manifest cannot be resumed"
            ) from error
        output_dir = checkpoint.parent
    else:
        output_dir = (
            resolve_path(args.output_dir)
            if args.output_dir
            else _PROJECT_ROOT
            / "results"
            / (
                "daemon-correctness-"
                + datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ-")
                + uuid4().hex[:8]
            )
        )
    if args.size <= 0 or args.runs <= 0 or args.sleep_seconds <= 0:
        parser.error("size, runs, and sleep-seconds must be positive")
    if (
        min(
            args.wait,
            args.daemon_start_wait,
            args.relay_delay_seconds,
            args.seed,
            *args.hold_values,
            *args.go_back_values,
        )
        < 0
    ):
        parser.error(
            "waits, delays, seed, hold values, and go-back values must be non-negative"
        )
    if args.relay_delay_seconds and not args.reset_environment:
        parser.error("--relay-delay-seconds requires --reset-environment")
    logging.basicConfig(level=logging.WARNING)
    config_path = resolve_path(args.config)
    script_path = resolve_path(args.script)
    param_sets = (
        [ParamSet(*p) for p in args.params]
        if args.params
        else build_param_grid(
            args.sleep_seconds, args.hold_values, args.go_back_values
        )
    )
    if len(param_sets) != len(set(param_sets)):
        parser.error("parameter sets must be unique")
    options = {
        key: value
        for key, value in vars(args).items()
        if key not in ("resume", "output_dir", "keep_temp", "json")
    }
    options.update(config=str(config_path), script=str(script_path))
    options = json.loads(json.dumps(options))
    plan = [
        {
            "id": f"sleep{p.sleep_seconds}_hold{p.hold_rounds}_goback{p.go_back_seconds}/run-{i:02d}",
            "params": p._asdict(),
            "run": i,
            "seed": (args.seed + i) % 32768,
        }
        for p in param_sets
        for i in range(1, args.runs + 1)
    ]
    manifest = compatibility_manifest(_PROJECT_ROOT, options)
    if not saved:
        output_dir.mkdir(parents=True, exist_ok=False)
    resources = ExitStack()
    progress = None
    temp_dir = None
    try:
        # Serialize checkpoint writers even when using an externally managed stack.
        lock = resources.enter_context(
            (output_dir / "progress.lock").open("a")
        )
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("Another process owns this checkpoint")
        if args.resume:
            saved = json.loads((output_dir / "progress.json").read_text())
        progress = Progress(output_dir, manifest, plan, saved)
        if saved and all(
            u["state"] == "completed" for u in progress.data["units"]
        ):
            if saved["state"] != "completed":
                publish_partial(progress)
                progress.finish("completed")
            print(
                "All repeats are already committed; no environment changes.",
                flush=True,
            )
            return 0
        if args.reset_environment:
            lock = resources.enter_context(
                (
                    Path(tempfile.gettempdir())
                    / "mailtrace-daemon-benchmark.lock"
                ).open("a")
            )
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError(
                    "Another daemon benchmark owns the demo stack"
                )
        progress.start()
        publish_partial(progress)
        print(
            f"Progress: {output_dir / 'progress.json'}",
            file=sys.stderr,
            flush=True,
        )
        temp_dir = Path(tempfile.mkdtemp(prefix="mailtrace-daemon-bench-"))
        recover_daemons(progress)
        environment = None
        if args.reset_environment:
            environment = DemoStack(
                Path(__file__).with_name("docker-compose.yml"),
                output_dir / "environment",
                load_config(str(config_path)),
                args.tempo_url,
                args.relay_delay_seconds,
            )
            environment.on_phase = progress.phase
            environment_directory = output_dir / "environment"
            environment_directory.mkdir(parents=True, exist_ok=True)
            progress.phase("recovering_environment")
            environment.cleanup(
                environment_directory
                / (
                    "resume-cleanup-"
                    + datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
                )
            )
            progress.phase("preparing_environment")
            print("Preparing demo images...", file=sys.stderr, flush=True)
            environment.prepare(resume=bool(saved))
            pinned = environment_directory / "compose.json"
            if pinned.is_file():
                progress.data["environment_sha256"] = digest(pinned)
                progress.publish()
        for unit in progress.data["units"]:
            if unit["state"] == "completed":
                continue
            relative = progress.begin(unit)
            params = ParamSet(**unit["params"])
            print(
                f"Running {unit['id']} (attempt {len(unit['attempts'])})",
                flush=True,
            )
            progress.phase(
                "starting_environment" if environment else "running"
            )
            scope = (
                environment.run(output_dir / "environment" / relative)
                if environment
                else nullcontext()
            )
            with scope:
                result = run_one(
                    base_config_path=config_path,
                    script_path=script_path,
                    temp_dir=temp_dir,
                    size=args.size,
                    wait_seconds=(
                        max(args.wait, 100) if environment else args.wait
                    ),
                    daemon_start_wait=args.daemon_start_wait,
                    tempo_url=args.tempo_url,
                    otel_endpoint=args.otel_endpoint,
                    params=params,
                    artifact_dir=output_dir / relative,
                    relay_delay_seconds=args.relay_delay_seconds,
                    seed=unit["seed"],
                    progress=progress.phase,
                )
                progress.phase("cleaning_environment")
                print(
                    "Scoring complete; cleaning demo resources...",
                    file=sys.stderr,
                    flush=True,
                )
            record = {
                **result_to_json(result),
                "sleep_seconds": params.sleep_seconds,
                "run": unit["run"],
                "duration_seconds": result.duration,
                "artifact_dir": str(relative),
            }
            progress.complete(unit, record)
            aggregates = publish_partial(progress)
            print(
                f"Committed {len(progress.records())}/{len(plan)} repeats: exact={result.exact}/{result.expected}, malformed={result.malformed}, missing={result.missing}",
                flush=True,
            )
        aggregates = publish_partial(progress)
        progress.finish("completed")
        if args.json:
            print(
                json.dumps(
                    [aggregated_result_to_json(row) for row in aggregates],
                    ensure_ascii=False,
                ),
                flush=True,
            )
        else:
            print_aggregated_table(aggregates)
        return 0
    except KeyboardInterrupt as error:
        if progress and progress.thread:
            progress.finish("interrupted", error)
        print(
            "Interrupted; committed results are retained. Resume using progress.json.",
            file=sys.stderr,
            flush=True,
        )
        return 130
    except BaseException as error:
        if progress and progress.thread:
            progress.finish("failed", error)
        raise
    finally:
        if progress:
            progress.close()
        resources.close()
        if temp_dir:
            if args.keep_temp:
                print(f"Temp dir: {temp_dir}")
            else:
                shutil.rmtree(temp_dir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
