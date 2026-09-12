#!/usr/bin/env python3
"""
Sidecar correctness benchmark for the production tracing daemon.

This script does not call daemon internals.  For each parameter set, it starts
the normal CLI daemon, sends test emails, reads expected traces from complete
OpenSearch logs, reads generated traces from Tempo, and compares structures.
"""

from __future__ import annotations

import argparse
import json
import logging
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
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, NamedTuple

import yaml

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_PROJECT_ROOT))

from mailtrace.config import load_config  # noqa: E402
from mailtrace.tracing.delay_parser import (  # noqa: E402
    EXIM_DELAY_STAGES,
    POSTFIX_DELAY_STAGES,
)
from mailtrace.tracing.query import (  # noqa: E402
    group_logs_by_hosts,
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
        "uv",
        "run",
        "python",
        "-m",
        "mailtrace",
        "tracing",
        "--config-path",
        str(config_path),
        "--otel-endpoint",
        otel_endpoint,
    ]
    log_file = log_path.open("w", encoding="utf-8")
    return subprocess.Popen(
        cmd,
        cwd=_PROJECT_ROOT,
        stdout=log_file,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )


def stop_process(proc: subprocess.Popen, timeout: int = 10) -> None:
    """Terminate a process group."""
    if proc.poll() is not None:
        return
    try:
        proc.send_signal(signal.SIGINT)
        proc.wait(timeout=timeout)
        return
    except subprocess.TimeoutExpired:
        pass
    proc.terminate()
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=timeout)


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


def expected_stages_from_logs(logs) -> tuple[str, ...]:
    """Infer expected delay span names from raw log signals."""
    postfix_delay_pattern = (
        r"\bdelays=\d+(?:\.\d+)?/\d+(?:\.\d+)?/" r"\d+(?:\.\d+)?/\d+(?:\.\d+)?"
    )
    if any(re.search(postfix_delay_pattern, log.message) for log in logs):
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


def build_expected_signatures(logs_by_message_id) -> TraceSignatures:
    """Build expected trace structures from complete OpenSearch logs."""
    signatures: TraceSignatures = {}
    for message_id, message_id_logs in logs_by_message_id.items():
        host_signatures: TraceSignature = {}
        for host, host_logs in group_logs_by_hosts(message_id_logs).items():
            host_signatures[host] = HostSignature(
                stages=expected_stages_from_logs(host_logs)
            )
        if host_signatures:
            signatures[message_id] = host_signatures
    return signatures


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
            delay_spans = []
            for child in child_spans.get(span_id, []):
                child_attributes = attribute_map(child)
                if "delay.duration_seconds" in child_attributes:
                    delay_spans.append(child)
            delay_spans.sort(
                key=lambda span: int(span.get("startTimeUnixNano", 0))
            )
            signatures.setdefault(message_id, {})[host] = HostSignature(
                stages=tuple(span["name"] for span in delay_spans)
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
) -> RunResult:
    """Run one sidecar correctness experiment."""
    config_path = write_temp_config(base_config_path, params, temp_dir)
    log_path = temp_dir / (
        f"daemon_sleep{params.sleep_seconds}_hold{params.hold_rounds}_"
        f"goback{params.go_back_seconds}.log"
    )

    proc = start_production_daemon(config_path, otel_endpoint, log_path)
    run_start = time.perf_counter()
    try:
        time.sleep(daemon_start_wait)
        traffic_start = datetime.now(UTC)
        send_emails(script_path, size)
        traffic_end = datetime.now(UTC)

        settle_wait = (
            wait_seconds
            + params.sleep_seconds * (params.hold_rounds + 2)
            + params.go_back_seconds
        )
        time.sleep(settle_wait)
        result_end = datetime.now(UTC)

        if proc.poll() is not None:
            tail = log_path.read_text(encoding="utf-8")[-1200:]
            raise RuntimeError(
                f"production daemon exited with {proc.returncode}\n{tail}"
            )

        config = load_config(str(config_path))
        expected_end = traffic_end + timedelta(seconds=wait_seconds + 10)
        logs = query_all_logs(config, traffic_start, expected_end)
        expected = build_expected_signatures(group_logs_by_message_id(logs))

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

        return RunResult(
            params=params,
            expected=correctness.expected,
            generated=correctness.generated,
            exact=correctness.exact,
            malformed=correctness.malformed,
            missing=correctness.missing,
            exact_rate=correctness.exact_rate,
            duration=time.perf_counter() - run_start,
        )
    finally:
        stop_process(proc)


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
    parser.add_argument("--json", action="store_true")
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


def main() -> int:
    """Run sidecar daemon correctness benchmark."""
    args = build_parser().parse_args()
    logging.basicConfig(level=logging.WARNING)

    config_path = resolve_path(args.config)
    script_path = resolve_path(args.script)
    temp_dir = Path(tempfile.mkdtemp(prefix="mailtrace-daemon-bench-"))
    param_sets = args.params or build_param_grid(
        sleep_seconds=args.sleep_seconds,
        hold_values=args.hold_values,
        go_back_values=args.go_back_values,
    )

    try:
        results = []
        aggregated_results = []
        for params in param_sets:
            repeated_results = []
            print(
                f"Running sleep={params.sleep_seconds}, "
                f"hold={params.hold_rounds}, "
                f"go_back={params.go_back_seconds}",
                flush=True,
            )
            for run_index in range(1, args.runs + 1):
                print(
                    f"  run {run_index}/{args.runs}",
                    flush=True,
                )
                result = run_one(
                    base_config_path=config_path,
                    script_path=script_path,
                    temp_dir=temp_dir,
                    size=args.size,
                    wait_seconds=args.wait,
                    daemon_start_wait=args.daemon_start_wait,
                    tempo_url=args.tempo_url,
                    otel_endpoint=args.otel_endpoint,
                    params=params,
                )
                results.append(result)
                repeated_results.append(result)
            aggregated_results.append(
                aggregate_results(params, repeated_results)
            )

        print()
        if args.json:
            if args.runs > 1:
                print(
                    json.dumps(
                        [
                            aggregated_result_to_json(result)
                            for result in aggregated_results
                        ],
                        ensure_ascii=False,
                    )
                )
            else:
                print(
                    json.dumps(
                        [result_to_json(result) for result in results],
                        ensure_ascii=False,
                    )
                )
        else:
            print_table(results)
        return 0
    finally:
        if args.keep_temp:
            print(f"Temp dir: {temp_dir}")
        else:
            shutil.rmtree(temp_dir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
