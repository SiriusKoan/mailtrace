import csv
import io
import json
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

from demo.docker import (
    bench_resource_rates,
    bench_resources,
    send_bulk_emails,
)


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.interrupt_at = None

    def monotonic(self) -> float:
        return self.now

    def wall_time(self) -> float:
        return 1_700_000_000 + self.now

    def sleep(self, duration: float) -> None:
        if (
            self.interrupt_at is not None
            and self.now + duration >= self.interrupt_at
        ):
            self.now = self.interrupt_at
            raise KeyboardInterrupt
        self.now += duration


class FakeReader:
    def __init__(
        self,
        clock: FakeClock,
        samples: list[bench_resources.ResourceSample],
        *,
        stop_at: float | None = None,
        fail_at_read: int | None = None,
    ) -> None:
        self.clock = clock
        self.samples = samples
        self.stop_at = stop_at
        self.fail_at_read = fail_at_read
        self.read_count = 0

    def is_active(self) -> bool:
        return self.stop_at is None or self.clock.now < self.stop_at

    def read_sample(self) -> bench_resources.ResourceSample:
        if self.read_count == self.fail_at_read:
            raise OSError("sample failed")
        sample = self.samples[min(self.read_count, len(self.samples) - 1)]
        self.read_count += 1
        return sample


class ParsingTest(unittest.TestCase):
    def test_parses_cgroup_v2_path(self) -> None:
        text = "0::/system.slice/docker-container.scope\n"
        self.assertEqual(
            bench_resources.parse_cgroup_v2_path(text),
            "/system.slice/docker-container.scope",
        )

    def test_rejects_cgroup_v1(self) -> None:
        with self.assertRaisesRegex(ValueError, "cgroup v2"):
            bench_resources.parse_cgroup_v2_path(
                "2:cpu,cpuacct:/docker/container\n"
            )

    def test_resolves_path_beneath_cgroup_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "cgroup.controllers").write_text("cpu memory\n")
            proc = root / "proc"
            (proc / "123").mkdir(parents=True)
            (proc / "123" / "cgroup").write_text("0::/container\n")

            resolved = bench_resources.resolve_cgroup_path(123, root, proc)

            self.assertEqual(resolved, root / "container")

    def test_rejects_path_outside_cgroup_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "cgroup.controllers").write_text("cpu memory\n")
            proc = root / "proc"
            (proc / "123").mkdir(parents=True)
            (proc / "123" / "cgroup").write_text("0::/../../outside\n")

            with self.assertRaisesRegex(RuntimeError, "escapes"):
                bench_resources.resolve_cgroup_path(123, root, proc)

    def test_parses_cpu_and_memory_values(self) -> None:
        cpu = "usage_usec 12345\nuser_usec 10000\nsystem_usec 2345\n"

        self.assertEqual(bench_resources.parse_cpu_usage_usec(cpu), 12345)
        self.assertEqual(bench_resources.parse_memory_current("4096\n"), 4096)
        self.assertTrue(
            bench_resources.parse_cgroup_populated("populated 1\n")
        )

    def test_rejects_invalid_resource_values(self) -> None:
        with self.assertRaises(ValueError):
            bench_resources.parse_cpu_usage_usec("usage_usec invalid\n")
        with self.assertRaises(ValueError):
            bench_resources.parse_memory_current("-1\n")

    def test_calculates_single_core_cpu_percent(self) -> None:
        self.assertEqual(bench_resources.cpu_percent(2_500_000, 1.0), 250.0)


class CsvTest(unittest.TestCase):
    def test_creates_header_and_appends_without_duplicate_header(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "resources.csv"
            first, writer = bench_resources.open_output_csv(path)
            writer.writerow((1, 10, 1.0, 100))
            first.close()

            second, writer = bench_resources.open_output_csv(path)
            writer.writerow((2, 20, 2.0, 200))
            second.close()

            with path.open(newline="") as output:
                rows = list(csv.reader(output))
            self.assertEqual(rows[0], list(bench_resources.CSV_FIELDS))
            self.assertEqual(len(rows), 3)

    def test_rejects_mismatched_existing_header_without_writing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "resources.csv"
            original = "wrong,header\n"
            path.write_text(original)

            with self.assertRaisesRegex(ValueError, "CSV header"):
                bench_resources.open_output_csv(path)

            self.assertEqual(path.read_text(), original)


class CollectionTest(unittest.TestCase):
    def _collect(
        self,
        clock: FakeClock,
        reader: FakeReader,
    ) -> tuple[dict[str, object], str | None, list[list[str]]]:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "resources.csv"
            summary, error = bench_resources.collect_resources(
                "container",
                path,
                reader,
                wall_clock=clock.wall_time,
                monotonic=clock.monotonic,
                sleeper=clock.sleep,
                poll_interval=0.1,
            )
            with path.open(newline="") as output:
                rows = list(csv.reader(output))
        return summary, error, rows

    def test_full_sample_is_csv_and_partial_tail_is_summary_only(self) -> None:
        clock = FakeClock()
        clock.interrupt_at = 1.4
        reader = FakeReader(
            clock,
            [
                bench_resources.ResourceSample(100, 1000),
                bench_resources.ResourceSample(600, 2000),
                bench_resources.ResourceSample(800, 3000),
            ],
        )

        summary, error, rows = self._collect(clock, reader)

        self.assertIsNone(error)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1][1], "500")
        self.assertEqual(summary["sample_count"], 1)
        self.assertEqual(summary["cpu_usage_usec_delta"], 700)
        self.assertAlmostEqual(summary["cpu_percent_average"], 0.05)
        self.assertAlmostEqual(summary["cpu_percent_max"], 0.05)
        self.assertEqual(summary["memory_current_bytes_start"], 1000)
        self.assertEqual(summary["memory_current_bytes_end"], 3000)
        self.assertEqual(summary["memory_current_bytes_average"], 2000)
        self.assertEqual(summary["memory_current_bytes_min"], 1000)
        self.assertEqual(summary["memory_current_bytes_max"], 3000)
        self.assertEqual(summary["stop_reason"], "signal")

    def test_ctrl_c_stops_with_signal_reason(self) -> None:
        clock = FakeClock()
        clock.interrupt_at = 0.25
        reader = FakeReader(
            clock,
            [
                bench_resources.ResourceSample(100, 1000),
                bench_resources.ResourceSample(200, 1200),
            ],
        )

        summary, _, rows = self._collect(clock, reader)

        self.assertEqual(summary["stop_reason"], "signal")
        self.assertEqual(summary["sample_count"], 0)
        self.assertEqual(len(rows), 1)

    def test_container_stop_preserves_partial_tail(self) -> None:
        clock = FakeClock()
        reader = FakeReader(
            clock,
            [
                bench_resources.ResourceSample(100, 1000),
                bench_resources.ResourceSample(250, 900),
            ],
            stop_at=0.4,
        )

        summary, _, rows = self._collect(clock, reader)

        self.assertEqual(summary["stop_reason"], "container_stopped")
        self.assertEqual(summary["cpu_usage_usec_delta"], 150)
        self.assertEqual(summary["memory_current_bytes_end"], 900)
        self.assertEqual(len(rows), 1)

    def test_read_failure_returns_partial_summary(self) -> None:
        clock = FakeClock()
        reader = FakeReader(
            clock,
            [bench_resources.ResourceSample(100, 1000)],
            fail_at_read=1,
        )

        summary, error, rows = self._collect(clock, reader)

        self.assertEqual(error, "sample failed")
        self.assertEqual(summary["stop_reason"], "read_error")
        self.assertEqual(summary["cpu_usage_usec_delta"], 0)
        self.assertEqual(summary["memory_current_bytes_end"], 1000)
        self.assertEqual(len(rows), 1)

    def test_final_cpu_counter_rollback_is_read_error(self) -> None:
        clock = FakeClock()
        clock.interrupt_at = 0.25
        reader = FakeReader(
            clock,
            [
                bench_resources.ResourceSample(100, 1000),
                bench_resources.ResourceSample(99, 1200),
            ],
        )

        summary, error, _ = self._collect(clock, reader)

        self.assertEqual(error, "CPU usage counter moved backwards")
        self.assertEqual(summary["stop_reason"], "read_error")
        self.assertEqual(summary["cpu_usage_usec_delta"], 0)

    def test_summary_has_fixed_fields(self) -> None:
        summary = bench_resources.empty_error_summary("container", 10.0)
        self.assertEqual(tuple(summary), bench_resources.SUMMARY_FIELDS)

    def test_run_writes_only_json_to_stdout(self) -> None:
        stdout = io.StringIO()
        stderr = io.StringIO()

        with patch.object(
            bench_resources,
            "inspect_container_pid",
            side_effect=RuntimeError("inspect failed"),
        ):
            with redirect_stdout(stdout), redirect_stderr(stderr):
                status = bench_resources.run("container", Path("unused.csv"))

        self.assertEqual(status, 1)
        self.assertEqual(
            json.loads(stdout.getvalue())["stop_reason"], "read_error"
        )
        self.assertEqual(stderr.getvalue(), "inspect failed\n")


class RateBenchmarkTest(unittest.TestCase):
    def test_no_arguments_selects_managed_environment_defaults(self) -> None:
        args = bench_resource_rates.parse_args([])

        self.assertEqual(args.rates, [10, 20, 50, 100, 200, 500])
        self.assertEqual(args.duration, 600.0)
        self.assertEqual(args.output_dir.parent.name, "results")
        self.assertRegex(args.output_dir.name, r"resource-\d{8}-\d{6}")

    def test_writes_cpu_usage_timestamp_svg(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            csv_path = Path(directory) / "resources.csv"
            output_path = Path(directory) / "cpu.svg"
            with csv_path.open("w", newline="", encoding="utf-8") as csv_file:
                writer = csv.DictWriter(
                    csv_file,
                    fieldnames=bench_resources.CSV_FIELDS,
                )
                writer.writeheader()
                writer.writerow(
                    {
                        "timestamp": 1_700_000_000,
                        "cpu_usage_usec_delta": 100_000,
                        "cpu_percent": 10.0,
                        "memory_current_bytes": 1000,
                    }
                )
                writer.writerow(
                    {
                        "timestamp": 1_700_000_001,
                        "cpu_usage_usec_delta": 250_000,
                        "cpu_percent": 25.0,
                        "memory_current_bytes": 2000,
                    }
                )

            bench_resource_rates.write_cpu_usage_svg(csv_path, output_path, 5)

            svg = output_path.read_text(encoding="utf-8")
            self.assertIn("CPU Usage at 5 emails/s", svg)
            self.assertIn("Timestamp (UTC)", svg)
            self.assertIn("CPU usage (%)", svg)
            self.assertIn("<polyline", svg)

    def test_default_rates_are_email_rates(self) -> None:
        args = bench_resource_rates.parse_args(["--output-dir", "results"])

        self.assertEqual(args.rates, [10, 20, 50, 100, 200, 500])
        self.assertEqual(args.duration, 600.0)

    def test_checks_exim_queue_count(self) -> None:
        with patch.object(
            bench_resource_rates.subprocess,
            "run",
            return_value=subprocess.CompletedProcess(
                args=[], returncode=0, stdout="0\n", stderr=""
            ),
        ):
            self.assertTrue(
                bench_resource_rates.exim_queue_is_empty("mailer-container")
            )

    def test_managed_experiment_cleans_stale_and_created_environment(
        self,
    ) -> None:
        output_dir = Path("results/test")
        container_ids = {
            "mailtrace": "trace-id",
            "mx": "mx-id",
            "mailpolicy": "policy-id",
            "mailbox": "mailbox-id",
            "mailbox2": "mailbox2-id",
            "mailer": "mailer-id",
        }
        rate_report = {
            "container": "trace-id",
            "rates": [5],
            "duration_seconds": 1.0,
            "trace_container": "trace-id",
            "runs": [],
        }
        with patch.object(
            bench_resource_rates, "cleanup_environment", return_value=None
        ) as cleanup, patch.object(
            bench_resource_rates, "start_environment"
        ) as start, patch.object(
            bench_resource_rates,
            "compose_container_id",
            side_effect=lambda service: container_ids[service],
        ), patch.object(
            bench_resource_rates,
            "run",
            return_value=(rate_report, 0),
        ) as run:
            report, status = bench_resource_rates.run_managed_experiment(
                output_dir, [5], 1.0, 0.1
            )

        self.assertEqual(status, 0)
        self.assertEqual(report["output_dir"], str(output_dir))
        self.assertEqual(cleanup.call_count, 2)
        start.assert_called_once_with()
        run.assert_called_once_with(
            "trace-id",
            output_dir,
            [5],
            1.0,
            trace_container="trace-id",
            queue_containers=[
                "mx-id",
                "policy-id",
                "mailbox-id",
                "mailbox2-id",
            ],
            exim_queue_container="mailer-id",
            trace_poll_interval=0.1,
        )

    def test_start_failure_is_reported_and_still_cleans_up(self) -> None:
        output_dir = Path("results/test")
        with patch.object(
            bench_resource_rates, "cleanup_environment", return_value=None
        ) as cleanup, patch.object(
            bench_resource_rates,
            "start_environment",
            side_effect=RuntimeError("compose up failed"),
        ), patch.object(
            bench_resource_rates, "capture_compose_logs"
        ) as capture:
            report, status = bench_resource_rates.run_managed_experiment(
                output_dir, [5], 1.0, 0.1
            )

        self.assertEqual(status, 1)
        self.assertEqual(report["runs"], [])
        self.assertEqual(report["error"], "compose up failed")
        self.assertEqual(cleanup.call_count, 2)
        capture.assert_called_once_with(output_dir)

    def test_interrupt_is_reported_and_still_cleans_up(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.object(
            bench_resource_rates, "cleanup_environment", return_value=None
        ) as cleanup, patch.object(
            bench_resource_rates,
            "start_environment",
            side_effect=KeyboardInterrupt,
        ), patch.object(
            bench_resource_rates, "capture_compose_logs"
        ):
            report, status = bench_resource_rates.run_managed_experiment(
                Path(directory), [5], 1.0, 0.1
            )

        self.assertEqual(status, 130)
        self.assertEqual(report["error"], "experiment interrupted")
        self.assertEqual(cleanup.call_count, 2)

    def test_sums_generated_trace_counts(self) -> None:
        log_output = "\n".join(
            [
                "INFO -   Traces generated 12",
                "INFO - unrelated",
                "INFO -   Traces generated 8",
            ]
        )

        self.assertEqual(
            bench_resource_rates.count_generated_traces(log_output), 20
        )

    def test_waits_for_exact_trace_count_and_empty_queues(self) -> None:
        trace_follower = Mock()
        trace_follower.trace_count.side_effect = [10, 20]
        with patch.object(
            bench_resource_rates,
            "nonempty_postfix_queues",
            return_value=[],
        ) as queue_check, patch.object(
            bench_resource_rates.time, "sleep"
        ) as sleep:
            count = bench_resource_rates.wait_for_trace_completion(
                trace_follower, ["mailqueue"], 20, 120.0
            )

        self.assertEqual(count, 20)
        queue_check.assert_called_once_with(["mailqueue"])
        sleep.assert_called_once_with(120.0)

    def test_waits_for_exim_queue_to_empty(self) -> None:
        trace_follower = Mock()
        trace_follower.trace_count.return_value = 20
        with patch.object(
            bench_resource_rates,
            "nonempty_postfix_queues",
            side_effect=[[], []],
        ), patch.object(
            bench_resource_rates,
            "exim_queue_is_empty",
            side_effect=[False, True],
        ) as exim_check, patch.object(
            bench_resource_rates.time, "sleep"
        ):
            count = bench_resource_rates.wait_for_trace_completion(
                trace_follower,
                ["mailqueue"],
                20,
                0.1,
                exim_queue_container="mailer",
            )

        self.assertEqual(count, 20)
        self.assertEqual(exim_check.call_count, 2)

    def test_rejects_trace_count_above_submissions(self) -> None:
        trace_follower = Mock()
        trace_follower.trace_count.return_value = 21
        with self.assertRaisesRegex(RuntimeError, "exceeded"):
            bench_resource_rates.wait_for_trace_completion(
                trace_follower, [], 20, 120.0
            )

    def test_retries_failed_queue_check_after_trace_parity(self) -> None:
        trace_follower = Mock()
        trace_follower.trace_count.return_value = 20
        with patch.object(
            bench_resource_rates,
            "nonempty_postfix_queues",
            side_effect=[subprocess.TimeoutExpired("docker", 300), []],
        ), patch.object(bench_resource_rates.time, "sleep") as sleep:
            count = bench_resource_rates.wait_for_trace_completion(
                trace_follower, ["mailqueue"], 20, 120.0
            )

        self.assertEqual(count, 20)
        sleep.assert_called_once_with(120.0)

    def test_runs_every_rate_and_returns_one_report(self) -> None:
        rates = [10, 20]
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(
                bench_resource_rates,
                "run_rate",
                side_effect=[
                    ({"emails_per_second": 10}, True),
                    ({"emails_per_second": 20}, True),
                ],
            ) as run_rate:
                result, status = bench_resource_rates.run(
                    "mailtrace", Path(directory) / "results", rates, 3.0
                )

        self.assertEqual(status, 0)
        self.assertEqual(
            [call.args[2] for call in run_rate.call_args_list], rates
        )
        self.assertEqual(result["rates"], rates)
        self.assertEqual(len(result["runs"]), 2)

    def test_cleanup_failure_changes_success_to_failure(self) -> None:
        rate_report = {
            "container": "trace-id",
            "rates": [5],
            "duration_seconds": 1.0,
            "trace_container": "trace-id",
            "runs": [],
        }
        with tempfile.TemporaryDirectory() as directory, patch.object(
            bench_resource_rates,
            "cleanup_environment",
            side_effect=[None, RuntimeError("cleanup failed")],
        ), patch.object(
            bench_resource_rates, "start_environment"
        ), patch.object(
            bench_resource_rates,
            "compose_container_id",
            return_value="trace-id",
        ), patch.object(
            bench_resource_rates, "run", return_value=(rate_report, 0)
        ), patch.object(
            bench_resource_rates, "capture_compose_logs"
        ):
            report, status = bench_resource_rates.run_managed_experiment(
                Path(directory), [5], 1.0, 0.1
            )

        self.assertEqual(status, 1)
        self.assertEqual(report["cleanup_error"], "cleanup failed")

    def test_sender_counts_only_successful_deliveries(self) -> None:
        with patch.object(
            send_bulk_emails, "send_message", return_value=False
        ):
            succeeded, failed, elapsed = send_bulk_emails.send_emails(20, 0.1)

        self.assertEqual(succeeded, 0)
        self.assertEqual(failed, 2)
        self.assertGreater(elapsed, 0)

    def test_sender_builds_ports_for_isolated_stack(self) -> None:
        configs = send_bulk_emails.build_configs(11025, 21025)

        self.assertEqual(
            [config["port"] for config in configs], [11025, 21025]
        )


if __name__ == "__main__":
    unittest.main()
