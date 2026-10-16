import contextlib
import json
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from demo.docker import bench_daemon_correctness as bench
from demo.docker.daemon_benchmark_stack import DemoStack

from mailtrace.models import LogEntry


class ManagedLifecycleTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        patcher = patch.object(
            bench.tempfile, "gettempdir", return_value=directory.name
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_direct_cli_rebuilds_each_repeat_and_retains_zero_scores(self):
        events = []
        stack = Mock()

        @contextlib.contextmanager
        def environment(directory):
            events.append("start")
            try:
                yield
            finally:
                events.append("cleanup")
                (directory / "after").mkdir(parents=True)
                (directory / "after/cleanup.json").write_text(
                    json.dumps(
                        {
                            "verified": True,
                            "after": {
                                "containers": [],
                                "networks": [],
                                "volumes": [],
                            },
                        }
                    )
                )

        stack.run.side_effect = environment

        def measure(**kwargs):
            events.append("measure")
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
            return bench.RunResult(kwargs["params"], 10, 10, 0, 10, 0, 0, 1.0)

        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "results"
            argv = [
                "bench_daemon_correctness.py",
                "--reset-environment",
                "--relay-delay-seconds",
                "40",
                "--params",
                "10,0,0",
                "--runs",
                "2",
                "--output-dir",
                str(output),
                "--json",
            ]
            with (
                patch.object(sys, "argv", argv),
                patch.object(
                    bench, "DemoStack", return_value=stack, create=True
                ),
                patch.object(bench, "run_one", side_effect=measure),
            ):
                self.assertEqual(bench.main(), 0)
            self.assertEqual(events, ["start", "measure", "cleanup"] * 2)
            records = json.loads((output / "runs.json").read_text())
            self.assertEqual(len(records), 2)
            self.assertEqual([row["exact"] for row in records], [0, 0])

    def test_failed_cleanup_prevents_the_next_measurement(self):
        stack = Mock()

        @contextlib.contextmanager
        def environment(directory):
            yield
            raise RuntimeError("cleanup failed")

        stack.run.side_effect = environment
        result = bench.RunResult(
            bench.ParamSet(10, 0, 0), 10, 10, 0, 10, 0, 0, 1
        )
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "results"
            argv = [
                "bench_daemon_correctness.py",
                "--reset-environment",
                "--params",
                "10,0,0",
                "--runs",
                "2",
                "--output-dir",
                str(output),
            ]
            with (
                patch.object(sys, "argv", argv),
                patch.object(bench, "DemoStack", return_value=stack),
                patch.object(bench, "run_one", return_value=result) as measure,
            ):
                with self.assertRaisesRegex(RuntimeError, "cleanup failed"):
                    bench.main()
            self.assertEqual(measure.call_count, 1)
            status = json.loads((output / "status.json").read_text())
            self.assertEqual(status["state"], "failed")

    def test_keyboard_interrupt_records_cancellation_after_cleanup(self):
        stack = Mock()
        events = []

        @contextlib.contextmanager
        def environment(directory):
            try:
                yield
            finally:
                events.append("cleanup")

        stack.run.side_effect = environment
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "results"
            argv = [
                "bench_daemon_correctness.py",
                "--reset-environment",
                "--params",
                "10,0,0",
                "--output-dir",
                str(output),
            ]
            with (
                patch.object(sys, "argv", argv),
                patch.object(bench, "DemoStack", return_value=stack),
                patch.object(bench, "run_one", side_effect=KeyboardInterrupt),
            ):
                self.assertEqual(bench.main(), 130)
            self.assertEqual(events, ["cleanup"])
            self.assertEqual(
                json.loads((output / "status.json").read_text())["state"],
                "interrupted",
            )

    def test_daemon_shutdown_signals_the_entire_process_group(self):
        process = Mock(pid=531)
        process.poll.return_value = None
        process.wait.side_effect = [
            subprocess.TimeoutExpired("daemon", 10),
            None,
        ]
        with patch.object(bench.os, "killpg") as kill:
            bench.stop_process(process)
        self.assertEqual(
            kill.call_args_list,
            [
                unittest.mock.call(531, signal.SIGINT),
                unittest.mock.call(531, signal.SIGTERM),
            ],
        )


class MeasurementBoundaryTest(unittest.TestCase):
    def logs(self, message_id, queue):
        def log(host, service, mid, message):
            return LogEntry(
                datetime="2026-10-15T00:00:00+00:00",
                hostname=host,
                service=service,
                mail_id=mid,
                message=message,
            )

        return [
            log(
                "mailer1",
                "exim",
                queue,
                f" <= sender-mx-1-team@sender.test id={message_id}",
            ),
            log("mailer1", "exim", queue, " => mailbox DT=40s QT=40s"),
            log("mailer1", "exim", queue, "Completed QT=40s"),
            log(
                "mailbox",
                "postfix/cleanup",
                queue,
                f"message-id=<{message_id}>",
            ),
            log(
                "mailbox",
                "postfix/lmtp",
                queue,
                "to=<user1@delivery.example.com>, status=sent",
            ),
            log(
                "mailbox",
                "postfix/lmtp",
                queue,
                "to=<user2@delivery.example.com>, status=sent",
            ),
            log("mailbox", "postfix/qmgr", queue, "removed"),
        ]

    def test_complete_input_requires_both_alias_recipients_and_queue_completion(
        self,
    ):
        logs = self.logs("message@test", "QUEUE")
        self.assertTrue(
            bench.delivery_audit({"message@test": logs}, {"message@test"}, 40)[
                "verified"
            ]
        )
        for omitted in (5, 6, 2):
            incomplete = [
                log for index, log in enumerate(logs) if index != omitted
            ]
            self.assertFalse(
                bench.delivery_audit(
                    {"message@test": incomplete}, {"message@test"}, 40
                )["verified"]
            )

    def test_readiness_waits_for_a_successful_query(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "daemon.log"
            path.write_text("")

            def ready():
                time.sleep(0.05)
                stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S,%f")[:-3]
                path.write_text(
                    f"{stamp} - mailtrace - INFO - Found 0 log entries from index\n"
                )

            thread = threading.Thread(target=ready)
            thread.start()
            process = Mock()
            process.poll.return_value = None
            bench.wait_for_rounds(
                process, path, datetime.min.replace(tzinfo=UTC), 1, 2
            )
            thread.join()
            self.assertEqual(
                bench.completed_rounds(path, datetime.min.replace(tzinfo=UTC)),
                1,
            )

    def test_run_scopes_baseline_and_waits_before_sending(self):
        message_id = "mailtrace-daemon-fixed-1@daemon-benchmark.test"
        logs = self.logs(message_id, "OWN") + self.logs(
            "unrelated@test", "OTHER"
        )
        events = []

        def daemon(config, endpoint, path):
            path.write_text("")
            process = Mock()
            process.poll.return_value = None
            return process

        def wait(*args):
            events.append("query-boundary")

        expected = {
            message_id: {"mailer1": bench.HostSignature(("transmission",))}
        }
        with (
            tempfile.TemporaryDirectory() as temporary,
            patch.object(
                bench, "uuid4", return_value=SimpleNamespace(hex="fixed")
            ),
            patch.object(bench, "start_production_daemon", side_effect=daemon),
            patch.object(bench, "stop_process"),
            patch.object(bench, "wait_for_rounds", side_effect=wait),
            patch.object(bench, "completed_rounds", return_value=2),
            patch.object(bench.time, "sleep") as sleep,
            patch.object(
                bench,
                "send_emails",
                side_effect=lambda *args: events.append("send"),
            ),
            patch.object(
                bench, "query_all_logs", side_effect=[[], logs]
            ) as query,
            patch.object(
                bench, "build_expected_signatures", return_value=expected
            ) as baseline,
            patch.object(bench, "fetch_tempo_traces", return_value=[]),
        ):
            result = bench.run_one(
                bench.resolve_path(bench.DEFAULT_CONFIG),
                Path("sender"),
                Path(temporary),
                1,
                0,
                "http://tempo",
                "http://otel",
                bench.ParamSet(10, 0, 0),
                Path(temporary) / "run",
                40,
            )
            self.assertEqual(result.missing, 1)
            self.assertEqual(
                events, ["query-boundary", "send", "query-boundary"]
            )
            self.assertEqual(query.call_count, 2)
            self.assertEqual(
                sleep.call_args_list,
                [unittest.mock.call(0), unittest.mock.call(5)],
            )
            self.assertEqual(set(baseline.call_args.args[0]), {message_id})


class StackCleanupTest(unittest.TestCase):
    def test_force_cleanup_removes_all_resources_between_repeats(self):
        state = {
            "containers": ["old-container"],
            "networks": ["old-network"],
            "volumes": ["old-volume"],
        }
        starts = []
        with tempfile.TemporaryDirectory() as temporary:
            stack = DemoStack(
                Path("compose.yml"),
                Path(temporary),
                SimpleNamespace(opensearch_config=None),
                "http://tempo",
                40,
            )

            def command(cmd, **kwargs):
                if cmd[1] == "ps":
                    return "\n".join(state["containers"])
                if cmd[1:3] == ["network", "ls"]:
                    return "\n".join(state["networks"])
                if cmd[1:3] == ["volume", "ls"]:
                    return "\n".join(state["volumes"])
                if cmd[1] == "rm":
                    self.assertIn("-f", cmd)
                    self.assertNotIn("unrelated-container", cmd)
                    state["containers"] = []
                if "down" in cmd:
                    self.assertIn("--volumes", cmd)
                    state["networks"] = []
                    state["volumes"] = []
                return ""

            def start(directory):
                self.assertFalse(any(state.values()))
                starts.append(True)
                for key in state:
                    state[key] = ["run-resource"]

            with (
                patch.object(stack, "command", side_effect=command),
                patch.object(stack, "start", side_effect=start),
            ):
                for number in range(2):
                    with stack.run(Path(temporary) / str(number)):
                        pass
            self.assertEqual(len(starts), 2)
            self.assertFalse(any(state.values()))

    def test_log_capture_interrupt_still_runs_cleanup(self):
        with tempfile.TemporaryDirectory() as temporary:
            stack = DemoStack(
                Path("compose.yml"),
                Path(temporary),
                SimpleNamespace(opensearch_config=None),
                "http://tempo",
                40,
            )
            with (
                patch.object(stack, "start"),
                patch.object(stack, "cleanup") as cleanup,
                patch.object(stack, "command", side_effect=KeyboardInterrupt),
            ):
                with self.assertRaises(KeyboardInterrupt):
                    with stack.run(Path(temporary)):
                        pass
            self.assertEqual(cleanup.call_count, 2)

    def test_sigint_during_cleanup_does_not_leave_resources(self):
        handler = signal.getsignal(signal.SIGINT)
        with tempfile.TemporaryDirectory() as temporary:
            stack = DemoStack(
                Path("compose.yml"),
                Path(temporary),
                SimpleNamespace(opensearch_config=None),
                "http://tempo",
                40,
            )

            def command(*args, **kwargs):
                signal.raise_signal(signal.SIGINT)
                return ""

            with (
                patch.object(
                    stack,
                    "resources",
                    return_value={
                        "containers": [],
                        "networks": [],
                        "volumes": [],
                    },
                ),
                patch.object(stack, "command", side_effect=command),
            ):
                try:
                    stack.cleanup(Path(temporary))
                except KeyboardInterrupt:
                    self.fail("SIGINT skipped cleanup completion")
            self.assertTrue(
                json.loads((Path(temporary) / "cleanup.json").read_text())[
                    "verified"
                ]
            )
        self.assertEqual(signal.getsignal(signal.SIGINT), handler)

    def test_inventory_timeout_does_not_skip_compose_down(self):
        with tempfile.TemporaryDirectory() as temporary:
            stack = DemoStack(
                Path("compose.yml"),
                Path(temporary),
                SimpleNamespace(opensearch_config=None),
                "http://tempo",
                40,
            )
            with (
                patch.object(
                    stack,
                    "resources",
                    side_effect=subprocess.TimeoutExpired(
                        ["docker", "ps"], 15
                    ),
                ),
                patch.object(stack, "command", return_value="") as command,
                patch("time.sleep"),
            ):
                with self.assertRaises(RuntimeError):
                    stack.cleanup(Path(temporary))
            self.assertTrue(
                any("down" in call.args[0] for call in command.call_args_list),
                "Inventory timeouts skipped every teardown attempt",
            )

    def test_cleanup_error_preserves_original_measurement_error(self):
        with tempfile.TemporaryDirectory() as temporary:
            stack = DemoStack(
                Path("compose.yml"),
                Path(temporary),
                SimpleNamespace(opensearch_config=None),
                "http://tempo",
                40,
            )
            with (
                patch.object(
                    stack,
                    "cleanup",
                    side_effect=[None, RuntimeError("cleanup unavailable")],
                ),
                patch.object(stack, "start"),
                patch.object(stack, "command", return_value=""),
            ):
                with self.assertRaisesRegex(TimeoutError, "required queries"):
                    with stack.run(Path(temporary)):
                        raise TimeoutError(
                            "Daemon did not complete required queries"
                        )

    def test_residual_resources_block_the_next_start(self):
        with tempfile.TemporaryDirectory() as temporary:
            stack = DemoStack(
                Path("compose.yml"),
                Path(temporary),
                SimpleNamespace(opensearch_config=None),
                "http://tempo",
                40,
            )
            with (
                patch.object(
                    stack,
                    "resources",
                    return_value={
                        "containers": ["stuck"],
                        "networks": [],
                        "volumes": [],
                    },
                ),
                patch.object(stack, "command", return_value=""),
                patch.object(stack, "start") as start,
                patch("time.sleep"),
            ):
                with self.assertRaisesRegex(
                    RuntimeError, "next repeat must not start"
                ):
                    with stack.run(Path(temporary)):
                        pass
                start.assert_not_called()


if __name__ == "__main__":
    unittest.main()
