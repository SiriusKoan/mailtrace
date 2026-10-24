"""Test configured Mailman inference through the daemon export path."""

import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from mailtrace.config import MailingListConfig, load_config
from mailtrace.models import LogEntry
from mailtrace.tracing import EmailTracesGenerator, otel

_CONFIG = """method: opensearch
log_level: INFO
ssh_config:
  username: test
  private_key: unused
opensearch_config: {}
"""
_HOST = "maillist1.example.test"


def config(extra=""):
    with TemporaryDirectory() as directory:
        path = Path(directory) / "config.yaml"
        path.write_text(_CONFIG + extra)
        return load_config(str(path))


class MailmanConfigTest(unittest.TestCase):
    def test_yaml_loads_explicit_hosts_and_defaults_to_disabled(self):
        self.assertEqual(config().mailing_list.mailman, [])
        self.assertEqual(
            config(
                'mailing_list:\n  mailman: ["maillist1"]\n'
            ).mailing_list.mailman,
            ["maillist1"],
        )

    def test_invalid_host_lists_are_rejected(self):
        for value in ('"maillist1"', "null", "[1]", '[""]', '[" "]'):
            with (
                self.subTest(value=value),
                self.assertRaisesRegex(ValueError, "mailing_list.mailman"),
            ):
                config(f"mailing_list:\n  mailman: {value}\n")


class MailmanInferenceTest(unittest.TestCase):
    def setUp(self):
        otel._providers.clear()
        self.config = config('mailing_list:\n  mailman: ["maillist1"]\n')
        with patch("mailtrace.tracing.init_exporter"):
            self.generator = EmailTracesGenerator(self.config, "memory://")

    def tearDown(self):
        for provider in otel._providers.values():
            provider.shutdown()
        otel._providers.clear()

    def log(self, host, queue, second, service, message, **fields):
        stamp = datetime(2026, 9, 1, tzinfo=timezone.utc) + timedelta(
            seconds=second
        )
        return LogEntry(
            stamp.isoformat(), host, service, queue, message, **fields
        )

    def incoming(
        self,
        upstream,
        queue,
        start,
        recipient,
        *,
        completed=None,
        service="postfix/lmtp",
        status="sent",
    ):
        return [
            self.log(
                upstream,
                upstream.upper(),
                start,
                "postfix/smtp",
                "status=sent, delays=0/0/0/0.01",
                queued_as=queue,
                relay_host=_HOST,
            ),
            self.log(
                _HOST,
                queue,
                start,
                "postfix/cleanup",
                "message-id=<test@example.test>",
            ),
            self.log(
                _HOST,
                queue,
                start + 0.5 if completed is None else completed,
                service,
                f"to=<{recipient}>, status={status}, " "delays=0/0/0/0.01",
            ),
        ]

    def reinjected(self, sender="team-bounces@lists.example.test"):
        return [
            self.log(
                _HOST,
                "B",
                2,
                "postfix/qmgr",
                f"from=<{sender}>, size=100, nrcpt=1",
            ),
            self.log(
                _HOST,
                "B",
                3,
                "postfix/smtp",
                "to=<recipient@example.test>, status=sent, "
                "delays=0/0/0/0.01",
            ),
        ]

    def logs(self, recipient="team@example.test", **source_options):
        return (
            self.incoming("p", "A", 0, recipient, **source_options)
            + self.incoming("q", "X", 1, "other@example.test")
            + self.reinjected()
        )

    def graph(self, logs):
        for provider in otel._providers.values():
            provider.shutdown()
        otel._providers.clear()
        exporter = InMemorySpanExporter()
        with patch.object(otel, "_exporter", exporter):
            count = self.generator._export_traces({"test@example.test": logs})
            otel.flush_traces()
        self.assertEqual(count, 1)
        spans = exporter.get_finished_spans()
        by_id = {span.context.span_id: span for span in spans}
        parents, attributes = {}, {}
        for span in spans:
            queue = span.attributes.get("email.queue_id")
            if queue is None:
                continue
            attributes[queue] = dict(span.attributes)
            parent = span.parent
            parents[queue] = None
            while parent is not None:
                ancestor = by_id[parent.span_id]
                if "email.queue_id" in ancestor.attributes:
                    parents[queue] = ancestor.attributes["email.queue_id"]
                    break
                parent = ancestor.parent
        return parents, attributes

    def test_configured_host_selects_address_branch_and_keeps_siblings_implicit(
        self,
    ):
        parents, attributes = self.graph(self.logs())
        self.assertEqual(parents["A"], "P")
        self.assertEqual(parents["X"], "Q")
        self.assertEqual(parents["B"], "P")
        self.assertEqual(attributes["B"]["email.handoff"], "implicit")

    def test_disabled_and_other_hosts_keep_generic_inference(self):
        for hosts in ([], ["other"], ["maillist10"]):
            with self.subTest(hosts=hosts):
                self.config.mailing_list = MailingListConfig(mailman=hosts)
                parents, _ = self.graph(self.logs())
                self.assertEqual(parents["B"], "Q")

    def test_hostname_matching_accepts_short_names_case_and_trailing_dot(self):
        self.config.mailing_list = MailingListConfig(mailman=["MAILLIST1."])
        parents, _ = self.graph(self.logs())
        self.assertEqual(parents["B"], "P")

    def test_owner_addresses_and_list_domain_variants(self):
        for recipient in (
            "team@example.test",
            "team@lists.example.test",
            "team-owner@example.test",
            "team-owner@lists.example.test",
        ):
            with self.subTest(recipient=recipient):
                parents, _ = self.graph(self.logs(recipient))
                self.assertEqual(parents["B"], "P")

    def test_verp_sender_and_reversed_input(self):
        logs = self.logs()[:-2] + self.reinjected(
            "TEAM-bounces+alice=other.test@LISTS.EXAMPLE.TEST"
        )
        parents, _ = self.graph(list(reversed(logs)))
        self.assertEqual(parents["B"], "P")

    def test_explicit_queue_handoff_overrides_address_inference(self):
        logs = self.logs()
        logs.append(
            self.log(
                "r",
                "R",
                1.8,
                "postfix/smtp",
                "status=sent",
                queued_as="B",
                relay_host=_HOST,
            )
        )
        parents, attributes = self.graph(logs)
        self.assertEqual(parents["B"], "R")
        self.assertEqual(attributes["B"]["email.handoff"], "explicit")

    def test_missing_failed_future_and_non_lmtp_sources_remain_unknown(self):
        cases = (
            {"recipient": "other@example.test"},
            {"recipient": "team@other.test"},
            {"status": "deferred"},
            {"completed": 2.5},
            {"service": "postfix/smtp"},
        )
        for options in cases:
            with self.subTest(options=options):
                parents, attributes = self.graph(self.logs(**options))
                self.assertIsNone(parents["B"])
                self.assertEqual(attributes["B"]["email.handoff"], "none")

    def test_latest_completed_matching_delivery_is_selected(self):
        logs = (
            self.incoming("p", "A", 0, "team@example.test")
            + self.incoming("q", "X", 1, "team@example.test")
            + self.reinjected()
        )
        parents, _ = self.graph(logs)
        self.assertEqual(parents["B"], "Q")

    def test_tied_matching_deliveries_remain_unknown(self):
        logs = (
            self.incoming("p", "A", 0, "team@example.test", completed=1.5)
            + self.incoming("q", "X", 1, "team@example.test", completed=1.5)
            + self.reinjected()
        )
        for rows in (logs, list(reversed(logs))):
            parents, attributes = self.graph(rows)
            self.assertIsNone(parents["B"])
            self.assertEqual(attributes["B"]["email.handoff"], "none")

    def test_ordinary_sender_keeps_generic_inference_on_configured_host(self):
        logs = self.logs()[:-2] + self.reinjected("sender@example.test")
        parents, _ = self.graph(logs)
        self.assertEqual(parents["B"], "Q")

    def test_configured_host_does_not_apply_rules_to_another_host(self):
        logs = [
            (
                replace(log, hostname="other.example.test")
                if log.hostname == _HOST
                else (
                    replace(log, relay_host="other.example.test")
                    if log.relay_host == _HOST
                    else log
                )
            )
            for log in self.logs()
        ]
        parents, _ = self.graph(logs)
        self.assertEqual(parents["B"], "Q")
