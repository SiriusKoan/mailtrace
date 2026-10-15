import contextlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from demo.docker import bench_daemon_correctness as bench
from demo.docker import daemon_benchmark_progress as checkpoint
from demo.docker.daemon_benchmark_stack import DemoStack


class ResumeTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        patcher = patch.object(
            bench.tempfile, "gettempdir", return_value=directory.name
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.output = Path(directory.name) / "output"
        self.stack = Mock()
        self.stack.run.side_effect = self.environment
        self.measured = []

    @contextlib.contextmanager
    def environment(self, directory):
        try:
            yield
        finally:
            (directory / "after").mkdir(parents=True, exist_ok=True)
            checkpoint.write_json(
                directory / "after/cleanup.json",
                {
                    "verified": True,
                    "after": {"containers": [], "networks": [], "volumes": []},
                },
            )

    def measure(self, **kwargs):
        self.measured.append((str(kwargs["artifact_dir"]), kwargs["seed"]))
        directory = kwargs["artifact_dir"]
        directory.mkdir(parents=True)
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
            (directory / name).write_text("{}")
        return bench.RunResult(kwargs["params"], 10, 0, 0, 0, 10, 0, 1)

    def cli(self, resume=False, measure=None):
        argv = ["bench_daemon_correctness.py"]
        argv += (
            ["--resume", str(self.output / "progress.json")]
            if resume
            else [
                "--reset-environment",
                "--params",
                "10,0,0",
                "--runs",
                "2",
                "--output-dir",
                str(self.output),
            ]
        )
        with (
            patch.object(sys, "argv", argv),
            patch.object(bench, "DemoStack", return_value=self.stack),
            patch.object(
                bench, "run_one", side_effect=measure or self.measure
            ),
        ):
            return bench.main()

    def fail_second(self, **kwargs):
        if self.measured:
            raise TimeoutError("Daemon did not complete required queries")
        return self.measure(**kwargs)

    def test_resume_retains_commits_and_replays_only_failed_unit(self):
        with self.assertRaises(TimeoutError):
            self.cli(measure=self.fail_second)
        saved = json.loads((self.output / "progress.json").read_text())
        first = saved["units"][0]
        self.assertEqual(saved["completed_runs"], 1)
        self.assertEqual(saved["failed_runs"], 1)
        self.assertEqual(self.cli(resume=True), 0)
        final = json.loads((self.output / "progress.json").read_text())
        self.assertEqual(final["run_id"], saved["run_id"])
        self.assertEqual(final["units"][0], first)
        self.assertEqual(len(self.measured), 2)
        self.assertIn("attempt-02", self.measured[1][0])
        self.assertEqual(self.measured[1][1], 2)
        self.assertEqual(final["completed_runs"], 2)
        self.assertEqual(final["failed_runs"], 0)
        self.assertEqual(len(final["units"][1]["attempts"]), 2)
        self.assertEqual(
            len(json.loads((self.output / "runs.json").read_text())), 2
        )

    def test_completed_resume_does_not_prepare_reset_or_measure(self):
        self.cli()
        self.stack.reset_mock()
        count = len(self.measured)
        self.assertEqual(self.cli(resume=True), 0)
        self.assertEqual(len(self.measured), count)
        self.stack.prepare.assert_not_called()
        self.stack.cleanup.assert_not_called()
        self.stack.run.assert_not_called()

    def test_resume_accepts_checkpoint_left_running_after_abrupt_exit(self):
        with self.assertRaises(TimeoutError):
            self.cli(measure=self.fail_second)
        path = self.output / "progress.json"
        saved = json.loads(path.read_text())
        saved["state"] = "running"
        saved["units"][1]["state"] = "running"
        saved["units"][1]["attempts"][-1]["state"] = "running"
        checkpoint.write_json(path, saved)
        self.assertEqual(self.cli(resume=True), 0)
        final = json.loads(path.read_text())
        self.assertEqual(
            final["units"][1]["attempts"][0]["state"], "interrupted"
        )
        self.assertEqual(len(self.measured), 2)

    def test_artifact_corruption_rejected_before_environment_reset(self):
        self.cli()
        (self.output / "sleep10_hold0_goback0/run-01/logs.json").write_text(
            "changed"
        )
        self.stack.reset_mock()
        with self.assertRaisesRegex(ValueError, "committed artifact"):
            self.cli(resume=True)
        self.stack.cleanup.assert_not_called()
        self.stack.prepare.assert_not_called()

    def test_incompatible_implementation_rejected_before_reset(self):
        self.cli()
        self.stack.reset_mock()
        original = bench.compatibility_manifest

        def incompatible(*args):
            return {**original(*args), "protocol": "different"}

        with patch.object(
            bench, "compatibility_manifest", side_effect=incompatible
        ):
            with self.assertRaisesRegex(ValueError, "Incompatible"):
                self.cli(resume=True)
        self.stack.cleanup.assert_not_called()

    def test_committed_unit_survives_failure_before_derived_report(self):
        original = bench.publish_partial
        count = 0

        def publish(progress):
            nonlocal count
            count += 1
            if count == 2:
                raise OSError("derived report write failed")
            return original(progress)

        with patch.object(bench, "publish_partial", side_effect=publish):
            with self.assertRaises(OSError):
                self.cli()
        self.assertEqual(self.cli(resume=True), 0)
        self.assertEqual(len(self.measured), 2)

    def test_last_commit_survives_failure_before_final_report(self):
        original = bench.publish_partial
        count = 0

        def publish(progress):
            nonlocal count
            count += 1
            if count == 3:
                raise OSError("final report write failed")
            return original(progress)

        with patch.object(bench, "publish_partial", side_effect=publish):
            with self.assertRaises(OSError):
                self.cli()
        self.stack.reset_mock()
        self.assertEqual(self.cli(resume=True), 0)
        self.assertEqual(len(self.measured), 2)
        self.stack.cleanup.assert_not_called()
        data = json.loads((self.output / "progress.json").read_text())
        self.assertEqual(data["state"], "completed")
        self.assertEqual(
            len(json.loads((self.output / "runs.json").read_text())), 2
        )

    def test_missing_raw_artifact_prevents_committing_a_result(self):
        def incomplete(**kwargs):
            result = self.measure(**kwargs)
            (kwargs["artifact_dir"] / "logs.json").unlink()
            return result

        with self.assertRaisesRegex(RuntimeError, "missing artifacts"):
            self.cli(measure=incomplete)
        data = json.loads((self.output / "progress.json").read_text())
        self.assertEqual(data["completed_runs"], 0)
        self.assertEqual(data["failed_runs"], 1)

    def test_corrupt_checkpoint_and_unknown_schema_do_not_reset(self):
        self.cli()
        path = self.output / "progress.json"
        saved = json.loads(path.read_text())
        self.stack.reset_mock()
        path.write_text("{")
        with self.assertRaises(json.JSONDecodeError):
            self.cli(resume=True)
        saved["schema_version"] = 999
        checkpoint.write_json(path, saved)
        with self.assertRaisesRegex(ValueError, "schema"):
            self.cli(resume=True)
        self.stack.cleanup.assert_not_called()

    def test_failed_initial_reset_stops_before_measurement(self):
        self.stack.cleanup.side_effect = RuntimeError("reset failed")
        with self.assertRaisesRegex(RuntimeError, "reset failed"):
            self.cli()
        self.assertEqual(self.measured, [])
        data = json.loads((self.output / "progress.json").read_text())
        self.assertEqual(data["state"], "failed")
        self.assertEqual(data["completed_runs"], 0)

    def test_reporting_thread_failure_interrupts_scheduling(self):
        manifest = {"options": {"reset_environment": False}}
        progress = checkpoint.Progress(self.output, manifest, [{"id": "one"}])
        with (
            patch.object(progress.stop, "wait", return_value=False),
            patch.object(
                progress, "publish", side_effect=OSError("disk full")
            ),
            patch.object(checkpoint._thread, "interrupt_main") as interrupt,
        ):
            progress.heartbeat()
        interrupt.assert_called_once()
        with self.assertRaisesRegex(RuntimeError, "persistence failed"):
            progress.finish("completed")

    def test_sender_route_selection_repeats_with_the_same_seed(self):
        directory = self.output.parent
        shim = directory / "swaks"
        shim.write_text("#!/bin/sh\nexit 0\n")
        shim.chmod(0o755)
        env = {
            **os.environ,
            "PATH": str(directory) + os.pathsep + os.environ["PATH"],
            "MAILTRACE_SEED": "123",
        }
        command = ["bash", str(bench.resolve_path(bench.DEFAULT_SCRIPT)), "12"]
        first = subprocess.run(
            command, env=env, capture_output=True, text=True, check=True
        )
        second = subprocess.run(
            command, env=env, capture_output=True, text=True, check=True
        )
        self.assertEqual(first.stdout, second.stdout)
        self.assertIn("successful=12 failed=0", first.stdout)

    def test_atomic_write_failure_keeps_last_valid_checkpoint(self):
        self.output.mkdir()
        path = self.output / "progress.json"
        checkpoint.write_json(path, {"committed": 1})
        with patch.object(
            checkpoint.os, "replace", side_effect=OSError("disk full")
        ):
            with self.assertRaises(OSError):
                checkpoint.write_json(path, {"committed": 2})
        self.assertEqual(json.loads(path.read_text()), {"committed": 1})
        self.assertEqual(list(self.output.iterdir()), [path])

    def test_live_owner_rejects_resume_without_reset(self):
        self.cli()
        self.stack.reset_mock()
        with patch.object(bench.fcntl, "flock", side_effect=BlockingIOError):
            with self.assertRaisesRegex(RuntimeError, "owns this checkpoint"):
                self.cli(resume=True)
        self.stack.cleanup.assert_not_called()


class HostReadinessTest(unittest.TestCase):
    def test_host_pressure_prevents_starting_more_containers(self):
        with tempfile.TemporaryDirectory() as directory:
            stack = DemoStack(
                Path("compose.yml"),
                Path(directory),
                SimpleNamespace(opensearch_config=None),
                "http://tempo",
                40,
            )
            with (
                patch(
                    "demo.docker.daemon_benchmark_stack.host_resources",
                    return_value={
                        "memory_kib": {"MemTotal": 32000000, "MemAvailable": 0}
                    },
                ),
                patch("time.monotonic", side_effect=[0, 901]),
                patch.object(stack, "command") as command,
            ):
                with self.assertRaisesRegex(TimeoutError, "Shared host"):
                    stack.start(Path(directory))
            command.assert_not_called()
            self.assertFalse(
                json.loads(
                    (Path(directory) / "host-readiness.json").read_text()
                )["verified"]
            )


if __name__ == "__main__":
    unittest.main()
