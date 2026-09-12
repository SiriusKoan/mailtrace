#!/usr/bin/env python3
"""Measure one Docker container through its cgroup v2 files."""

import argparse
import csv
import json
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, TextIO, Tuple

CSV_FIELDS = (
    "timestamp",
    "cpu_usage_usec_delta",
    "cpu_percent",
    "memory_current_bytes",
)
SUMMARY_FIELDS = (
    "container",
    "started_at",
    "ended_at",
    "wall_time_seconds",
    "sample_count",
    "cpu_usage_usec_delta",
    "cpu_percent_average",
    "cpu_percent_max",
    "memory_current_bytes_start",
    "memory_current_bytes_end",
    "memory_current_bytes_average",
    "memory_current_bytes_min",
    "memory_current_bytes_max",
    "stop_reason",
)
CGROUP_ROOT = Path("/sys/fs/cgroup")
PROC_ROOT = Path("/proc")
SAMPLE_INTERVAL_SECONDS = 1.0
POLL_INTERVAL_SECONDS = 0.1


@dataclass(frozen=True)
class ResourceSample:
    cpu_usage_usec: int
    memory_current_bytes: int


def _parse_key_values(text: str, source: str) -> dict[str, int]:
    values = {}
    for line in text.splitlines():
        fields = line.split()
        if len(fields) != 2 or fields[0] in values:
            raise ValueError(f"invalid {source} data: {line!r}")
        try:
            value = int(fields[1])
        except ValueError as exc:
            raise ValueError(f"invalid {source} value: {line!r}") from exc
        if value < 0:
            raise ValueError(f"negative {source} value: {line!r}")
        values[fields[0]] = value
    return values


def parse_cgroup_v2_path(text: str) -> str:
    unified_path = None
    for line in text.splitlines():
        fields = line.split(":", 2)
        if len(fields) != 3:
            raise ValueError(f"invalid cgroup entry: {line!r}")
        hierarchy, controllers, path = fields
        if hierarchy == "0" and controllers == "":
            if unified_path is not None or not path.startswith("/"):
                raise ValueError("invalid cgroup v2 path")
            unified_path = path
    if unified_path is None:
        raise ValueError("cgroup v2 entry 0:: is missing")
    return unified_path


def parse_cpu_usage_usec(text: str) -> int:
    values = _parse_key_values(text, "cpu.stat")
    try:
        return values["usage_usec"]
    except KeyError as exc:
        raise ValueError("cpu.stat does not contain usage_usec") from exc


def parse_memory_current(text: str) -> int:
    try:
        value = int(text.strip())
    except ValueError as exc:
        raise ValueError("invalid memory.current data") from exc
    if value < 0:
        raise ValueError("negative memory.current value")
    return value


def parse_cgroup_populated(text: str) -> bool:
    values = _parse_key_values(text, "cgroup.events")
    try:
        populated = values["populated"]
    except KeyError as exc:
        raise ValueError("cgroup.events does not contain populated") from exc
    if populated not in (0, 1):
        raise ValueError("invalid populated value in cgroup.events")
    return bool(populated)


def cpu_percent(cpu_delta_usec: int, elapsed_seconds: float) -> float:
    if cpu_delta_usec < 0:
        raise ValueError("CPU usage counter moved backwards")
    if elapsed_seconds <= 0:
        raise ValueError("sampling interval must be positive")
    return cpu_delta_usec / (elapsed_seconds * 1_000_000) * 100


def resolve_cgroup_path(
    pid: int,
    cgroup_root: Path = CGROUP_ROOT,
    proc_root: Path = PROC_ROOT,
) -> Path:
    root = cgroup_root.resolve()
    if not (root / "cgroup.controllers").is_file():
        raise RuntimeError(f"cgroup v2 is not mounted at {root}")

    relative = parse_cgroup_v2_path(
        (proc_root / str(pid) / "cgroup").read_text(encoding="utf-8")
    )
    path = (root / relative.lstrip("/")).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise RuntimeError(
            "container cgroup path escapes cgroup root"
        ) from exc
    return path


def inspect_container_pid(container: str) -> int:
    result = subprocess.run(
        ["docker", "inspect", container],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        message = result.stderr.strip() or "docker inspect failed"
        raise RuntimeError(message)
    try:
        inspected = json.loads(result.stdout)
        state = inspected[0]["State"]
        pid = int(state["Pid"])
    except (
        IndexError,
        KeyError,
        TypeError,
        ValueError,
        json.JSONDecodeError,
    ) as exc:
        raise RuntimeError("invalid docker inspect output") from exc
    if not state.get("Running") or pid <= 0:
        raise RuntimeError(f"container is not running: {container}")
    return pid


class CgroupReader:
    def __init__(
        self,
        pid: int,
        cgroup_path: Path,
        proc_root: Path = PROC_ROOT,
    ) -> None:
        self.pid = pid
        self.cgroup_path = cgroup_path
        self.proc_root = proc_root
        self.relative_path = parse_cgroup_v2_path(
            (proc_root / str(pid) / "cgroup").read_text(encoding="utf-8")
        )

    def read_sample(self) -> ResourceSample:
        cpu = parse_cpu_usage_usec(
            (self.cgroup_path / "cpu.stat").read_text(encoding="utf-8")
        )
        memory = parse_memory_current(
            (self.cgroup_path / "memory.current").read_text(encoding="utf-8")
        )
        return ResourceSample(cpu, memory)

    def is_active(self) -> bool:
        try:
            current_path = parse_cgroup_v2_path(
                (self.proc_root / str(self.pid) / "cgroup").read_text(
                    encoding="utf-8"
                )
            )
            events = (self.cgroup_path / "cgroup.events").read_text(
                encoding="utf-8"
            )
        except FileNotFoundError:
            return False
        return current_path == self.relative_path and parse_cgroup_populated(
            events
        )


def open_output_csv(path: Path) -> Tuple[TextIO, csv.writer]:
    output = path.open("a+", encoding="utf-8", newline="")
    try:
        output.seek(0)
        rows = csv.reader(output)
        header = next(rows, None)
        if header is None:
            writer = csv.writer(output)
            writer.writerow(CSV_FIELDS)
            output.flush()
        elif tuple(header) != CSV_FIELDS:
            raise ValueError(
                "CSV header does not match: " + ",".join(CSV_FIELDS)
            )
        output.seek(0, 2)
        return output, csv.writer(output)
    except Exception:
        output.close()
        raise


def _summary(
    container: str,
    started_wall: float,
    ended_wall: float,
    started_mono: float,
    ended_mono: float,
    sample_count: int,
    first: ResourceSample,
    last: ResourceSample,
    memories: list[int],
    interval_percentages: list[float],
    stop_reason: str,
) -> dict[str, object]:
    wall_time = max(0.0, ended_mono - started_mono)
    cpu_delta = last.cpu_usage_usec - first.cpu_usage_usec
    if cpu_delta < 0:
        raise ValueError("CPU usage counter moved backwards")
    average = cpu_percent(cpu_delta, wall_time) if wall_time else 0.0
    return {
        "container": container,
        "started_at": int(started_wall),
        "ended_at": int(ended_wall),
        "wall_time_seconds": wall_time,
        "sample_count": sample_count,
        "cpu_usage_usec_delta": cpu_delta,
        "cpu_percent_average": average,
        "cpu_percent_max": max(interval_percentages, default=0.0),
        "memory_current_bytes_start": first.memory_current_bytes,
        "memory_current_bytes_end": last.memory_current_bytes,
        "memory_current_bytes_average": sum(memories) / len(memories),
        "memory_current_bytes_min": min(memories),
        "memory_current_bytes_max": max(memories),
        "stop_reason": stop_reason,
    }


def collect_resources(
    container: str,
    output_path: Path,
    reader: CgroupReader,
    *,
    wall_clock: Callable[[], float] = time.time,
    monotonic: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
    poll_interval: float = POLL_INTERVAL_SECONDS,
) -> Tuple[dict[str, object], Optional[str]]:
    output, writer = open_output_csv(output_path)
    try:
        first = reader.read_sample()
        started_mono = monotonic()
        started_wall = wall_clock()
        last = first
        last_mono = started_mono
        memories = [first.memory_current_bytes]
        percentages: list[float] = []
        sample_count = 0
        next_deadline = started_mono + SAMPLE_INTERVAL_SECONDS
        stop_reason = "signal"
        error = None

        try:
            while True:
                while monotonic() < next_deadline:
                    if not reader.is_active():
                        stop_reason = "container_stopped"
                        break
                    remaining = next_deadline - monotonic()
                    sleeper(min(poll_interval, remaining))
                else:
                    if not reader.is_active():
                        stop_reason = "container_stopped"
                        break
                    current = reader.read_sample()
                    current_mono = monotonic()
                    delta = current.cpu_usage_usec - last.cpu_usage_usec
                    percent = cpu_percent(delta, current_mono - last_mono)
                    writer.writerow(
                        (
                            int(wall_clock()),
                            delta,
                            percent,
                            current.memory_current_bytes,
                        )
                    )
                    output.flush()
                    sample_count += 1
                    memories.append(current.memory_current_bytes)
                    percentages.append(percent)
                    last = current
                    last_mono = current_mono
                    next_deadline += SAMPLE_INTERVAL_SECONDS
                    while next_deadline <= current_mono:
                        next_deadline += SAMPLE_INTERVAL_SECONDS
                    continue
                break
        except KeyboardInterrupt:
            stop_reason = "signal"
        except (OSError, RuntimeError, ValueError) as exc:
            stop_reason = "read_error"
            error = str(exc)

        if stop_reason != "read_error":
            try:
                final = reader.read_sample()
                final_mono = monotonic()
                final_delta = final.cpu_usage_usec - last.cpu_usage_usec
                if final_delta < 0:
                    raise ValueError("CPU usage counter moved backwards")
                if final_mono > last_mono:
                    percentages.append(
                        cpu_percent(final_delta, final_mono - last_mono)
                    )
                memories.append(final.memory_current_bytes)
                last = final
            except ValueError as exc:
                stop_reason = "read_error"
                error = str(exc)
            except (OSError, RuntimeError) as exc:
                if stop_reason == "signal":
                    stop_reason = "read_error"
                    error = f"final sample failed: {exc}"
                elif stop_reason == "container_stopped":
                    error = f"final sample unavailable: {exc}"

        ended_mono = monotonic()
        ended_wall = wall_clock()
        try:
            summary = _summary(
                container,
                started_wall,
                ended_wall,
                started_mono,
                ended_mono,
                sample_count,
                first,
                last,
                memories,
                percentages,
                stop_reason,
            )
        except ValueError as exc:
            error = str(exc)
            summary = _summary(
                container,
                started_wall,
                ended_wall,
                started_mono,
                ended_mono,
                sample_count,
                first,
                first,
                [first.memory_current_bytes],
                [],
                "read_error",
            )
        return summary, error
    finally:
        output.close()


def empty_error_summary(container: str, now: float) -> dict[str, object]:
    return {
        "container": container,
        "started_at": int(now),
        "ended_at": int(now),
        "wall_time_seconds": 0.0,
        "sample_count": 0,
        "cpu_usage_usec_delta": 0,
        "cpu_percent_average": 0.0,
        "cpu_percent_max": 0.0,
        "memory_current_bytes_start": None,
        "memory_current_bytes_end": None,
        "memory_current_bytes_average": None,
        "memory_current_bytes_min": None,
        "memory_current_bytes_max": None,
        "stop_reason": "read_error",
    }


def run(container: str, output_path: Path) -> int:
    try:
        pid = inspect_container_pid(container)
        cgroup_path = resolve_cgroup_path(pid)
        reader = CgroupReader(pid, cgroup_path)
        summary, error = collect_resources(container, output_path, reader)
    except (OSError, RuntimeError, ValueError) as exc:
        now = time.time()
        summary = empty_error_summary(container, now)
        error = str(exc)

    if error:
        print(error, file=sys.stderr)
    json.dump(summary, sys.stdout, separators=(",", ":"))
    sys.stdout.write("\n")
    sys.stdout.flush()
    return 1 if summary["stop_reason"] == "read_error" else 0


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Measure Docker container CPU and memory usage"
    )
    parser.add_argument("--container", required=True)
    parser.add_argument("--output", required=True, type=Path)
    return parser.parse_args(argv)


def main() -> int:
    args = parse_args()
    return run(args.container, args.output)


if __name__ == "__main__":
    raise SystemExit(main())
