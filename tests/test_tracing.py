import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from opentelemetry.context import Context
from opentelemetry.sdk.trace import Span as SDKSpan
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from opentelemetry.trace import StatusCode

from mailtrace.config import OpenSearchMappingConfig
from mailtrace.events import classify_log_entry
from mailtrace.models import (
    DeliveryStatus,
    EventType,
    LogEntry,
    SmtpStatusClass,
)
from mailtrace.parser import OpensearchParser, SyslogParser, extract_next_mail_id
from mailtrace.tracing import EmailTracesGenerator, otel, query
from mailtrace.tracing.delay_parser import DelayInfo, detect_mta_from_entries
from mailtrace.tracing.lifecycle import PendingTrace, should_export_trace
from mailtrace.tracing.otel import dt_to_ns


class EmailTracesGeneratorTest(unittest.TestCase):
    def setUp(self) -> None:
        otel._providers.clear()

    def tearDown(self) -> None:
        for provider in otel._providers.values():
            provider.shutdown()
        otel._providers.clear()

    def test_postfix_host_span_contains_minimum_duration_stage(self) -> None:
        completion_time = datetime.fromisoformat(
            "2026-01-08T22:55:08.952580+08:00"
        )
        total_delay = 0.100002
        expected_start = completion_time - timedelta(seconds=total_delay)
        logs = [
            LogEntry(
                datetime="2026-01-08T22:55:08.866887+08:00",
                hostname="mailpolicy",
                service="postfix/cleanup",
                mail_id="D375CC4B895",
                message="message-id=<message@example.com>",
            ),
            LogEntry(
                datetime=completion_time.isoformat(),
                hostname="mailpolicy",
                service="postfix/smtp",
                mail_id="D375CC4B895",
                message=("status=sent, delay=0.1, " "delays=0.02/0/0.06/0.02"),
            ),
        ]
        generator = object.__new__(EmailTracesGenerator)
        exporter = InMemorySpanExporter()

        with patch.object(otel, "_exporter", exporter):
            generator._export_traces({"message@example.com": logs})
            otel.flush_traces()

        finished = exporter.get_finished_spans()
        host = next(
            span
            for span in finished
            if span.attributes
            and span.attributes.get("email.queue_id") == "D375CC4B895"
        )
        delivery = next(span for span in finished if span.name == "delivery")
        stages = [
            span
            for span in finished
            if span.parent and span.parent.span_id == delivery.context.span_id
        ]

        self.assertEqual(host.start_time, dt_to_ns(expected_start))
        self.assertEqual(host.end_time, dt_to_ns(completion_time))
        self.assertEqual(
            {span.name for span in stages},
            {"before_qmgr", "in_qmgr", "conn_setup", "transmission"},
        )
        self.assertTrue(
            all(
                delivery.start_time <= stage.start_time
                and delivery.end_time >= stage.end_time
                for stage in stages
            )
        )

    def test_groups_same_host_queue_ids_as_separate_hops(self) -> None:
        logs = [
            LogEntry(
                datetime="2026-07-30T15:18:57+08:00",
                hostname="maildirect10",
                service="postfix/lmtp",
                mail_id="E2F1",
                message="status=sent, delay=0.17, delays=0.01/0/0.01/0.15",
            ),
            LogEntry(
                datetime="2026-07-30T15:19:01+08:00",
                hostname="maildirect10",
                service="postfix/smtp",
                mail_id="E4F2",
                message="status=sent, delay=4.4, delays=0.1/0/0.1/4.2",
            ),
        ]

        grouped = query.group_logs_by_hops(logs)

        self.assertEqual(
            set(grouped),
            {("maildirect10", "E2F1"), ("maildirect10", "E4F2")},
        )

    def test_message_grouping_backfills_logs_seen_before_message_id(self) -> None:
        message_id = "local-forward@example.com"
        timestamp = "2026-08-01T03:26:09+00:00"
        logs = [
            LogEntry(
                datetime=timestamp,
                hostname="maildirect9",
                service="postfix/qmgr",
                mail_id="E6FB69FB9E",
                message="from=<sender@example.com>, queue active",
            ),
            LogEntry(
                datetime=timestamp,
                hostname="maildirect9",
                service="postfix/local",
                mail_id="E6FB69FB9E",
                message=(
                    "to=<recipient@example.com>, delay=0.07, "
                    "delays=0.06/0/0/0.01, "
                    "status=sent (forwarded as EA8449FBBF)"
                ),
                queued_as="EA8449FBBF",
            ),
            LogEntry(
                datetime=timestamp,
                hostname="maildirect9",
                service="postfix/cleanup",
                mail_id="E6FB69FB9E",
                message=f"message-id=<{message_id}>",
            ),
        ]

        grouped = query.group_logs_by_message_id(logs)

        self.assertEqual(grouped[message_id], logs)

    def test_message_grouping_reuses_queue_mapping_across_batches(self) -> None:
        message_id = "late-delivery@example.com"
        queue_id = "MAILER3Q"
        queue_mapping: dict[tuple[str, str], str] = {}
        first_batch = [
            LogEntry(
                datetime="2026-08-01T03:26:09+00:00",
                hostname="mailer3",
                service="postfix/cleanup",
                mail_id=queue_id,
                message=f"message-id=<{message_id}>",
            )
        ]
        later_batch = [
            LogEntry(
                datetime="2026-08-01T03:26:49+00:00",
                hostname="mailer3",
                service="postfix/smtp",
                mail_id=queue_id,
                message=(
                    "to=<recipient@example.com>, status=sent, "
                    "delays=4.4/0/0.03/36"
                ),
            )
        ]

        query.group_logs_by_message_id(first_batch, queue_mapping)
        grouped = query.group_logs_by_message_id(later_batch, queue_mapping)

        self.assertEqual(grouped[message_id], later_batch)

    def test_accumulator_merges_late_queue_only_log(self) -> None:
        message_id = "late-accumulation@example.com"
        queue_id = "MAILER3Q"
        generator = object.__new__(EmailTracesGenerator)
        generator._pending = {}
        generator._queue_id_to_message_id = {}
        generator._current_round = 1
        first_batch = [
            LogEntry(
                datetime="2026-08-01T03:26:09+00:00",
                hostname="mailer3",
                service="postfix/cleanup",
                mail_id=queue_id,
                message=f"message-id=<{message_id}>",
            )
        ]
        later_batch = [
            LogEntry(
                datetime="2026-08-01T03:26:49+00:00",
                hostname="mailer3",
                service="postfix/smtp",
                mail_id=queue_id,
                message="to=<recipient@example.com>, delays=4.4/0/0.03/36",
            )
        ]

        generator._accumulate_logs(
            query.group_logs_by_message_id(
                first_batch,
                generator._queue_id_to_message_id,
            )
        )
        generator._current_round = 2
        generator._accumulate_logs(
            query.group_logs_by_message_id(
                later_batch,
                generator._queue_id_to_message_id,
            )
        )

        pending = generator._pending[message_id]
        self.assertEqual(pending.logs, first_batch + later_batch)
        self.assertEqual(pending.last_seen_round, 2)

    def test_host_hops_are_sequential_siblings_with_causal_links(self) -> None:
        logs = [
            LogEntry(
                datetime="2026-07-30T16:10:00+00:00",
                hostname="maildirect10",
                service="postfix/cleanup",
                mail_id="LOCAL1",
                message="message-id=<chain@example.com>",
            ),
            LogEntry(
                datetime="2026-07-30T16:10:00.140000+00:00",
                hostname="maildirect10",
                service="postfix/local",
                mail_id="LOCAL1",
                message=(
                    "to=<first@example.com>, status=sent "
                    "(forwarded as LOCAL2), delay=0.14, "
                    "delays=0.05/0/0/0.09"
                ),
                queued_as="LOCAL2",
            ),
            LogEntry(
                datetime="2026-07-30T16:10:00.140000+00:00",
                hostname="maildirect10",
                service="postfix/cleanup",
                mail_id="LOCAL2",
                message="message-id=<chain@example.com>",
            ),
            LogEntry(
                datetime="2026-07-30T16:10:04.310000+00:00",
                hostname="maildirect10",
                service="postfix/smtp",
                mail_id="LOCAL2",
                message=(
                    "to=<second@example.com>, status=sent, delay=4.31, "
                    "delays=0.08/0/0.03/4.2"
                ),
                queued_as="REMOTE1",
                relay_host="mailer4",
                relay_ip="192.0.2.4",
                relay_port=25,
                smtp_code=250,
            ),
            LogEntry(
                datetime="2026-07-30T16:10:04.310000+00:00",
                hostname="mailer4",
                service="postfix/cleanup",
                mail_id="REMOTE1",
                message="message-id=<chain@example.com>",
            ),
            LogEntry(
                datetime="2026-07-30T16:10:10.430000+00:00",
                hostname="mailer4",
                service="postfix/smtp",
                mail_id="REMOTE1",
                message=(
                    "to=<second@example.com>, status=sent, delay=6.12, "
                    "delays=4.2/0/0.82/1.1"
                ),
            ),
        ]
        exporter = InMemorySpanExporter()
        generator = object.__new__(EmailTracesGenerator)

        with patch.object(otel, "_exporter", exporter):
            generator._export_traces({"chain@example.com": logs})
            otel.flush_traces()

        spans = {
            span.attributes.get("email.queue_id"): span
            for span in exporter.get_finished_spans()
            if span.attributes and "email.queue_id" in span.attributes
        }
        first = spans["LOCAL1"]
        second = spans["LOCAL2"]
        remote = spans["REMOTE1"]
        root = next(
            span
            for span in exporter.get_finished_spans()
            if span.name == "email.delivery"
        )

        self.assertEqual(first.parent.span_id, root.context.span_id)
        self.assertEqual(second.parent.span_id, root.context.span_id)
        self.assertEqual(remote.parent.span_id, root.context.span_id)
        self.assertEqual(second.links[0].context.span_id, first.context.span_id)
        self.assertEqual(remote.links[0].context.span_id, second.context.span_id)
        self.assertLess(first.start_time, second.start_time)
        self.assertLess(second.start_time, remote.start_time)
        self.assertLess(first.end_time, second.end_time)
        self.assertLess(second.end_time, remote.end_time)
        self.assertEqual(first.attributes["email.transport"], "postfix/local")
        self.assertEqual(first.attributes["email.next_queue_id"], "LOCAL2")
        self.assertEqual(second.attributes["email.transport"], "postfix/smtp")
        self.assertEqual(second.attributes["email.next_queue_id"], "REMOTE1")
        self.assertEqual(second.attributes["email.relay_host"], "mailer4")
        self.assertEqual(second.attributes["email.relay_ip"], "192.0.2.4")
        self.assertEqual(second.attributes["email.relay_port"], 25)
        self.assertEqual(second.attributes["smtp.response_code"], 250)

        finished = exporter.get_finished_spans()

        def stage_for(host_span, stage_name):
            delivery_ids = {
                span.context.span_id
                for span in finished
                if span.name == "delivery"
                and span.parent
                and span.parent.span_id == host_span.context.span_id
            }
            return next(
                span
                for span in finished
                if span.name == stage_name
                and span.parent
                and span.parent.span_id in delivery_ids
            )

        transmission = stage_for(second, "transmission")
        before_qmgr = stage_for(remote, "before_qmgr")
        self.assertEqual(transmission.start_time, before_qmgr.start_time)
        self.assertEqual(transmission.end_time, before_qmgr.end_time)

    def test_multiple_deliveries_have_recipient_branches(self) -> None:
        logs = [
            LogEntry(
                datetime="2026-07-30T16:20:00+00:00",
                hostname="source",
                service="postfix/cleanup",
                mail_id="SOURCE1",
                message="message-id=<recipients@example.com>",
            ),
            LogEntry(
                datetime="2026-07-30T16:20:01+00:00",
                hostname="source",
                service="postfix/smtp",
                mail_id="SOURCE1",
                message=(
                    "to=<first@example.com>, status=sent, delay=1, "
                    "delays=0.1/0.1/0.1/0.7"
                ),
            ),
            LogEntry(
                datetime="2026-07-30T16:20:02+00:00",
                hostname="source",
                service="postfix/smtp",
                mail_id="SOURCE1",
                message=(
                    "to=<second@example.com>, status=sent, delay=2, "
                    "delays=0.2/0.2/0.2/1.4"
                ),
            ),
        ]
        logs = [classify_log_entry(log) for log in logs]
        exporter = InMemorySpanExporter()
        generator = object.__new__(EmailTracesGenerator)

        with patch.object(otel, "_exporter", exporter):
            generator._export_traces({"recipients@example.com": logs})
            otel.flush_traces()

        finished = exporter.get_finished_spans()
        root = next(span for span in finished if span.name == "email.delivery")
        host = next(
            span
            for span in finished
            if span.attributes
            and span.attributes.get("email.queue_id") == "SOURCE1"
        )
        deliveries = {
            span.attributes.get("email.recipient"): span
            for span in finished
            if span.name == "delivery" and span.attributes
        }

        self.assertEqual(
            set(deliveries), {"first@example.com", "second@example.com"}
        )
        self.assertEqual(root.status.status_code, StatusCode.UNSET)
        self.assertNotIn("mail.delivery_status", root.attributes)
        for delivery in deliveries.values():
            self.assertEqual(delivery.parent.span_id, host.context.span_id)
            stages = [
                span
                for span in finished
                if span.parent
                and span.parent.span_id == delivery.context.span_id
            ]
            self.assertEqual(len(stages), 4)
            self.assertTrue(
                all(
                    delivery.start_time <= stage.start_time
                    and delivery.end_time >= stage.end_time
                    for stage in stages
                )
            )

    def test_temporary_failure_marks_only_delivery_span(self) -> None:
        logs = [
            classify_log_entry(
                LogEntry(
                    datetime="2026-08-02T00:04:28+00:00",
                    hostname="mail.example.com",
                    service="postfix/cleanup",
                    mail_id="TEMP1",
                    message="message-id=<temporary@example.com>",
                )
            ),
            classify_log_entry(
                LogEntry(
                    datetime="2026-08-02T00:04:29+00:00",
                    hostname="mail.example.com",
                    service="postfix/smtp",
                    mail_id="TEMP1",
                    message=(
                        "to=<user@example.com>, delay=1, "
                        "delays=0.1/0.1/0.1/0.7, dsn=4.7.1, "
                        "status=deferred (451 4.7.1 Try again later)"
                    ),
                )
            ),
        ]
        exporter = InMemorySpanExporter()
        generator = object.__new__(EmailTracesGenerator)

        with patch.object(otel, "_exporter", exporter):
            generator._export_traces({"temporary@example.com": logs})
            otel.flush_traces()

        finished = exporter.get_finished_spans()
        root = next(span for span in finished if span.name == "email.delivery")
        host = next(
            span for span in finished if span.name == "mail.example.com"
        )
        delivery = next(span for span in finished if span.name == "delivery")
        delay_spans = [
            span
            for span in finished
            if span.parent and span.parent.span_id == delivery.context.span_id
        ]

        self.assertEqual(delivery.status.status_code, StatusCode.ERROR)
        self.assertEqual(
            delivery.attributes["error.type"], "temporary_failure"
        )
        self.assertEqual(
            delivery.attributes["mail.delivery_status"], "deferred"
        )
        self.assertEqual(delivery.attributes["smtp.response_code"], 451)
        self.assertEqual(
            delivery.attributes["smtp.enhanced_status_code"], "4.7.1"
        )
        self.assertEqual(host.status.status_code, StatusCode.UNSET)
        self.assertEqual(root.status.status_code, StatusCode.UNSET)
        self.assertTrue(
            all(
                span.status.status_code is StatusCode.UNSET
                for span in delay_spans
            )
        )

    def test_permanent_milter_reject_marks_host_and_root(self) -> None:
        logs = [
            classify_log_entry(
                LogEntry(
                    datetime="2026-08-02T00:04:28+00:00",
                    hostname="mail.example.com",
                    service="postfix/cleanup",
                    mail_id="REJECT1",
                    message="message-id=<rejected@example.com>",
                )
            ),
            classify_log_entry(
                LogEntry(
                    datetime="2026-08-02T00:04:29+00:00",
                    hostname="mail.example.com",
                    service="postfix/cleanup",
                    mail_id="REJECT1",
                    message=(
                        "milter-reject: END-OF-MESSAGE: "
                        "5.7.1 Command rejected"
                    ),
                )
            ),
        ]
        exporter = InMemorySpanExporter()
        generator = object.__new__(EmailTracesGenerator)

        with patch.object(otel, "_exporter", exporter):
            generator._export_traces({"rejected@example.com": logs})
            otel.flush_traces()

        finished = exporter.get_finished_spans()
        root = next(span for span in finished if span.name == "email.delivery")
        host = next(
            span for span in finished if span.name == "mail.example.com"
        )

        self.assertEqual(root.status.status_code, StatusCode.ERROR)
        self.assertEqual(root.attributes["error.type"], "permanent_failure")
        self.assertNotIn("smtp.response_code", root.attributes)
        self.assertEqual(host.status.status_code, StatusCode.ERROR)
        self.assertEqual(host.attributes["error.type"], "permanent_failure")
        self.assertNotIn("smtp.response_code", host.attributes)
        self.assertEqual(host.attributes["smtp.enhanced_status_code"], "5.7.1")
        self.assertFalse(any(span.name == "delivery" for span in finished))

    def test_permanent_failure_marks_its_branch_and_root(self) -> None:
        logs = [
            LogEntry(
                datetime="2026-08-02T00:00:00+00:00",
                hostname="source",
                service="postfix/cleanup",
                mail_id="SOURCE1",
                message="message-id=<branches@example.com>",
            ),
            LogEntry(
                datetime="2026-08-02T00:00:01+00:00",
                hostname="source",
                service="postfix/smtp",
                mail_id="SOURCE1",
                message=(
                    "to=<failed@example.com>, delay=1, "
                    "delays=0.1/0.1/0.1/0.7, status=sent "
                    "(250 2.0.0 Ok: queued as FAIL1)"
                ),
                queued_as="FAIL1",
                relay_host="failed-destination",
            ),
            LogEntry(
                datetime="2026-08-02T00:00:02+00:00",
                hostname="source",
                service="postfix/smtp",
                mail_id="SOURCE1",
                message=(
                    "to=<sent@example.com>, delay=1, "
                    "delays=0.1/0.1/0.1/0.7, status=sent "
                    "(250 2.0.0 Ok: queued as SENT1)"
                ),
                queued_as="SENT1",
                relay_host="sent-destination",
            ),
            LogEntry(
                datetime="2026-08-02T00:00:03+00:00",
                hostname="failed-destination",
                service="postfix/smtp",
                mail_id="FAIL1",
                message=(
                    "to=<failed@example.com>, delay=1, "
                    "delays=0.1/0.1/0.1/0.7, dsn=5.1.1, "
                    "status=bounced (550 5.1.1 User unknown)"
                ),
            ),
            LogEntry(
                datetime="2026-08-02T00:00:04+00:00",
                hostname="sent-destination",
                service="postfix/smtp",
                mail_id="SENT1",
                message=(
                    "to=<sent@example.com>, delay=1, "
                    "delays=0.1/0.1/0.1/0.7, dsn=2.0.0, "
                    "status=sent (250 2.0.0 Ok)"
                ),
            ),
        ]
        logs = [classify_log_entry(log) for log in logs]
        exporter = InMemorySpanExporter()
        generator = object.__new__(EmailTracesGenerator)

        with patch.object(otel, "_exporter", exporter):
            generator._export_traces({"branches@example.com": logs})
            otel.flush_traces()

        finished = exporter.get_finished_spans()
        root = next(span for span in finished if span.name == "email.delivery")
        branches = {
            span.attributes["email.destination"]: span
            for span in finished
            if span.name == "delivery.branch"
        }
        failed_host = next(
            span
            for span in finished
            if span.attributes.get("email.queue_id") == "FAIL1"
        )
        failed_delivery = next(
            span
            for span in finished
            if span.name == "delivery"
            and span.parent
            and span.parent.span_id == failed_host.context.span_id
        )

        self.assertEqual(root.status.status_code, StatusCode.ERROR)
        self.assertEqual(
            branches["failed-destination"].status.status_code,
            StatusCode.ERROR,
        )
        self.assertEqual(
            branches["sent-destination"].status.status_code,
            StatusCode.UNSET,
        )
        self.assertEqual(failed_delivery.status.status_code, StatusCode.ERROR)
        self.assertEqual(failed_host.status.status_code, StatusCode.UNSET)
        self.assertNotIn(
            "smtp.response_code", branches["failed-destination"].attributes
        )

    def test_handoff_without_delays_uses_log_timestamp(self) -> None:
        logs = [
            LogEntry(
                datetime="2026-07-30T16:10:00+00:00",
                hostname="source",
                service="postfix/cleanup",
                mail_id="SOURCE1",
                message="message-id=<fallback@example.com>",
            ),
            LogEntry(
                datetime="2026-07-30T16:10:01+00:00",
                hostname="source",
                service="postfix/local",
                mail_id="SOURCE1",
                message="status=sent (forwarded as CHILD1)",
                queued_as="CHILD1",
            ),
            LogEntry(
                datetime="2026-07-30T16:10:03+00:00",
                hostname="source",
                service="postfix/smtp",
                mail_id="CHILD1",
                message=(
                    "status=sent, delay=1, delays=0.1/0.1/0.1/0.7"
                ),
            ),
        ]
        exporter = InMemorySpanExporter()
        generator = object.__new__(EmailTracesGenerator)

        with patch.object(otel, "_exporter", exporter):
            generator._export_traces({"fallback@example.com": logs})
            otel.flush_traces()

        spans = {
            span.attributes.get("email.queue_id"): span
            for span in exporter.get_finished_spans()
            if span.attributes and "email.queue_id" in span.attributes
        }
        handoff_time = dt_to_ns(
            datetime.fromisoformat("2026-07-30T16:10:01+00:00")
        )

        self.assertEqual(spans["SOURCE1"].end_time, handoff_time)
        self.assertEqual(spans["CHILD1"].start_time, handoff_time)
        self.assertEqual(
            spans["CHILD1"].links[0].context.span_id,
            spans["SOURCE1"].context.span_id,
        )

    def test_relayed_queues_create_parallel_host_branches(self) -> None:
        logs = [
            LogEntry(
                datetime="2026-07-30T15:00:00+08:00",
                hostname="source",
                service="postfix/cleanup",
                mail_id="SRC1",
                message="message-id=<fanout@example.com>",
            ),
            LogEntry(
                datetime="2026-07-30T15:00:01+08:00",
                hostname="source",
                service="postfix/smtp",
                mail_id="SRC1",
                message=(
                    "to=<first@example.com>, status=sent, delay=1, "
                    "delays=0.1/0.1/0.1/0.7"
                ),
                queued_as="DST1",
                relay_host="destination-a",
            ),
            LogEntry(
                datetime="2026-07-30T15:00:02+08:00",
                hostname="source",
                service="postfix/smtp",
                mail_id="SRC1",
                message=(
                    "to=<second@example.com>, status=sent, delay=2, "
                    "delays=0.1/0.1/0.1/1.7"
                ),
                queued_as="DST2",
                relay_host="destination-b",
            ),
            LogEntry(
                datetime="2026-07-30T15:00:03+08:00",
                hostname="destination-a",
                service="postfix/smtp",
                mail_id="DST1",
                message="status=sent, delay=1, delays=0.1/0.1/0.1/0.7",
                queued_as="LEAF1",
                relay_host="leaf-a",
            ),
            LogEntry(
                datetime="2026-07-30T15:00:04+08:00",
                hostname="destination-b",
                service="postfix/smtp",
                mail_id="DST2",
                message="status=sent, delay=1, delays=0.1/0.1/0.1/0.7",
            ),
            LogEntry(
                datetime="2026-07-30T15:00:05+08:00",
                hostname="leaf-a",
                service="postfix/smtp",
                mail_id="LEAF1",
                message="status=sent, delay=1, delays=0.1/0.1/0.1/0.7",
            ),
        ]
        exporter = InMemorySpanExporter()
        generator = object.__new__(EmailTracesGenerator)

        with patch.object(otel, "_exporter", exporter):
            generator._export_traces({"fanout@example.com": logs})
            otel.flush_traces()

        spans = {
            span.attributes.get("email.queue_id"): span
            for span in exporter.get_finished_spans()
            if span.attributes and "email.queue_id" in span.attributes
        }
        source_span = spans["SRC1"]
        destination_a = spans["DST1"]
        destination_b = spans["DST2"]
        leaf_a = spans["LEAF1"]
        root = next(
            span
            for span in exporter.get_finished_spans()
            if span.name == "email.delivery"
        )
        branches = {
            span.attributes.get("email.destination"): span
            for span in exporter.get_finished_spans()
            if span.name == "delivery.branch" and span.attributes
        }

        self.assertEqual(source_span.parent.span_id, root.context.span_id)
        self.assertEqual(set(branches), {"destination-a", "destination-b"})
        self.assertTrue(
            all(
                branch.parent.span_id == root.context.span_id
                for branch in branches.values()
            )
        )
        self.assertEqual(
            destination_a.parent.span_id,
            branches["destination-a"].context.span_id,
        )
        self.assertEqual(
            destination_b.parent.span_id,
            branches["destination-b"].context.span_id,
        )
        self.assertEqual(
            leaf_a.parent.span_id,
            branches["destination-a"].context.span_id,
        )
        self.assertEqual(
            branches["destination-a"].attributes.get("email.recipient"),
            "first@example.com",
        )
        self.assertEqual(
            branches["destination-b"].attributes.get("email.recipient"),
            "second@example.com",
        )
        self.assertEqual(
            destination_a.links[0].context.span_id, source_span.context.span_id
        )
        self.assertEqual(
            destination_b.links[0].context.span_id, source_span.context.span_id
        )
        self.assertEqual(
            leaf_a.links[0].context.span_id, destination_a.context.span_id
        )
        self.assertLessEqual(
            branches["destination-a"].start_time, destination_a.start_time
        )
        self.assertGreaterEqual(
            branches["destination-a"].end_time, leaf_a.end_time
        )
        self.assertLessEqual(
            branches["destination-b"].start_time, destination_b.start_time
        )
        self.assertGreaterEqual(
            branches["destination-b"].end_time, destination_b.end_time
        )

        delivery_recipients = {
            span.attributes.get("email.recipient")
            for span in exporter.get_finished_spans()
            if span.name == "delivery" and span.attributes
        }
        self.assertTrue(
            {"first@example.com", "second@example.com"}.issubset(
                delivery_recipients
            )
        )

    def test_same_destination_hops_do_not_create_route_branches(self) -> None:
        logs = [
            LogEntry(
                datetime="2026-07-30T15:00:00+00:00",
                hostname="source",
                service="postfix/cleanup",
                mail_id="SRC1",
                message="message-id=<same-destination@example.com>",
            ),
            LogEntry(
                datetime="2026-07-30T15:00:01+00:00",
                hostname="source",
                service="postfix/smtp",
                mail_id="SRC1",
                message=(
                    "to=<first@example.com>, status=sent, delay=1, "
                    "delays=0.1/0.1/0.1/0.7"
                ),
                queued_as="DST1",
                relay_host="destination",
            ),
            LogEntry(
                datetime="2026-07-30T15:00:02+00:00",
                hostname="source",
                service="postfix/smtp",
                mail_id="SRC1",
                message=(
                    "to=<second@example.com>, status=sent, delay=2, "
                    "delays=0.2/0.1/0.1/1.6"
                ),
                queued_as="DST2",
                relay_host="destination",
            ),
            LogEntry(
                datetime="2026-07-30T15:00:03+00:00",
                hostname="destination",
                service="postfix/smtp",
                mail_id="DST1",
                message="status=sent, delay=1, delays=0.1/0.1/0.1/0.7",
            ),
            LogEntry(
                datetime="2026-07-30T15:00:04+00:00",
                hostname="destination",
                service="postfix/smtp",
                mail_id="DST2",
                message="status=sent, delay=1, delays=0.1/0.1/0.1/0.7",
            ),
        ]
        exporter = InMemorySpanExporter()
        generator = object.__new__(EmailTracesGenerator)

        with patch.object(otel, "_exporter", exporter):
            generator._export_traces({"same-destination@example.com": logs})
            otel.flush_traces()

        finished = exporter.get_finished_spans()
        root = next(span for span in finished if span.name == "email.delivery")
        hosts = {
            span.attributes.get("email.queue_id"): span
            for span in finished
            if span.attributes and "email.queue_id" in span.attributes
        }

        self.assertFalse(
            any(span.name == "delivery.branch" for span in finished)
        )
        self.assertEqual(hosts["DST1"].parent.span_id, root.context.span_id)
        self.assertEqual(hosts["DST2"].parent.span_id, root.context.span_id)
        self.assertEqual(
            hosts["DST1"].links[0].context.span_id,
            hosts["SRC1"].context.span_id,
        )
        self.assertEqual(
            hosts["DST2"].links[0].context.span_id,
            hosts["SRC1"].context.span_id,
        )

    def test_hop_parent_matching_accepts_fully_qualified_relay_host(
        self,
    ) -> None:
        source = ("source", "SRC1")
        destination = ("destination", "DST1")
        hops = {
            source: [
                LogEntry(
                    datetime="2026-07-30T15:00:01+00:00",
                    hostname="source",
                    service="postfix/smtp",
                    mail_id="SRC1",
                    message="status=sent (queued as DST1)",
                    queued_as="DST1",
                    relay_host="destination.example.com",
                )
            ],
            destination: [
                LogEntry(
                    datetime="2026-07-30T15:00:02+00:00",
                    hostname="destination",
                    service="postfix/cleanup",
                    mail_id="DST1",
                    message="message-id=<alias@example.com>",
                )
            ],
        }

        self.assertEqual(query.build_hop_parents(hops), {destination: source})

    def test_cyclic_hop_links_fall_back_to_root_children(self) -> None:
        logs = [
            LogEntry(
                datetime="2026-07-30T15:00:00+00:00",
                hostname="first",
                service="postfix/smtp",
                mail_id="FIRST1",
                message="status=sent, delay=1, delays=0.1/0.1/0.1/0.7",
                queued_as="SECOND1",
                relay_host="second",
            ),
            LogEntry(
                datetime="2026-07-30T15:00:01+00:00",
                hostname="second",
                service="postfix/smtp",
                mail_id="SECOND1",
                message="status=sent, delay=1, delays=0.1/0.1/0.1/0.7",
                queued_as="FIRST1",
                relay_host="first",
            ),
        ]
        exporter = InMemorySpanExporter()
        generator = object.__new__(EmailTracesGenerator)

        with patch.object(otel, "_exporter", exporter):
            generator._export_traces({"cycle@example.com": logs})
            otel.flush_traces()

        finished = exporter.get_finished_spans()
        root = next(span for span in finished if span.name == "email.delivery")
        hosts = [
            span
            for span in finished
            if span.attributes and "email.queue_id" in span.attributes
        ]

        self.assertEqual(len(hosts), 2)
        self.assertTrue(
            all(span.parent.span_id == root.context.span_id for span in hosts)
        )

    def test_extracts_same_host_forwarded_queue_id(self) -> None:
        log = LogEntry(
            datetime="2026-07-30T15:18:57+08:00",
            hostname="maildirect10",
            service="postfix/lmtp",
            mail_id="E2F1",
            message="status=sent (forwarded as E4F2)",
        )

        self.assertEqual(extract_next_mail_id(log), "E4F2")

    def test_detect_mta_ignores_missing_service(self) -> None:
        log = LogEntry(
            datetime="2026-07-30T15:18:57+08:00",
            hostname="maildirect10",
            service=None,
            mail_id="E2F1",
            message="status=sent",
        )

        self.assertIsNone(detect_mta_from_entries([log]))
        postfix_log = LogEntry(
            datetime=log.datetime,
            hostname=log.hostname,
            service="postfix/smtp",
            mail_id=log.mail_id,
            message=log.message,
        )
        self.assertEqual(
            detect_mta_from_entries([log, postfix_log]),
            "postfix",
        )


class LogEventParsingTest(unittest.TestCase):
    def test_milter_reject_extracts_event_and_temporary_status(self) -> None:
        lines = [
            "2026-08-02T00:04:28+08:00 csmx2.cs.nctu.edu.tw "
            "postfix/smtpd[4556]: 8F566600B6: "
            "client=farewell.cs.nctu.edu.tw[140.113.235.55]",
            "2026-08-02T00:04:28+08:00 csmx2.cs.nctu.edu.tw "
            "postfix/cleanup[4558]: 8F566600B6: "
            "message-id=<message@example.com>",
            "2026-08-02T00:04:28+08:00 csmx2.cs.nctu.edu.tw "
            "postfix/cleanup[4558]: 8F566600B6: "
            "milter-reject: END-OF-MESSAGE: 4.7.1 Try again later",
        ]

        logs = [SyslogParser().parse_with_enrichment(line) for line in lines]

        self.assertEqual(logs[1].mail_id, "8F566600B6")
        self.assertEqual(logs[0].event_type, EventType.CONNECTION)
        self.assertEqual(logs[1].event_type, EventType.MESSAGE_ID)
        self.assertEqual(logs[2].event_type, EventType.MILTER_REJECT)
        self.assertEqual(
            logs[2].smtp_status_class,
            SmtpStatusClass.TEMPORARY_FAILURE,
        )
        self.assertEqual(
            logs[2].delivery_status,
            DeliveryStatus.TEMPORARY_FAILURE,
        )
        self.assertFalse(logs[2].is_terminal)

    def test_mail_status_precedes_conflicting_smtp_code(self) -> None:
        deferred = classify_log_entry(
            LogEntry(
                datetime="2026-08-02T00:04:28+00:00",
                hostname="mail.example.com",
                service="postfix/smtp",
                mail_id="DEFER1",
                message=(
                    "to=<user@example.com>, status=deferred "
                    "(550 5.1.1 User unknown)"
                ),
            )
        )

        self.assertEqual(deferred.mail_status, "deferred")
        self.assertEqual(deferred.smtp_code, 550)
        self.assertEqual(deferred.smtp_enhanced_status_code, "5.1.1")
        self.assertEqual(
            deferred.delivery_status, DeliveryStatus.TEMPORARY_FAILURE
        )

    def test_delivery_event_falls_back_to_smtp_code(self) -> None:
        rejected = classify_log_entry(
            LogEntry(
                datetime="2026-08-02T00:04:28+00:00",
                hostname="mail.example.com",
                service="postfix/lmtp",
                mail_id="REJECT1",
                message="to=<user@example.com>, 550 5.1.1 User unknown",
            )
        )

        self.assertIsNone(rejected.mail_status)
        self.assertEqual(rejected.smtp_code, 550)
        self.assertEqual(rejected.smtp_enhanced_status_code, "5.1.1")
        self.assertEqual(
            rejected.delivery_status, DeliveryStatus.PERMANENT_FAILURE
        )
        self.assertTrue(rejected.is_terminal)

    def test_delivery_status_distinguishes_forwarding_and_final_delivery(
        self,
    ) -> None:
        parser = SyslogParser()
        forwarded = parser.parse_with_enrichment(
            "2026-08-02T00:04:28+08:00 source postfix/smtp[1]: Q1: "
            "to=<user@example.com>, status=sent (250 2.0.0 OK: "
            "queued as Q2), delays=0.1/0/0/0.2"
        )
        delivered = parser.parse_with_enrichment(
            "2026-08-02T00:04:29+08:00 destination postfix/lmtp[2]: Q2: "
            "to=<user@example.com>, status=sent (250 2.0.0 Saved), "
            "delays=0.01/0/0/0.02"
        )

        self.assertEqual(forwarded.event_type, EventType.SMTP_DELIVERY)
        self.assertEqual(forwarded.delivery_status, DeliveryStatus.FORWARDED)
        self.assertEqual(forwarded.smtp_status_class, SmtpStatusClass.SUCCESS)
        self.assertFalse(forwarded.is_terminal)
        self.assertEqual(delivered.event_type, EventType.LMTP_DELIVERY)
        self.assertEqual(delivered.delivery_status, DeliveryStatus.DELIVERED)
        self.assertTrue(delivered.is_terminal)

    def test_structured_fields_are_ignored_when_mapping_is_unset(self) -> None:
        parser = OpensearchParser(
            OpenSearchMappingConfig(
                facility="",
                hostname="host.name",
                message="message",
                timestamp="@timestamp",
                service="appname",
            )
        )
        entry = parser.parse_with_enrichment(
            {
                "@timestamp": "2026-08-02T00:04:28Z",
                "host": {"name": "csmx2"},
                "appname": "postfix/cleanup",
                "message": "LOGQ1: message-id=<message@example.com>",
                "postfix": {
                    "queueid": "STRUCTURED-QID",
                    "message-id": "structured@example.com",
                },
            }
        )

        self.assertEqual(entry.mail_id, "LOGQ1")
        self.assertEqual(entry.event_type, EventType.MESSAGE_ID)


class TraceLifecycleTest(unittest.TestCase):
    def test_duplicate_overlap_does_not_refresh_last_seen_round(self) -> None:
        log = LogEntry("t", "host", "service", "Q1", "message")
        pending = PendingTrace(
            logs=[log],
            first_seen_round=1,
            last_seen_round=1,
            has_terminal_outcome=True,
        )

        self.assertEqual(
            pending.merge([log], 2, EmailTracesGenerator._log_key),
            0,
        )
        self.assertEqual(pending.last_seen_round, 1)

    def test_temporary_failure_waits_until_max_age(self) -> None:
        pending = PendingTrace(
            logs=[LogEntry("t", "host", "service", "Q1", "temporary")],
            first_seen_round=1,
            last_seen_round=1,
            has_terminal_outcome=False,
        )

        self.assertFalse(
            should_export_trace(pending, 13, 15, 12, 1800)
        )
        self.assertTrue(
            should_export_trace(pending, 121, 15, 12, 1800)
        )

    def test_terminal_failure_exports_after_hold_rounds(self) -> None:
        pending = PendingTrace(
            logs=[],
            first_seen_round=1,
            last_seen_round=1,
            has_terminal_outcome=True,
        )

        self.assertTrue(
            should_export_trace(pending, 13, 15, 12, 1800)
        )


class OpenTelemetryResourceTest(unittest.TestCase):
    def setUp(self) -> None:
        otel._providers.clear()

    def tearDown(self) -> None:
        for provider in otel._providers.values():
            provider.shutdown()
        otel._providers.clear()

    def test_host_span_resource_identifies_host(self) -> None:
        start_time = datetime.fromisoformat("2026-01-08T22:55:08+08:00")

        with patch.object(otel, "_exporter", None):
            span = otel.create_host_span("mailpolicy", start_time, Context())
            span.end(end_time=dt_to_ns(start_time))

        self.assertEqual(
            otel._providers["mailpolicy"].resource.attributes.get("host.name"),
            "mailpolicy",
        )

    def test_root_span_recipients_are_comma_separated(self) -> None:
        start_time = datetime.fromisoformat("2026-01-08T22:55:08+08:00")

        with patch.object(otel, "_exporter", None):
            span = otel.create_root_span(
                "message@example.com",
                start_time,
                recipients=["first@example.com", "second@example.com"],
            )
            span.end(end_time=dt_to_ns(start_time))

        if not isinstance(span, SDKSpan):
            self.fail(f"Expected SDKSpan, got {type(span).__name__}")
        if span.attributes is None:
            self.fail("Expected span attributes")
        self.assertEqual(
            span.attributes["email.recipients"],
            "first@example.com,second@example.com",
        )

    def test_host_span_recipients_are_comma_separated(self) -> None:
        start_time = datetime.fromisoformat("2026-01-08T22:55:08+08:00")

        with patch.object(otel, "_exporter", None):
            span = otel.create_host_span(
                "mailpolicy",
                start_time,
                Context(),
                recipients=["first@example.com", "second@example.com"],
            )
            span.end(end_time=dt_to_ns(start_time))

        if not isinstance(span, SDKSpan):
            self.fail(f"Expected SDKSpan, got {type(span).__name__}")
        if span.attributes is None:
            self.fail("Expected span attributes")
        self.assertEqual(
            span.attributes["email.recipients"],
            "first@example.com,second@example.com",
        )

    def test_host_span_contains_delivery_attributes(self) -> None:
        start_time = datetime.fromisoformat("2026-01-08T22:55:08+08:00")

        with patch.object(otel, "_exporter", None):
            span = otel.create_host_span(
                "mailpolicy",
                start_time,
                Context(),
                transport="postfix/smtp",
                next_queue_id="NEXT123",
                relay_host="mailer4",
                relay_ip="192.0.2.4",
                relay_port=25,
                smtp_response_code=250,
            )
            span.end(end_time=dt_to_ns(start_time))

        if not isinstance(span, SDKSpan):
            self.fail(f"Expected SDKSpan, got {type(span).__name__}")
        if span.attributes is None:
            self.fail("Expected span attributes")
        self.assertEqual(span.attributes["email.transport"], "postfix/smtp")
        self.assertEqual(span.attributes["email.next_queue_id"], "NEXT123")
        self.assertEqual(span.attributes["email.relay_host"], "mailer4")
        self.assertEqual(span.attributes["email.relay_ip"], "192.0.2.4")
        self.assertEqual(span.attributes["email.relay_port"], 25)
        self.assertEqual(span.attributes["smtp.response_code"], 250)

    def test_delay_span_resource_identifies_host(self) -> None:
        start_time = datetime.fromisoformat("2026-01-08T22:55:08+08:00")
        delays = DelayInfo(
            before_qmgr=0.02,
            in_qmgr=0,
            conn_setup=0.06,
            transmission=0.02,
        )

        with patch.object(otel, "_exporter", None):
            otel.create_delay_spans(
                delays, "mailpolicy", start_time, Context()
            )

        self.assertEqual(
            otel._providers["mailpolicy"].resource.attributes.get("host.name"),
            "mailpolicy",
        )

    def test_delay_spans_apply_minimum_duration(self) -> None:
        start_time = datetime.fromisoformat("2026-01-08T22:55:08+08:00")
        delays = DelayInfo(
            before_qmgr=0.02,
            in_qmgr=0,
            conn_setup=0.06,
            transmission=0.02,
        )

        with patch.object(otel, "_exporter", None):
            spans = otel.create_delay_spans(
                delays, "mailpolicy", start_time, Context()
            )

        span = spans[-1]
        if not isinstance(span, SDKSpan):
            self.fail(f"Expected SDKSpan, got {type(span).__name__}")
        self.assertEqual(
            span.end_time,
            dt_to_ns(start_time + timedelta(seconds=0.100002)),
        )


if __name__ == "__main__":
    unittest.main()
