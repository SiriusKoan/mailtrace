import io
import os
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock, call, patch

from demo.docker import bench_tracing as benchmark
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from mailtrace.models import LogEntry
from mailtrace.tracing import otel
from mailtrace.tracing.delay_parser import (
    EXIM_DELAY_STAGES,
    POSTFIX_DELAY_STAGES,
)


class TracingBenchmarkTest(unittest.TestCase):
    def setUp(self) -> None:
        self.message_id = "batch@test.example"
        self.start = datetime(2026, 1, 1, tzinfo=UTC)
        self.delegate = InMemorySpanExporter()
        self.exporter = benchmark._RecordingExporter(self.delegate)
        self.providers = {}
        self.provider_patch = patch.object(otel, "_providers", self.providers)
        self.exporter_patch = patch.object(otel, "_exporter", self.exporter)
        self.provider_patch.start()
        self.exporter_patch.start()
        self.logs = [
            self.log(
                "mailer1",
                "1xTEST-000001-AA",
                0,
                f"<= sender@test.example id={self.message_id} RT=0.05s",
            ),
            self.log(
                "mailer1",
                "1xTEST-000001-AA",
                1,
                '=> user@test.example DT=0.20s C="250 queued as BBB222"',
                queued_as="BBB222",
                relay_host="policy.example.com",
            ),
            self.log("mailer1", "1xTEST-000001-AA", 2, "Completed QT=0.50s"),
            self.log("policy", "BBB222", 3, f"message-id=<{self.message_id}>"),
            self.log(
                "policy",
                "BBB222",
                4,
                "to=<user@test.example>, status=sent, delays=0.1/0.2/0.3/0.4",
            ),
        ]

    def tearDown(self) -> None:
        for provider in self.providers.values():
            provider.shutdown()
        self.exporter_patch.stop()
        self.provider_patch.stop()

    def log(self, host, queue_id, seconds, message, **fields) -> LogEntry:
        return LogEntry(
            datetime=(self.start + timedelta(seconds=seconds)).isoformat(),
            hostname=host,
            service="exim4" if host.startswith("mailer") else "postfix/smtp",
            mail_id=queue_id,
            message=message,
            **fields,
        )

    def run_cycle(self, logs):
        with patch.object(benchmark, "query_all_logs", return_value=logs):
            return benchmark._run_tracing_cycle(
                object(),
                self.start,
                self.start + timedelta(minutes=1),
                self.exporter,
            )

    def test_split_exim_fields_use_production_delivery_spans(self) -> None:
        with patch.object(
            benchmark, "export_traces", wraps=benchmark.export_traces
        ) as production_export:
            result = self.run_cycle(self.logs)

        production_export.assert_called_once()
        self.assertEqual((result.n_expected, result.n_traces), (1, 1))
        self.assertEqual(
            (result.exact_matches, result.malformed_traces), (1, 0)
        )
        self.assertEqual(result.missing_events, 0)
        spans = self.exporter.recording.get_finished_spans()
        self.assertEqual(spans, self.delegate.get_finished_spans())
        by_id = {span.context.span_id: span for span in spans}
        queue_stage = next(span for span in spans if span.name == "queue_time")
        self.assertAlmostEqual(
            queue_stage.attributes["delay.duration_seconds"], 0.25
        )
        self.assertEqual(by_id[queue_stage.parent.span_id].name, "delivery")
        signatures = benchmark._build_generated_signatures(spans)
        self.assertEqual(
            signatures[self.message_id],
            {
                "mailer1": benchmark.HostSignature(tuple(EXIM_DELAY_STAGES)),
                "policy": benchmark.HostSignature(tuple(POSTFIX_DELAY_STAGES)),
            },
        )

    def test_missing_actual_stage_is_malformed_and_missing_root_is_missing(
        self,
    ) -> None:
        self.run_cycle(self.logs)
        expected = benchmark._build_expected_signatures(
            {self.message_id: self.logs}
        )
        spans = self.exporter.recording.get_finished_spans()
        partial = [span for span in spans if span.name != "queue_time"]

        comparison = benchmark._compare_trace_signatures(
            expected, benchmark._build_generated_signatures(partial)
        )
        self.assertEqual(
            (comparison.exact, comparison.malformed, comparison.missing),
            (0, 1, 0),
        )
        comparison = benchmark._compare_trace_signatures(
            expected, benchmark._build_generated_signatures([])
        )
        self.assertEqual(
            (comparison.exact, comparison.malformed, comparison.missing),
            (0, 0, 1),
        )

    def test_repeated_hosts_and_branches_combine_stages_without_inheriting_children(
        self,
    ) -> None:
        hops = [
            ("mx", "AAA111"),
            ("policy", "BBB222"),
            ("mx", "CCC333"),
            ("mailbox", "DDD444"),
        ]
        logs = [
            self.log(host, queue_id, 0, f"message-id=<{self.message_id}>")
            for host, queue_id in hops
        ]
        for source, target in (
            (hops[0], hops[1]),
            (hops[1], hops[2]),
            (hops[0], hops[3]),
        ):
            logs.append(
                self.log(
                    *source,
                    1,
                    f"to=<user@test.example>, delays=0.1/0.2/0.3/0.4, status=sent (250 queued as {target[1]})",
                    queued_as=target[1],
                    relay_host=target[0],
                )
            )
        result = self.run_cycle(logs)

        self.assertEqual(result.exact_matches, 1)
        spans = self.exporter.recording.get_finished_spans()
        signatures = benchmark._build_generated_signatures(spans)
        self.assertEqual(
            signatures[self.message_id]["mx"].stages,
            tuple(POSTFIX_DELAY_STAGES),
        )
        self.assertEqual(signatures[self.message_id]["mailbox"].stages, ())
        self.assertEqual(
            sum(
                span.attributes.get("server.address") == "mx" for span in spans
            ),
            2,
        )

    def test_correctness_checks_follow_all_phase_timers(self) -> None:
        expected_builder = benchmark._build_expected_signatures
        with patch.object(
            benchmark, "perf_counter", side_effect=[0, 1, 2, 5, 6, 10]
        ) as clock:

            def check_expected(groups):
                self.assertEqual(clock.call_count, 6)
                return expected_builder(groups)

            with patch.object(
                benchmark,
                "_build_expected_signatures",
                side_effect=check_expected,
            ):
                result = self.run_cycle(self.logs)

        self.assertEqual(
            (result.query_time, result.spans_time, result.flush_time),
            (1, 3, 4),
        )
        self.assertEqual((result.total_time, result.avg_per_trace), (8, 8))

    def test_repeated_cycles_clear_recorded_spans(self) -> None:
        self.run_cycle(self.logs)
        second_id = "second@test.example"
        logs = [
            replace(
                log, message=log.message.replace(self.message_id, second_id)
            )
            for log in self.logs
        ]
        result = self.run_cycle(logs)
        signatures = benchmark._build_generated_signatures(
            self.exporter.recording.get_finished_spans()
        )

        self.assertEqual(result.exact_matches, 1)
        self.assertEqual(set(signatures), {second_id})

    def test_requested_batch_sizes_keep_all_recorded_trace_structures(
        self,
    ) -> None:
        for size in (10, 50, 100, 1000):
            with self.subTest(size=size):
                logs = []
                for index in range(size):
                    queue_ids = {
                        "1xTEST-000001-AA": f"1xTEST-{index:06d}-AA",
                        "BBB222": f"B{index:06X}",
                    }
                    for log in self.logs:
                        message = log.message.replace(
                            self.message_id, f"batch-{index}@test.example"
                        )
                        for old, new in queue_ids.items():
                            message = message.replace(old, new)
                        logs.append(
                            replace(
                                log,
                                mail_id=queue_ids[log.mail_id],
                                queued_as=queue_ids.get(log.queued_as),
                                message=message,
                            )
                        )
                result = self.run_cycle(logs)

                self.assertEqual(
                    (result.n_expected, result.n_traces), (size, size)
                )
                self.assertEqual(result.exact_matches, size)
                self.assertEqual(
                    (result.malformed_traces, result.missing_events), (0, 0)
                )

    def test_empty_log_window_skips_export(self) -> None:
        with patch.object(benchmark, "export_traces") as export:
            self.assertIsNone(self.run_cycle([]))
        export.assert_not_called()

    def test_cli_runs_production_cycle_with_demo_endpoint(self) -> None:
        output = io.StringIO()
        with (
            patch(
                "sys.argv",
                ["bench_tracing.py", "--skip-send", "--sizes", "10"],
            ),
            patch.object(benchmark, "load_config"),
            patch.object(benchmark, "init_exporter") as initialize,
            patch.object(benchmark, "query_all_logs", return_value=self.logs),
            redirect_stdout(output),
        ):
            self.assertEqual(benchmark.main(), 0)

        initialize.assert_called_once_with("http://localhost:14317")
        self.assertIn("exact=    1", output.getvalue())

    def test_cli_requeries_same_batch_when_indexing_is_delayed(self) -> None:
        complete = benchmark.RunResult(
            0, 1000, 1000, 1000, 0, 0, 1.0, 1, 1, 1, 3, 0.003
        )
        with (
            patch(
                "sys.argv",
                [
                    "bench_tracing.py",
                    "--sizes",
                    "1000",
                    "--wait",
                    "30",
                    "--max-retries",
                    "3",
                ],
            ),
            patch.object(benchmark, "load_config"),
            patch.object(benchmark, "init_exporter"),
            patch.object(benchmark, "send_emails", return_value=True) as send,
            patch.object(benchmark.time, "sleep") as sleep,
            patch.object(benchmark.time, "monotonic", return_value=0),
            patch.object(benchmark, "datetime") as clock,
            patch.object(
                benchmark,
                "_run_tracing_cycle",
                side_effect=[
                    None,
                    None,
                    None,
                    None,
                    complete,
                ],
            ) as cycle,
            redirect_stdout(io.StringIO()),
        ):
            clock.now.side_effect = [
                self.start + timedelta(seconds=seconds)
                for seconds in (0, 0, 1, 2, 3, 4)
            ]
            self.assertEqual(benchmark.main(), 0)

        send.assert_called_once()
        self.assertEqual(sleep.call_args_list, [call(1)] * 4)
        windows = [call.args[1:3] for call in cycle.call_args_list]
        self.assertEqual(len({start for start, _ in windows}), 1)
        self.assertEqual(
            [end for _, end in windows],
            [
                self.start + timedelta(seconds=seconds)
                for seconds in (5, 6, 7, 8, 9)
            ],
        )

    def test_cli_ready_batch_does_not_sleep(self) -> None:
        complete = benchmark.RunResult(
            0, 1000, 1000, 1000, 0, 0, 1.0, 1, 1, 1, 3, 0.003
        )
        with (
            patch("sys.argv", ["bench_tracing.py", "--sizes", "1000"]),
            patch.object(benchmark, "load_config"),
            patch.object(benchmark, "init_exporter"),
            patch.object(benchmark, "send_emails", return_value=True),
            patch.object(benchmark.time, "sleep") as sleep,
            patch.object(
                benchmark, "_run_tracing_cycle", return_value=complete
            ) as cycle,
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(benchmark.main(), 0)

        sleep.assert_not_called()
        cycle.assert_called_once()

    def test_cli_stops_polling_at_wait_deadline(self) -> None:
        output = io.StringIO()
        with (
            patch(
                "sys.argv",
                ["bench_tracing.py", "--sizes", "1000", "--wait", "2"],
            ),
            patch.object(benchmark, "load_config"),
            patch.object(benchmark, "init_exporter"),
            patch.object(benchmark, "send_emails", return_value=True) as send,
            patch.object(benchmark.time, "sleep") as sleep,
            patch.object(
                benchmark.time, "monotonic", side_effect=[0, 0, 1, 2]
            ),
            patch.object(
                benchmark, "_run_tracing_cycle", return_value=None
            ) as cycle,
            redirect_stdout(output),
            patch("sys.stderr", new=io.StringIO()),
        ):
            self.assertEqual(benchmark.main(), 1)

        send.assert_called_once()
        self.assertEqual(cycle.call_count, 3)
        self.assertEqual(sleep.call_count, 2)
        self.assertIn("Timed out", output.getvalue())

    def test_incomplete_indexed_batch_skips_export(self) -> None:
        with (
            patch.object(benchmark, "query_all_logs", return_value=self.logs),
            patch.object(benchmark, "export_traces") as export,
        ):
            result = benchmark._run_tracing_cycle(
                object(),
                self.start,
                self.start + timedelta(minutes=1),
                self.exporter,
                expected_count=2,
            )

        self.assertIsNone(result)
        export.assert_not_called()

    def test_cli_keeps_trace_errors_when_expected_count_is_complete(
        self,
    ) -> None:
        candidate = benchmark.RunResult(
            0, 1000, 999, 998, 1, 1, 0.998, 1, 1, 1, 3, 0.003
        )
        output = io.StringIO()
        with (
            patch("sys.argv", ["bench_tracing.py", "--sizes", "1000"]),
            patch.object(benchmark, "load_config"),
            patch.object(benchmark, "init_exporter"),
            patch.object(benchmark, "send_emails", return_value=True) as send,
            patch.object(benchmark.time, "sleep"),
            patch.object(
                benchmark, "_run_tracing_cycle", return_value=candidate
            ) as cycle,
            redirect_stdout(output),
        ):
            self.assertEqual(benchmark.main(), 0)

        send.assert_called_once()
        cycle.assert_called_once()
        self.assertIn("malformed=    1", output.getvalue())
        self.assertIn("missing=    1", output.getvalue())

    def test_sender_assigns_unique_message_id_prefix_to_each_batch(
        self,
    ) -> None:
        with (
            patch.dict(os.environ, {"MAILTRACE_RUN_PREFIX": "inherited"}),
            patch.object(
                benchmark.subprocess,
                "run",
                return_value=Mock(returncode=0),
            ) as run,
            redirect_stdout(io.StringIO()),
        ):
            self.assertTrue(
                benchmark.send_emails(benchmark.Path("send.sh"), 1000)
            )
            self.assertTrue(
                benchmark.send_emails(benchmark.Path("send.sh"), 1000)
            )

        prefixes = [
            call.kwargs["env"]["MAILTRACE_RUN_PREFIX"]
            for call in run.call_args_list
        ]
        self.assertEqual(len(set(prefixes)), 2)
        self.assertNotIn("inherited", prefixes)


if __name__ == "__main__":
    unittest.main()
