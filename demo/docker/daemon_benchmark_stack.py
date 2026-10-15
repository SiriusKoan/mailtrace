"""Own the demo Compose stack for one daemon benchmark repeat at a time."""

import json
import logging
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from contextlib import contextmanager
from pathlib import Path

from demo.docker.daemon_benchmark_progress import host_resources, write_json
from opensearchpy import OpenSearch

SERVICES = (
    "opensearch",
    "loghost",
    "mx",
    "mailer1",
    "mailer2",
    "mailer3",
    "mailpolicy1",
    "mailpolicy2",
    "mailpolicy3",
    "mailbox",
    "tempo",
)
PROJECT = "docker"


@contextmanager
def finish_cleanup():
    """Finish bounded cleanup commands even when Ctrl+C is pressed again."""
    previous = signal.signal(signal.SIGINT, signal.SIG_IGN)
    try:
        yield
    finally:
        signal.signal(signal.SIGINT, previous)


class DemoStack:
    def __init__(
        self, compose_file, directory, config, tempo_url, relay_delay_seconds
    ):
        self.on_phase = lambda name: None
        self.directory = Path(directory)
        self.config = config.opensearch_config
        self.tempo_url = tempo_url.rstrip("/")
        self.env = dict(
            os.environ, MAILTRACE_RELAY_DELAY_SECONDS=str(relay_delay_seconds)
        )
        self.original = [
            "docker",
            "compose",
            "-p",
            PROJECT,
            "-f",
            str(compose_file),
        ]
        self.compose = [
            "docker",
            "compose",
            "-p",
            PROJECT,
            "-f",
            str(self.directory / "compose.json"),
        ]

    def command(self, command, timeout=60):
        return subprocess.run(
            command,
            env=self.env,
            capture_output=True,
            text=True,
            check=True,
            timeout=timeout,
        ).stdout

    def prepare(self, resume=False):
        self.directory.mkdir(parents=True, exist_ok=True)
        pinned = self.directory / "compose.json"
        if resume and pinned.exists():
            data = json.loads(pinned.read_text())
            for service in data["services"].values():
                self.command(["docker", "image", "inspect", service["image"]])
            return
        data = json.loads(
            self.command([*self.original, "config", "--format", "json"])
        )
        data["services"] = {name: data["services"][name] for name in SERVICES}
        built = [
            name
            for name, service in data["services"].items()
            if "build" in service
        ]
        subprocess.run(
            [*self.original, "build", *built],
            env=self.env,
            stdout=sys.stderr,
            stderr=sys.stderr,
            check=True,
            timeout=600,
        )
        for name, service in data["services"].items():
            image = service.get("image", f"{PROJECT}-{name}")
            try:
                image_id = self.command(
                    [
                        "docker",
                        "image",
                        "inspect",
                        "--format",
                        "{{.Id}}",
                        image,
                    ]
                ).strip()
            except subprocess.CalledProcessError:
                subprocess.run(
                    ["docker", "pull", image],
                    stdout=sys.stderr,
                    stderr=sys.stderr,
                    check=True,
                    timeout=600,
                )
                image_id = self.command(
                    [
                        "docker",
                        "image",
                        "inspect",
                        "--format",
                        "{{.Id}}",
                        image,
                    ]
                ).strip()
            service["image"] = image_id
            service.pop("build", None)
            service["cpus"] = (
                2.0
                if name == "opensearch"
                else 1.0 if name == "tempo" else 0.5
            )
        (self.directory / "compose.json").write_text(
            json.dumps(data, indent=2)
        )

    def resources(self):
        result = {}
        label = f"label=com.docker.compose.project={PROJECT}"
        for kind, command in {
            "containers": ["docker", "ps", "-aq", "--filter", label],
            "networks": ["docker", "network", "ls", "-q", "--filter", label],
            "volumes": ["docker", "volume", "ls", "-q", "--filter", label],
        }.items():
            result[kind] = self.command(command, timeout=30).split()
        return result

    def cleanup(self, directory):
        with finish_cleanup():
            directory.mkdir(parents=True, exist_ok=True)
            errors = []
            for attempt in range(3):
                try:
                    before = {}
                    try:
                        before = self.resources()
                    except (OSError, subprocess.SubprocessError) as error:
                        errors.append(f"Inventory before cleanup: {error}")
                    if before.get("containers"):
                        try:
                            self.command(
                                [
                                    "docker",
                                    "rm",
                                    "-f",
                                    "-v",
                                    *before["containers"],
                                ],
                                timeout=60,
                            )
                        except (OSError, subprocess.SubprocessError) as error:
                            errors.append(f"Force removal: {error}")
                    # Compose teardown must still run when inventory times out.
                    try:
                        output = self.command(
                            [
                                *(
                                    self.compose
                                    if (
                                        self.directory / "compose.json"
                                    ).exists()
                                    else self.original
                                ),
                                "down",
                                "--volumes",
                                "--remove-orphans",
                                "--timeout",
                                "5",
                            ],
                            timeout=60,
                        )
                        (directory / "compose-down.log").write_text(output)
                    except (OSError, subprocess.SubprocessError) as error:
                        errors.append(f"Compose teardown: {error}")
                    after = self.resources()
                    if any(after.values()):
                        raise RuntimeError(
                            f"Experiment resources remain: {after}"
                        )
                    (directory / "cleanup.json").write_text(
                        json.dumps(
                            {
                                "verified": True,
                                "before": before,
                                "after": after,
                                "previous_errors": errors,
                            },
                            indent=2,
                        )
                    )
                    return
                except (
                    OSError,
                    subprocess.SubprocessError,
                    RuntimeError,
                ) as error:
                    errors.append(str(error))
                    (directory / "cleanup.json").write_text(
                        json.dumps(
                            {"verified": False, "errors": errors}, indent=2
                        )
                    )
                    if attempt == 2:
                        raise RuntimeError(
                            "Demo cleanup failed; the next repeat must not start"
                        ) from error
                    time.sleep(2)

    def wait_for_host(self, directory):
        self.on_phase("waiting_for_host")
        deadline = time.monotonic() + 900
        healthy = 0
        while True:
            sample = host_resources()
            memory = sample.get("memory_kib", {})
            available = memory.get("MemAvailable", 0)
            minimum = min(3 * 1024 * 1024, memory.get("MemTotal", 0) // 10)
            pressure = {}
            for name in ("memory", "io"):
                lines = sample.get(name + "_pressure", "").splitlines()
                full = next(
                    (line for line in lines if line.startswith("full ")), ""
                )
                pressure[name] = float(
                    dict(item.split("=") for item in full.split()[1:]).get(
                        "avg10", 0
                    )
                )
            ready = (
                available >= minimum
                and pressure["memory"] < 5
                and pressure["io"] < 20
            )
            healthy = healthy + 1 if ready else 0
            write_json(
                directory / "host-readiness.json",
                {
                    "verified": healthy >= 2,
                    "resources": sample,
                    "minimum_available_kib": minimum,
                },
            )
            if healthy >= 2:
                return
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    "Shared host remains under memory or I/O pressure; resume after resources recover"
                )
            if healthy == 0:
                print(
                    "Waiting for shared-host memory and I/O pressure to recover...",
                    file=sys.stderr,
                    flush=True,
                )
            time.sleep(5)

    def start(self, directory):
        self.wait_for_host(directory)
        self.on_phase("starting_opensearch")
        output = self.command(
            [
                *self.compose,
                "up",
                "-d",
                "--no-build",
                "--pull",
                "never",
                "opensearch",
                "loghost",
            ],
            timeout=120,
        )
        (directory / "compose-up.log").write_text(output)
        cfg = self.config
        client = OpenSearch(
            hosts=[{"host": cfg.host, "port": cfg.port}],
            http_auth=(cfg.username, cfg.password),
            use_ssl=cfg.use_ssl,
            verify_certs=cfg.verify_certs,
            ssl_show_warn=False,
            timeout=3,
            max_retries=0,
        )
        logger = logging.getLogger("opensearch")
        previous_level = logger.level
        logger.setLevel(logging.ERROR)
        print(
            "Waiting for OpenSearch readiness (up to 600 seconds)...",
            file=sys.stderr,
            flush=True,
        )
        try:
            deadline = time.monotonic() + 600
            while True:
                try:
                    if client.cluster.health()["status"] in (
                        "green",
                        "yellow",
                    ):
                        break
                except Exception:
                    if time.monotonic() >= deadline:
                        raise
                if time.monotonic() >= deadline:
                    raise TimeoutError("OpenSearch did not become ready")
                time.sleep(2)
            if not client.indices.exists(index=cfg.index):
                client.indices.create(
                    index=cfg.index,
                    body={
                        "mappings": {
                            "properties": {
                                cfg.mapping.timestamp: {"type": "date"}
                            }
                        }
                    },
                )
        finally:
            logger.setLevel(previous_level)
            client.close()
        print(
            "OpenSearch is ready; starting mail services and Tempo...",
            file=sys.stderr,
            flush=True,
        )
        self.on_phase("starting_mail_services")
        output = self.command(
            [
                *self.compose,
                "up",
                "-d",
                "--no-build",
                "--pull",
                "never",
                *SERVICES,
            ],
            timeout=120,
        )
        with (directory / "compose-up.log").open("a") as stream:
            stream.write(output)
        deadline = time.monotonic() + 180
        while True:
            try:
                with urllib.request.urlopen(
                    self.tempo_url + "/ready", timeout=2
                ) as response:
                    if response.status != 200:
                        raise OSError("Tempo is not ready")
                for port in (10025, 20025, 20026, 20027):
                    with socket.create_connection(
                        ("127.0.0.1", port), timeout=2
                    ) as connection:
                        if not connection.recv(128).startswith(b"220"):
                            raise OSError(
                                f"SMTP endpoint {port} did not greet"
                            )
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("Tempo or SMTP did not become ready")
                time.sleep(1)
        ids = self.command([*self.compose, "ps", "-q"]).split()
        containers = json.loads(self.command(["docker", "inspect", *ids]))
        if len(containers) != len(SERVICES) or any(
            item["HostConfig"]["Memory"] != 0
            or item["HostConfig"]["MemorySwap"] != 0
            for item in containers
        ):
            raise RuntimeError(
                "Demo must contain eleven services without container memory limits"
            )
        (directory / "environment.json").write_text(
            json.dumps(
                [
                    {
                        "id": item["Id"],
                        "name": item["Name"],
                        "image": item["Image"],
                        "memory_limit": item["HostConfig"]["Memory"],
                        "memory_swap_limit": item["HostConfig"]["MemorySwap"],
                    }
                    for item in containers
                ],
                indent=2,
            )
        )

    @contextmanager
    def run(self, directory):
        directory = Path(directory)
        self.cleanup(directory / "before")
        original_error = None
        try:
            self.start(directory)
            yield
        except BaseException as error:
            original_error = error
            raise
        finally:
            with finish_cleanup():
                try:
                    output = self.command(
                        [*self.compose, "logs", "--no-color", "--tail", "200"],
                        timeout=10,
                    )
                    (directory / "compose.log").write_text(output)
                except (OSError, subprocess.SubprocessError):
                    pass
                finally:
                    try:
                        self.cleanup(directory / "after")
                    except BaseException as cleanup_error:
                        if original_error is None:
                            raise
                        original_error.add_note(
                            f"Cleanup also failed: {cleanup_error}"
                        )
                        print(
                            f"Cleanup also failed: {cleanup_error}",
                            file=sys.stderr,
                            flush=True,
                        )
