import unittest
from datetime import datetime, timedelta

from mailtrace.models import LogEntry
from mailtrace.tracing.builder import (
    DeliveryRecord,
    HopPlacement,
    PreparedHop,
    _calculate_hop_timing,
    _plan_hops,
    _prepare_hops,
)
from mailtrace.tracing.delay_parser import DelayInfo
from mailtrace.tracing.otel import MIN_SPAN_DURATION_SECONDS


class TraceBuilderTest(unittest.TestCase):
    def test_prepare_hops_names_delivery_fields_and_bounds(self) -> None:
        completion = datetime.fromisoformat("2026-01-08T22:55:08.952580+08:00")
        delivery_log = LogEntry(
            datetime=completion.isoformat(),
            hostname="mail-a",
            service="postfix/smtp",
            mail_id="QUEUE-A",
            message=(
                "to=<recipient@example.com>, status=sent, "
                "delays=0.02/0/0.06/0.02"
            ),
        )
        no_delay_time = completion + timedelta(seconds=1)
        no_delay_log = LogEntry(
            datetime=no_delay_time.isoformat(),
            hostname="mail-b",
            service="postfix/cleanup",
            mail_id="QUEUE-B",
            message="message-id=<message@example.com>",
        )

        prepared = _prepare_hops(
            {
                ("mail-a", "QUEUE-A"): [delivery_log],
                ("mail-b", "QUEUE-B"): [no_delay_log],
            }
        )

        delivery_hop = prepared[("mail-a", "QUEUE-A")]
        self.assertEqual(len(delivery_hop.delivery_records), 1)
        record = delivery_hop.delivery_records[0]
        self.assertIs(record.log, delivery_log)
        self.assertEqual(record.recipient, "recipient@example.com")
        self.assertEqual(
            record.start_time,
            completion - timedelta(seconds=0.100002),
        )
        self.assertEqual(delivery_hop.raw_end, completion)

        no_delay_hop = prepared[("mail-b", "QUEUE-B")]
        self.assertEqual(no_delay_hop.delivery_records, [])
        self.assertEqual(no_delay_hop.raw_start, no_delay_time)
        self.assertEqual(no_delay_hop.raw_end, no_delay_time)

    def test_plan_hops_orders_explicit_and_same_host_handoffs(self) -> None:
        start = datetime.fromisoformat("2026-01-08T12:00:00+00:00")
        parent_hop = ("mail-a", "QUEUE-A")
        child_hop = ("mail-b.example.com", "QUEUE-B")
        inferred_hop = ("mail-b", "QUEUE-C")
        handoff_log = LogEntry(
            datetime=start.isoformat(),
            hostname=parent_hop[0],
            service="postfix/smtp",
            mail_id=parent_hop[1],
            message="status=sent",
            queued_as=child_hop[1],
            relay_host="mail-b",
        )
        prepared = {
            parent_hop: PreparedHop(
                logs=[handoff_log],
                delivery_records=[],
                raw_start=start,
                raw_end=start,
            ),
            child_hop: PreparedHop(
                logs=[],
                delivery_records=[],
                raw_start=start + timedelta(seconds=1),
                raw_end=start + timedelta(seconds=1),
            ),
            inferred_hop: PreparedHop(
                logs=[],
                delivery_records=[],
                raw_start=start + timedelta(seconds=2),
                raw_end=start + timedelta(seconds=2),
            ),
        }

        placements, links, parents, ordered = _plan_hops(
            "message@example.com", prepared
        )

        handoff = (parent_hop, handoff_log)
        self.assertEqual(
            placements[child_hop],
            HopPlacement(handoffs=(handoff,), explicit_handoff=True),
        )
        self.assertEqual(
            placements[inferred_hop],
            HopPlacement(handoffs=(handoff,), explicit_handoff=False),
        )
        self.assertEqual(links, {child_hop: handoff})
        self.assertEqual(parents, {child_hop: parent_hop})
        self.assertLess(ordered.index(parent_hop), ordered.index(child_hop))
        self.assertLess(ordered.index(parent_hop), ordered.index(inferred_hop))

    def test_plan_hops_attaches_cycles_to_root(self) -> None:
        start = datetime.fromisoformat("2026-01-08T12:00:00+00:00")
        hop_a = ("mail-a", "QUEUE-A")
        hop_b = ("mail-b", "QUEUE-B")
        log_a = LogEntry(
            datetime=start.isoformat(),
            hostname=hop_a[0],
            service="postfix/smtp",
            mail_id=hop_a[1],
            message="status=sent",
            queued_as=hop_b[1],
            relay_host=hop_b[0],
        )
        log_b = LogEntry(
            datetime=start.isoformat(),
            hostname=hop_b[0],
            service="postfix/smtp",
            mail_id=hop_b[1],
            message="status=sent",
            queued_as=hop_a[1],
            relay_host=hop_a[0],
        )
        prepared = {
            hop_a: PreparedHop([log_a], [], start, start),
            hop_b: PreparedHop([log_b], [], start, start),
        }

        with self.assertLogs("mailtrace", level="WARNING"):
            placements, links, parents, ordered = _plan_hops(
                "cycle@example.com", prepared
            )

        self.assertEqual(links, {})
        self.assertEqual(parents, {})
        self.assertEqual(ordered, [hop_a, hop_b])
        self.assertEqual(
            placements,
            {
                hop_a: HopPlacement((), None),
                hop_b: HopPlacement((), None),
            },
        )

    def test_calculate_hop_timing_uses_handoff_stage_boundary(self) -> None:
        start = datetime.fromisoformat("2026-01-08T12:00:00+00:00")
        parent_hop = ("mail-a", "QUEUE-A")
        child_hop = ("mail-b", "QUEUE-B")
        handoff_log = LogEntry(
            datetime=start.isoformat(),
            hostname=parent_hop[0],
            service="postfix/smtp",
            mail_id=parent_hop[1],
            message="status=sent",
        )
        delays = DelayInfo(
            before_qmgr=1,
            in_qmgr=0,
            conn_setup=2,
            transmission=3,
        )
        prepared = {
            parent_hop: PreparedHop(
                logs=[handoff_log],
                delivery_records=[
                    DeliveryRecord(delays, start, None, handoff_log)
                ],
                raw_start=start,
                raw_end=start + timedelta(seconds=6.000002),
            ),
            child_hop: PreparedHop(
                logs=[],
                delivery_records=[],
                raw_start=start + timedelta(seconds=10),
                raw_end=start + timedelta(seconds=11),
            ),
        }

        offsets, bounds = _calculate_hop_timing(
            [parent_hop, child_hop],
            prepared,
            {child_hop: (parent_hop, handoff_log)},
            {child_hop: parent_hop},
        )

        expected_start = start + timedelta(
            seconds=3 + MIN_SPAN_DURATION_SECONDS
        )
        self.assertEqual(bounds[child_hop][0], expected_start)
        self.assertEqual(
            offsets[child_hop],
            expected_start - prepared[child_hop].raw_start,
        )

    def test_calculate_hop_timing_clamps_log_handoff(self) -> None:
        start = datetime.fromisoformat("2026-01-08T12:00:00+00:00")
        parent_hop = ("mail-a", "QUEUE-A")
        child_hop = ("mail-b", "QUEUE-B")
        handoff_log = LogEntry(
            datetime=(start - timedelta(seconds=1)).isoformat(),
            hostname=parent_hop[0],
            service="postfix/smtp",
            mail_id=parent_hop[1],
            message="status=sent",
        )
        prepared = {
            parent_hop: PreparedHop([handoff_log], [], start, start),
            child_hop: PreparedHop(
                [],
                [],
                start + timedelta(seconds=10),
                start + timedelta(seconds=11),
            ),
        }

        _, bounds = _calculate_hop_timing(
            [parent_hop, child_hop],
            prepared,
            {child_hop: (parent_hop, handoff_log)},
            {child_hop: parent_hop},
        )

        self.assertEqual(
            bounds[child_hop][0],
            start + timedelta(seconds=MIN_SPAN_DURATION_SECONDS),
        )


if __name__ == "__main__":
    unittest.main()
