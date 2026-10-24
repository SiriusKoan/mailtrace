"""Test generic same-host inference before global dependency ordering."""

import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from demo.docker.bench_handoff_correctness import export_span_graphs

from mailtrace.models import LogEntry


class TraceOrderingTest(unittest.TestCase):
    def log(self, host, queue, second, service, message, **fields):
        stamp = datetime(2026, 9, 1, tzinfo=timezone.utc) + timedelta(
            seconds=second
        )
        return LogEntry(
            stamp.isoformat(), host, service, queue, message, **fields
        )

    def incoming(self, upstream, queue, second=0):
        return [
            self.log(
                upstream,
                upstream,
                second,
                "postfix/smtp",
                "to=<recipient@example.test>, status=sent, delays=0/0/0/0.01",
                queued_as=queue,
                relay_host="relay",
            ),
            self.log(
                "relay",
                queue,
                second,
                "postfix/cleanup",
                "message-id=<test@example.test>",
            ),
            self.log(
                "relay",
                queue,
                second + 0.1,
                "postfix/smtp",
                "to=<recipient@example.test>, status=sent, delays=0/0/0/0.01",
            ),
        ]

    def unlinked(self, second=2):
        return [
            self.log(
                "relay",
                "B",
                second,
                "postfix/qmgr",
                "from=<sender@example.test>, size=100, nrcpt=1",
            ),
            self.log(
                "relay",
                "B",
                second + 1,
                "postfix/smtp",
                "to=<recipient@example.test>, status=sent, delays=0/0/0/0.01",
            ),
        ]

    def graph(self, logs):
        count, graphs = export_span_graphs({"test@example.test": logs})
        self.assertEqual(count, 1)
        return graphs["test@example.test"]

    def test_clock_skew_does_not_leave_inferred_hop_at_root(self):
        logs = self.incoming("p", "A") + self.unlinked()
        logs[0] = replace(logs[0], datetime="2026-09-01T00:02:00+00:00")
        _, edges, attributes, _ = self.graph(logs)
        self.assertIn(
            (("p.example.com", "p"), ("relay.example.com", "A")), edges
        )
        self.assertIn(
            (("p.example.com", "p"), ("relay.example.com", "B")), edges
        )
        self.assertIn(
            "implicit",
            {
                a.get("email.handoff")
                for a in attributes
                if a["hop"] == ("relay.example.com", "B")
            },
        )

    def test_input_order_does_not_change_inferred_parent(self):
        logs = self.incoming("p", "A") + self.unlinked()
        self.assertEqual(
            self.graph(logs)[:2], self.graph(list(reversed(logs)))[:2]
        )

    def test_explicit_handoff_takes_priority_over_local_inference(self):
        logs = self.incoming("p", "A") + self.unlinked()
        logs.append(
            self.log(
                "q",
                "q",
                1.5,
                "postfix/smtp",
                "status=sent",
                queued_as="B",
                relay_host="relay",
            )
        )
        _, edges, attributes, _ = self.graph(logs)
        self.assertIn(
            (("q.example.com", "q"), ("relay.example.com", "B")), edges
        )
        self.assertNotIn(
            (("p.example.com", "p"), ("relay.example.com", "B")), edges
        )
        self.assertIn(
            "explicit",
            {
                a.get("email.handoff")
                for a in attributes
                if a["hop"] == ("relay.example.com", "B")
            },
        )

    def test_inference_uses_local_chronology_despite_another_hosts_clock(self):
        logs = (
            self.incoming("p", "A")
            + self.incoming("q", "X", second=1)
            + self.unlinked()
        )
        logs[3] = replace(logs[3], datetime="2026-09-01T00:02:00+00:00")
        _, edges, _, _ = self.graph(logs)
        self.assertIn(
            (("q.example.com", "q"), ("relay.example.com", "B")), edges
        )
        self.assertNotIn(
            (("p.example.com", "p"), ("relay.example.com", "B")), edges
        )
