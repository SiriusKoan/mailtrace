import unittest
from types import SimpleNamespace
from unittest.mock import patch

from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from opentelemetry.trace import StatusCode

from mailtrace.config import (
    Config,
    Method,
    OpenSearchConfig,
    OpenSearchMappingConfig,
    SSHConfig,
)
from mailtrace.models import LogEntry
from mailtrace.parser import (
    OpensearchParser,
    SyslogParser,
    extract_next_mail_id,
)
from mailtrace.tracing import EmailTracesGenerator, otel, query


def _logs() -> list[LogEntry]:
    lines = [
        "00.000000 mail.example.com postfix/cleanup AAAAAA00001 "
        "message-id=<original@example.com>",
        "00.000000 mail.example.com postfix/qmgr AAAAAA00001 "
        "from=<sender@example.com>, size=100, nrcpt=1 (queue active)",
        "02.000000 mail.example.com postfix/smtp AAAAAA00001 "
        "to=<missing@example.com>, delay=2, delays=0.1/0.1/0.1/1.7, "
        "dsn=5.1.1, status=bounced (550 5.1.1 User unknown)",
        "02.100000 mail.example.com postfix/cleanup BBBBBB00002 "
        "message-id=<bounce@example.com>",
        "02.200000 mail.example.com postfix/bounce AAAAAA00001 "
        "sender non-delivery notification: BBBBBB00002",
        "02.200000 mail.example.com postfix/qmgr BBBBBB00002 "
        "from=<>, size=200, nrcpt=1 (queue active)",
        "03.200000 mail.example.com postfix/smtp BBBBBB00002 "
        "to=<sender@example.com>, relay=return.example.com[192.0.2.2]:25, "
        "delay=1.1, delays=0.1/0.1/0.1/0.8, dsn=2.0.0, "
        "status=sent (250 2.0.0 Ok: queued as CCCCCC00003)",
        "03.200000 return.example.com postfix/cleanup CCCCCC00003 "
        "message-id=<bounce@example.com>",
        "04.200000 return.example.com postfix/lmtp CCCCCC00003 "
        "to=<sender@example.com>, delay=1, delays=0.1/0.1/0.1/0.7, "
        "dsn=2.0.0, status=sent (250 2.0.0 Saved)",
    ]
    parser = SyslogParser()
    entries = []
    for line in lines:
        seconds, host, service, queue_id, message = line.split(" ", 4)
        entries.append(
            parser.parse_with_enrichment(
                f"2026-10-12T00:00:{seconds}+00:00 "
                f"{host} {service}[123]: {queue_id}: {message}"
            )
        )
    return entries


class BounceTracingTest(unittest.TestCase):
    def setUp(self) -> None:
        otel._providers.clear()
        self.config = Config(
            method=Method.OPENSEARCH,
            log_level="INFO",
            ssh_config=SSHConfig(username="test", private_key="unused"),
            opensearch_config=OpenSearchConfig(),
        )

    def tearDown(self) -> None:
        for provider in otel._providers.values():
            provider.shutdown()
        otel._providers.clear()

    def test_parsers_enrich_only_sender_non_delivery_notifications(
        self,
    ) -> None:
        parser = OpensearchParser(
            OpenSearchMappingConfig(
                facility="",
                hostname="host.name",
                message="message",
                timestamp="@timestamp",
                service="appname",
            )
        )
        cases = [
            (
                "postfix/bounce",
                "sender non-delivery notification: BBBBBB00002",
                "BBBBBB00002",
            ),
            (
                "postfix/bounce",
                "sender non-delivery notification: 3Pt2mN2VXxznjll",
                "3Pt2mN2VXxznjll",
            ),
            (
                "postfix/smtp",
                "sender non-delivery notification: BBBBBB00002",
                None,
            ),
            (
                "postfix/bounce",
                "sender delivery status notification: BBBBBB00002",
                None,
            ),
            ("postfix/bounce", "sender non-delivery notification:", None),
        ]
        for service, message, expected in cases:
            with self.subTest(service=service, message=message):
                entry = parser.parse_with_enrichment(
                    {
                        "@timestamp": "2026-10-12T00:00:02Z",
                        "host": {"name": "mail.example.com"},
                        "appname": service,
                        "message": f"AAAAAA00001: {message}",
                    }
                )
                self.assertEqual(entry.mail_id, "AAAAAA00001")
                self.assertEqual(entry.queued_as, expected)
                self.assertEqual(extract_next_mail_id(entry), expected)
                self.assertIsNone(entry.relay_host)

    def test_notification_merges_distinct_message_ids_in_any_log_order(
        self,
    ) -> None:
        logs = _logs()
        for ordered_logs in (logs, list(reversed(logs))):
            with self.subTest(reverse=ordered_logs != logs):
                grouped = query.group_logs_by_message_id(ordered_logs)
                self.assertEqual(
                    grouped, {"original@example.com": ordered_logs}
                )
                self.assertEqual(
                    query.build_hop_parents(
                        query.group_logs_by_hops(ordered_logs)
                    ),
                    {
                        ("mail.example.com", "BBBBBB00002"): (
                            "mail.example.com",
                            "AAAAAA00001",
                        ),
                        ("return.example.com", "CCCCCC00003"): (
                            "mail.example.com",
                            "BBBBBB00002",
                        ),
                    },
                )

    def test_notification_does_not_merge_same_queue_id_on_another_host(
        self,
    ) -> None:
        logs = _logs()
        other = SyslogParser().parse_with_enrichment(
            "2026-10-12T00:00:02+00:00 other.example.com "
            "postfix/cleanup[123]: BBBBBB00002: message-id=<other@example.com>"
        )
        grouped = query.group_logs_by_message_id(logs + [other])
        self.assertEqual(grouped["original@example.com"], logs)
        self.assertEqual(grouped["other@example.com"], [other])

    def test_late_notification_merges_pending_traces_and_survives_overlap(
        self,
    ) -> None:
        logs = _logs()
        generator = object.__new__(EmailTracesGenerator)
        generator.config = self.config
        generator._pending = {}
        generator._queue_id_to_message_id = {}
        generator._current_round = 0
        batches = [
            logs[:3],
            [logs[3], *logs[5:8]],
            [logs[4]],
            [logs[3], logs[7], logs[8]],
        ]
        for batch in batches:
            generator._current_round += 1
            generator._accumulate_logs(
                query.group_logs_by_message_id(
                    batch, generator._queue_id_to_message_id
                )
            )

        self.assertEqual(set(generator._pending), {"original@example.com"})
        pending = generator._pending["original@example.com"]
        self.assertEqual(
            {generator._log_key(log) for log in pending.logs},
            {generator._log_key(log) for log in logs},
        )
        self.assertEqual(len(pending.logs), len(logs))
        self.assertEqual(pending.first_seen_round, 1)
        self.assertEqual(pending.last_seen_round, 4)
        self.assertEqual(
            set(generator._queue_id_to_message_id.values()),
            {"original@example.com"},
        )

        generator.config = SimpleNamespace(
            tracing=SimpleNamespace(
                hold_rounds=2, sleep_seconds=10, max_trace_age_seconds=600
            )
        )
        generator._current_round = 6
        self.assertEqual(
            set(generator._collect_ready()), {"original@example.com"}
        )
        self.assertEqual(generator._pending, {})
        self.assertEqual(generator._queue_id_to_message_id, {})

    def test_notification_before_bounce_logs_keeps_remote_delivery_in_original_trace(
        self,
    ) -> None:
        logs = _logs()
        for later_logs in (
            [logs[3], *logs[5:]],
            list(reversed([logs[3], *logs[5:]])),
            logs[5:],
        ):
            with self.subTest(later_logs=later_logs):
                queue_mapping = {}
                query.group_logs_by_message_id(
                    [*logs[:3], logs[4]], queue_mapping
                )
                self.assertEqual(
                    query.group_logs_by_message_id(later_logs, queue_mapping),
                    {"original@example.com": later_logs},
                )

    def test_export_keeps_original_failure_and_nests_successful_bounce_delivery(
        self,
    ) -> None:
        logs = _logs()
        exporter = InMemorySpanExporter()
        generator = object.__new__(EmailTracesGenerator)
        generator.config = self.config
        with patch.object(otel, "_exporter", exporter):
            count = generator._export_traces(
                query.group_logs_by_message_id(logs)
            )
            otel.flush_traces()

        self.assertEqual(count, 1)
        finished = exporter.get_finished_spans()
        root = next(span for span in finished if span.name == "email.delivery")
        hosts = {
            span.attributes["email.queue_id"]: span
            for span in finished
            if "email.queue_id" in span.attributes
        }
        self.assertEqual(
            set(hosts), {"AAAAAA00001", "BBBBBB00002", "CCCCCC00003"}
        )
        self.assertTrue(
            all(
                span.context.trace_id == root.context.trace_id
                for span in finished
            )
        )
        self.assertEqual(root.status.status_code, StatusCode.ERROR)
        self.assertEqual(root.attributes["error.type"], "permanent_failure")
        self.assertEqual(root.attributes["message.id"], "original@example.com")
        self.assertEqual(root.attributes["email.sender"], "sender@example.com")
        self.assertEqual(
            root.attributes["email.recipients"], "missing@example.com"
        )
        self.assertEqual(root.attributes["email.expanded_recipients"], "")
        self.assertEqual(
            hosts["AAAAAA00001"].attributes["email.next_queue_id"],
            "BBBBBB00002",
        )
        self.assertEqual(
            hosts["BBBBBB00002"].parent.span_id,
            hosts["AAAAAA00001"].context.span_id,
        )
        self.assertEqual(
            hosts["BBBBBB00002"].attributes["email.handoff"], "explicit"
        )
        self.assertEqual(
            hosts["BBBBBB00002"].attributes["message.id"], "bounce@example.com"
        )
        self.assertEqual(
            hosts["CCCCCC00003"].attributes["message.id"], "bounce@example.com"
        )

        deliveries = {
            span.parent.span_id: span
            for span in finished
            if span.name == "delivery"
        }
        self.assertEqual(
            deliveries[
                hosts["AAAAAA00001"].context.span_id
            ].status.status_code,
            StatusCode.ERROR,
        )
        self.assertEqual(
            deliveries[hosts["AAAAAA00001"].context.span_id].attributes[
                "mail.delivery_status"
            ],
            "bounced",
        )
        for queue_id in ("BBBBBB00002", "CCCCCC00003"):
            delivery = deliveries[hosts[queue_id].context.span_id]
            self.assertEqual(delivery.status.status_code, StatusCode.UNSET)
            self.assertEqual(
                delivery.attributes["email.recipients"], "sender@example.com"
            )
        transmission = next(
            span
            for span in finished
            if span.name == "transmission"
            and span.parent.span_id
            == deliveries[hosts["BBBBBB00002"].context.span_id].context.span_id
        )
        self.assertEqual(
            hosts["CCCCCC00003"].parent.span_id, transmission.context.span_id
        )


if __name__ == "__main__":
    unittest.main()
