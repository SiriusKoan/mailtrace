import unittest
from unittest.mock import patch

from demo.docker.bench_daemon_correctness import (
    HostSignature,
    build_expected_signatures,
    build_generated_signatures,
    compare_signatures,
    trace_spans,
)
from google.protobuf.json_format import MessageToDict
from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from mailtrace.models import LogEntry
from mailtrace.tracing import otel
from mailtrace.tracing.builder import export_traces
from mailtrace.tracing.delay_parser import (
    EXIM_DELAY_STAGES,
    POSTFIX_DELAY_STAGES,
)


def host_span(span_id, hostname, parent_id=None):
    span = {
        "spanId": span_id,
        "name": hostname,
        "attributes": [
            {"key": "server.address", "value": {"stringValue": hostname}},
            {"key": "message.id", "value": {"stringValue": "message@test"}},
        ],
    }
    if parent_id:
        span["parentSpanId"] = parent_id
    return span


def delay_spans(parent_id, stages, start=0):
    return [
        {
            "spanId": f"{parent_id}-{name}",
            "parentSpanId": parent_id,
            "name": name,
            "startTimeUnixNano": str(start + index),
            "attributes": [
                {"key": "delay.duration_seconds", "value": {"doubleValue": 1}}
            ],
        }
        for index, name in enumerate(stages)
    ]


def generated_signature(spans):
    trace_obj = {"resourceSpans": [{"scopeSpans": [{"spans": spans}]}]}
    return build_generated_signatures([trace_obj], {"message@test"})


class ExpectedSignatureTest(unittest.TestCase):
    def test_baseline_uses_exported_hosts_without_changing_tracing_state(self):
        no_queue_log = LogEntry(
            datetime="2026-10-08T12:00:00+08:00",
            hostname="mail-b",
            service="postfix/cleanup",
            mail_id="",
            message="message-id=<message@test>",
        )
        logs = {
            "message@test": [
                LogEntry(
                    datetime="2026-10-08T12:00:00+08:00",
                    hostname="mail-a",
                    service="postfix/smtp",
                    mail_id="QUEUE-A",
                    message="status=sent, delays=0.1/0.2/0.3/0.4",
                ),
                no_queue_log,
            ],
            "no-queue@test": [no_queue_log],
        }
        previous_exporter = InMemorySpanExporter()
        previous_providers = {"existing": object()}
        with (
            patch.object(otel, "_exporter", previous_exporter),
            patch.object(otel, "_providers", previous_providers),
        ):
            self.assertEqual(
                build_expected_signatures(logs),
                {
                    "message@test": {
                        "mail-a": HostSignature(tuple(POSTFIX_DELAY_STAGES))
                    }
                },
            )
            self.assertIs(otel._exporter, previous_exporter)
            self.assertIs(otel._providers, previous_providers)
            self.assertEqual(previous_exporter.get_finished_spans(), ())

    def test_baseline_restores_tracing_state_when_export_fails(self):
        previous_exporter = InMemorySpanExporter()
        previous_providers = {"existing": object()}
        with (
            patch.object(otel, "_exporter", previous_exporter),
            patch.object(otel, "_providers", previous_providers),
            patch(
                "demo.docker.bench_daemon_correctness.export_traces",
                side_effect=RuntimeError("export failed"),
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "export failed"):
                build_expected_signatures({})
            self.assertIs(otel._exporter, previous_exporter)
            self.assertIs(otel._providers, previous_providers)


class GeneratedSignatureTest(unittest.TestCase):
    def test_actual_exporter_with_delivery_wrapper_matches_complete_logs(self):
        logs = {
            "message@test": [
                LogEntry(
                    datetime="2026-10-08T12:00:00+08:00",
                    hostname="mail-a",
                    service="postfix/smtp",
                    mail_id="QUEUE-A",
                    message=(
                        "to=<recipient@test>, status=sent, "
                        "delays=0.1/0.2/0.3/0.4"
                    ),
                )
            ]
        }
        exporter = InMemorySpanExporter()
        with (
            patch.object(otel, "_exporter", exporter),
            patch.object(otel, "_providers", {}),
        ):
            self.assertEqual(export_traces(logs), 1)
            otel.flush_traces()
            trace_obj = MessageToDict(
                encode_spans(exporter.get_finished_spans())
            )
            for provider in otel._providers.values():
                provider.shutdown()

        self.assertEqual(
            sum(span["name"] == "delivery" for span in trace_spans(trace_obj)),
            1,
        )
        expected = build_expected_signatures(logs)
        generated = build_generated_signatures([trace_obj], set(logs))
        self.assertEqual(generated, expected)
        self.assertEqual(compare_signatures(expected, generated).exact, 1)

    def test_direct_delay_children_remain_supported(self):
        spans = [host_span("host-a", "mail-a")]
        spans.extend(delay_spans("host-a", POSTFIX_DELAY_STAGES))
        self.assertEqual(
            generated_signature(spans)["message@test"]["mail-a"],
            HostSignature(tuple(POSTFIX_DELAY_STAGES)),
        )

    def test_repeated_host_and_delivery_stages_are_combined_once(self):
        spans = [host_span("host-a", "mail-a")]
        for wrapper_id, start in (("first", 0), ("retry", 10)):
            spans.append(
                {
                    "spanId": wrapper_id,
                    "parentSpanId": "host-a",
                    "name": "delivery",
                }
            )
            spans.extend(delay_spans(wrapper_id, POSTFIX_DELAY_STAGES, start))
        # An empty repeated host span must not overwrite existing stages.
        spans.append(host_span("host-a-again", "mail-a"))
        self.assertEqual(
            generated_signature(spans)["message@test"]["mail-a"],
            HostSignature(tuple(POSTFIX_DELAY_STAGES)),
        )

    def test_child_host_stages_do_not_belong_to_parent_host(self):
        spans = [host_span("host-a", "mail-a")]
        spans.append(
            {
                "spanId": "delivery-a",
                "parentSpanId": "host-a",
                "name": "delivery",
            }
        )
        spans.extend(delay_spans("delivery-a", POSTFIX_DELAY_STAGES))
        spans.append(host_span("host-b", "mail-b", "delivery-a-transmission"))
        spans.append(
            {
                "spanId": "delivery-b",
                "parentSpanId": "host-b",
                "name": "delivery",
            }
        )
        spans.extend(delay_spans("delivery-b", EXIM_DELAY_STAGES, 10))
        self.assertEqual(
            generated_signature(spans)["message@test"],
            {
                "mail-a": HostSignature(tuple(POSTFIX_DELAY_STAGES)),
                "mail-b": HostSignature(tuple(EXIM_DELAY_STAGES)),
            },
        )

    def test_missing_stage_is_still_malformed(self):
        spans = [host_span("host-a", "mail-a")]
        spans.append(
            {
                "spanId": "delivery-a",
                "parentSpanId": "host-a",
                "name": "delivery",
            }
        )
        spans.extend(delay_spans("delivery-a", POSTFIX_DELAY_STAGES[:-1]))
        expected = {
            "message@test": {
                "mail-a": HostSignature(tuple(POSTFIX_DELAY_STAGES))
            }
        }
        result = compare_signatures(expected, generated_signature(spans))
        self.assertEqual(
            (result.exact, result.malformed, result.missing), (0, 1, 0)
        )


if __name__ == "__main__":
    unittest.main()
