import unittest
from unittest.mock import patch

from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from mailtrace.models import LogEntry
from mailtrace.tracing import otel
from mailtrace.tracing.builder import export_traces


class RecipientAttributesTest(unittest.TestCase):
    def setUp(self) -> None:
        otel._providers.clear()

    def tearDown(self) -> None:
        for provider in otel._providers.values():
            provider.shutdown()
        otel._providers.clear()

    def export(self, logs: list[LogEntry]):
        exporter = InMemorySpanExporter()
        with patch.object(otel, "_exporter", exporter):
            self.assertEqual(export_traces({"message@example.com": logs}), 1)
            otel.flush_traces()
        return exporter.get_finished_spans()

    def delivery_log(
        self,
        hostname: str,
        queue_id: str,
        message: str,
        second: int,
        **kwargs,
    ) -> LogEntry:
        return LogEntry(
            datetime=f"2026-10-09T12:00:{second:02d}+00:00",
            hostname=hostname,
            service="postfix/smtp",
            mail_id=queue_id,
            message=f"{message}, status=sent, delays=0.1/0.1/0.1/0.7",
            **kwargs,
        )

    def test_nested_aliases_keep_original_and_expanded_addresses_separate(
        self,
    ) -> None:
        logs = [
            self.delivery_log(
                "source",
                "SOURCE1",
                "to=<team@example.com>",
                1,
                queued_as="POLICY1",
                relay_host="policy",
            ),
            self.delivery_log(
                "policy",
                "POLICY1",
                "orig_to=<team@example.com>, to=<first@example.com>",
                2,
            ),
            self.delivery_log(
                "policy",
                "POLICY1",
                "to=<nested@example.com>, orig_to=<team@example.com>",
                3,
                queued_as="SOURCE2",
                relay_host="source",
            ),
            self.delivery_log(
                "source",
                "SOURCE2",
                "to=<nested@example.com>",
                4,
                queued_as="POLICY2",
                relay_host="policy",
            ),
            self.delivery_log(
                "policy",
                "POLICY2",
                "orig_to=<nested@example.com>, to=<second@example.com>",
                5,
            ),
            self.delivery_log(
                "policy",
                "POLICY1",
                "to=<first@example.com>, orig_to=<team@example.com>",
                6,
            ),
        ]

        spans = self.export(logs)
        root = next(span for span in spans if span.name == "email.delivery")
        self.assertEqual(
            root.attributes["email.recipients"], "team@example.com"
        )
        self.assertEqual(
            root.attributes["email.expanded_recipients"],
            "first@example.com,nested@example.com,second@example.com",
        )
        hosts = {
            (
                span.attributes["email.queue_id"],
                span.attributes["email.expanded_recipients"],
            ): span
            for span in spans
            if "email.queue_id" in span.attributes
        }
        self.assertEqual(
            set(hosts),
            {
                ("SOURCE1", ""),
                ("POLICY1", "first@example.com"),
                ("POLICY1", "nested@example.com"),
                ("SOURCE2", ""),
                ("POLICY2", "second@example.com"),
            },
        )
        for span in spans:
            self.assertNotIn("email.recipient", span.attributes)
        for address in ("first@example.com", "nested@example.com"):
            host = hosts[("POLICY1", address)]
            self.assertEqual(
                host.attributes["email.recipients"], "team@example.com"
            )
            deliveries = [
                span
                for span in spans
                if span.name == "delivery"
                and span.parent.span_id == host.context.span_id
            ]
            self.assertTrue(deliveries)
            for delivery in deliveries:
                self.assertEqual(
                    delivery.attributes["email.recipients"], "team@example.com"
                )
                self.assertEqual(
                    delivery.attributes["email.expanded_recipients"], address
                )
        self.assertEqual(
            hosts[("POLICY2", "second@example.com")].attributes[
                "email.recipients"
            ],
            "nested@example.com",
        )

    def test_direct_recipients_have_no_expanded_addresses(self) -> None:
        logs = [
            self.delivery_log("source", "SOURCE1", f"to=<{address}>", second)
            for second, address in enumerate(
                ("first@example.com", "second@example.com"), start=1
            )
        ]
        spans = self.export(logs)
        root = next(span for span in spans if span.name == "email.delivery")
        self.assertEqual(
            root.attributes["email.recipients"],
            "first@example.com,second@example.com",
        )
        for span in spans:
            if span.name in {"email.delivery", "source", "delivery"}:
                self.assertEqual(
                    span.attributes["email.expanded_recipients"], ""
                )

    def test_original_address_is_available_without_upstream_delivery_logs(
        self,
    ) -> None:
        logs = [
            LogEntry(
                datetime="2026-10-09T12:00:00+00:00",
                hostname="source",
                service="postfix/cleanup",
                mail_id="SOURCE1",
                message="message-id=<message@example.com>",
                queued_as="POLICY1",
                relay_host="policy",
            ),
            self.delivery_log(
                "policy",
                "POLICY1",
                "orig_to=<team@example.com>, to=<first@example.com>",
                1,
            ),
        ]
        spans = self.export(logs)
        root = next(span for span in spans if span.name == "email.delivery")
        self.assertEqual(
            root.attributes["email.recipients"], "team@example.com"
        )
        self.assertEqual(
            root.attributes["email.expanded_recipients"], "first@example.com"
        )

    def test_missing_recipient_metadata_is_omitted(self) -> None:
        spans = self.export(
            [self.delivery_log("source", "SOURCE1", "relay=destination", 1)]
        )
        for span in spans:
            self.assertNotIn("email.recipients", span.attributes)
            self.assertNotIn("email.expanded_recipients", span.attributes)


if __name__ == "__main__":
    unittest.main()
