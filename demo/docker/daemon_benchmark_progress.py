"""Atomic checkpoints and a heartbeat for the daemon correctness experiment."""

import _thread
import hashlib
import json
import os
import platform
import sys
import tempfile
import threading
from datetime import UTC, datetime
from importlib.metadata import distributions
from pathlib import Path
from uuid import uuid4


def now():
    return datetime.now(UTC).isoformat()


def write_json(path, value):
    """Publish complete JSON snapshots; guarantee process-crash recovery."""
    path = Path(path)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, delete=False
        ) as stream:
            temporary = Path(stream.name)
            json.dump(value, stream, ensure_ascii=False, indent=2, default=str)
            stream.write("\n")
            stream.flush()
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def host_resources():
    """Record shared-host pressure without changing host or container limits."""
    result = {}
    if Path("/proc/meminfo").exists():
        memory = dict(
            line.split(":", 1)
            for line in Path("/proc/meminfo").read_text().splitlines()
        )
        result["memory_kib"] = {
            key: int(memory[key].split()[0])
            for key in ("MemAvailable", "MemTotal", "SwapFree", "SwapTotal")
        }
    for name in ("memory", "io", "cpu"):
        path = Path("/proc/pressure") / name
        if path.exists():
            result[name + "_pressure"] = path.read_text().strip()
    return result


def compatibility_manifest(root, options):
    files = set((root / "mailtrace").rglob("*.py"))
    files.update((root / "demo/docker").glob("daemon_benchmark_*.py"))
    files.update((root / "demo/docker").rglob("Dockerfile"))
    for context in (
        "loghost",
        "mailer",
        "mx",
        "mailpolicy",
        "mailbox",
        "rsyslog",
    ):
        files.update(
            p
            for p in (root / "demo/docker" / context).rglob("*")
            if p.is_file()
        )
    files.update(
        root / name
        for name in (
            "demo/docker/bench_daemon_correctness.py",
            "demo/docker/docker-compose.yml",
            "tempo.yaml",
            "pyproject.toml",
            "uv.lock",
        )
    )
    files.update(Path(options[key]) for key in ("config", "script"))
    return {
        "options": options,
        "files": {str(p): digest(p) for p in sorted(files) if p.is_file()},
        "python": sys.version,
        "platform": platform.platform(),
        "cpu_count": os.cpu_count(),
        "dependencies": sorted(
            [d.metadata["Name"], d.version] for d in distributions()
        ),
        "protocol": "cold-stack-per-repeat; complete-log-baseline; seed-per-repeat; v1",
    }


def fingerprint(manifest):
    return hashlib.sha256(
        json.dumps(manifest, sort_keys=True).encode()
    ).hexdigest()


class Progress:
    """One coordinated writer commits results together with unit completion."""

    def __init__(self, output, manifest, plan, saved=None):
        self.output = Path(output)
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.thread = None
        self.reporting_error = None
        self.data = saved or {
            "schema_version": 1,
            "run_id": uuid4().hex,
            "manifest": manifest,
            "fingerprint": fingerprint(manifest),
            "started_at": now(),
            "state": "running",
            "phase": "preparing_environment",
            "heartbeat_seconds": 5,
            "active_unit": None,
            "environment_sha256": None,
            "units": [
                {**unit, "state": "pending", "attempts": []} for unit in plan
            ],
        }
        if saved:
            self.validate(manifest, plan)

    def validate(self, manifest, plan):
        data = self.data
        if data.get("schema_version") != 1:
            raise ValueError(
                "Unsupported checkpoint schema; use a fresh output directory"
            )
        if (
            data.get("fingerprint") != fingerprint(manifest)
            or data.get("manifest") != manifest
        ):
            raise ValueError(
                "Incompatible benchmark implementation, inputs, or environment; start a fresh run"
            )
        units = data.get("units", [])
        if [{key: u[key] for key in plan[0]} for u in units] != plan:
            raise ValueError(
                "Checkpoint work plan does not match the requested experiment"
            )
        for unit in units:
            if unit["state"] not in (
                "pending",
                "running",
                "failed",
                "completed",
            ):
                raise ValueError("Invalid checkpoint unit state")
            if unit["state"] == "completed":
                record = unit["result"]
                if (
                    record["expected"] <= 0
                    or record["exact"]
                    + record["malformed"]
                    + record["missing"]
                    != record["expected"]
                ):
                    raise ValueError("Invalid committed correctness counts")
                for relative, checksum in unit["files"].items():
                    path = (self.output / relative).resolve()
                    if (
                        not path.is_relative_to(self.output.resolve())
                        or not path.is_file()
                        or digest(path) != checksum
                    ):
                        raise ValueError(
                            f"Missing or changed committed artifact: {relative}"
                        )
        checksum = data.get("environment_sha256")
        if (
            checksum
            and digest(self.output / "environment/compose.json") != checksum
        ):
            raise ValueError(
                "Pinned experiment environment changed; use a fresh run"
            )

    def records(self):
        return [
            u["result"]
            for u in self.data["units"]
            if u["state"] == "completed"
        ]

    def publish(self):
        with self.lock:
            if self.reporting_error:
                raise RuntimeError(
                    "Benchmark progress persistence failed"
                ) from self.reporting_error
            data = self.data
            data["updated_at"] = now()
            data["host_resources"] = host_resources()
            states = [u["state"] for u in data["units"]]
            data["total_runs"] = len(states)
            data["completed_runs"] = states.count("completed")
            data["failed_runs"] = states.count("failed")
            data["active_runs"] = states.count("running")
            data["pending_runs"] = states.count("pending")
            write_json(self.output / "progress.json", data)
            write_json(
                self.output / "status.json",
                {
                    key: value
                    for key, value in data.items()
                    if key not in ("units", "manifest")
                },
            )

    def heartbeat(self):
        while not self.stop.wait(5):
            try:
                self.publish()
            except BaseException as error:
                self.reporting_error = error
                _thread.interrupt_main()
                return

    def start(self):
        for unit in self.data["units"]:
            if unit["state"] == "running":
                unit["state"] = "failed"
                unit["attempts"][-1].update(
                    state="interrupted",
                    error="Previous process ended before commit",
                )
        self.data.pop("error", None)
        self.data.setdefault("sessions", []).append({"started_at": now()})
        self.data.update(
            state="running", phase="preparing_environment", active_unit=None
        )
        self.publish()
        self.thread = threading.Thread(target=self.heartbeat, daemon=True)
        self.thread.start()

    def close(self):
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=6)

    def phase(self, name):
        with self.lock:
            self.data["phase"] = name
            self.publish()

    def begin(self, unit):
        with self.lock:
            number = len(unit["attempts"]) + 1
            relative = (
                unit["id"]
                if number == 1
                else f'{unit["id"]}/attempt-{number:02d}'
            )
            unit["attempts"].append(
                {
                    "number": number,
                    "started_at": now(),
                    "state": "running",
                    "artifact_dir": relative,
                }
            )
            unit["state"] = "running"
            self.data.update(
                active_unit=unit["id"],
                current={**unit["params"], "run": unit["run"]},
            )
            self.publish()
            return Path(relative)

    def complete(self, unit, record):
        with self.lock:
            artifact = self.output / record["artifact_dir"]
            environment = self.output / "environment" / record["artifact_dir"]
            for name in (
                "result.json",
                "logs.json",
                "signatures.json",
                "tempo_traces.json",
                "traffic_manifest.json",
                "readiness.json",
                "input_completion.json",
                "observation.json",
                "daemon.log",
            ):
                if not (artifact / name).is_file():
                    raise RuntimeError(
                        f"Cannot commit a repeat with missing artifacts: {artifact / name}"
                    )
            if self.data["manifest"]["options"]["reset_environment"]:
                cleanup = json.loads(
                    (environment / "after/cleanup.json").read_text()
                )
                if not cleanup["verified"] or any(cleanup["after"].values()):
                    raise RuntimeError(
                        "Cannot commit a repeat before verified environment cleanup"
                    )
            unit["files"] = {
                str(path.relative_to(self.output)): digest(path)
                for directory in (artifact, environment)
                for path in directory.rglob("*")
                if path.is_file()
            }
            unit.update(state="completed", result=record)
            unit["attempts"][-1].update(state="completed", finished_at=now())
            self.data.update(active_unit=None, phase="between_runs")
            self.publish()

    def finish(self, state, error=None):
        with self.lock:
            if self.reporting_error:
                raise RuntimeError(
                    "Benchmark progress persistence failed"
                ) from self.reporting_error
            if error:
                detail = {
                    "type": type(error).__name__,
                    "message": str(error),
                    "notes": getattr(error, "__notes__", []),
                    "at": now(),
                }
                for unit in self.data["units"]:
                    if unit["state"] == "running":
                        unit["state"] = "failed"
                        unit["attempts"][-1].update(state=state, error=detail)
                self.data["error"] = detail
            self.data.update(state=state, phase=state, active_unit=None)
            self.publish()
