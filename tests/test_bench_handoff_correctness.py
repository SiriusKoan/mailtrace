import unittest

from demo.docker.bench_handoff_correctness import (
    RawLog,
    ablate_message_ids,
    build_raw_delays,
    build_raw_graphs,
    build_scenarios,
    expected_graph_size,
    export_span_graphs,
    handoff_records,
    message_id_records,
    normalize_host,
    queue_id_from_message,
    received_hops,
    strip_handoff_evidence,
    strip_message_id_evidence,
)

from mailtrace.models import EventType, LogEntry
from mailtrace.parser import extract_next_mail_id
from mailtrace.tracing.query import group_logs_by_message_id


class ScenarioTest(unittest.TestCase):
    def test_builds_full_route_matrix_and_complex_cases(self) -> None:
        scenarios = build_scenarios(10025, [20025, 20026, 20027])
        by_name = {scenario.name: scenario for scenario in scenarios}

        self.assertEqual(len(scenarios), 16)
        self.assertEqual(
            set(by_name),
            {
                *(
                    f"mx-{number}-{alias}"
                    for number in range(1, 4)
                    for alias in ("single", "team")
                ),
                *(
                    f"mailer-{number}-{alias}"
                    for number in range(1, 4)
                    for alias in ("single", "team")
                ),
                "mx-multi-domain",
                "mailer-multi-domain",
                "parallel-shared-route",
                "revisit-origin",
            },
        )
        self.assertEqual(
            by_name["mx-1-single"].expected_routes,
            {
                "user1": (
                    "mx.example.com",
                    "mailer1.example.com",
                    "mailpolicy1.example.com",
                    "mailbox.example.com",
                )
            },
        )
        self.assertEqual(
            by_name["mailer-1-team"].expected_routes,
            {
                "user1": (
                    "mailer1.example.com",
                    "mailpolicy1.example.com",
                    "mailbox.example.com",
                ),
                "user2": (
                    "mailer1.example.com",
                    "mailpolicy1.example.com",
                    "mx.example.com",
                    "mailer2.example.com",
                    "mailpolicy2.example.com",
                    "mailbox.example.com",
                ),
            },
        )
        self.assertEqual(
            by_name["mx-multi-domain"].recipients,
            (
                "single@1.example.com",
                "alias-from-1@2.example.com",
            ),
        )
        self.assertEqual(
            by_name["mailer-multi-domain"].expected_routes,
            {
                "user1": (
                    "mailer1.example.com",
                    "mailpolicy1.example.com",
                    "mailbox.example.com",
                ),
                "user2": (
                    "mailer1.example.com",
                    "mailpolicy2.example.com",
                    "mailbox.example.com",
                ),
            },
        )
        self.assertEqual(
            by_name["parallel-shared-route"].expected_routes["user1"],
            by_name["parallel-shared-route"].expected_routes["user2"],
        )
        self.assertEqual(
            by_name["revisit-origin"].expected_routes,
            {
                "user1": (
                    "mx.example.com",
                    "mailer1.example.com",
                    "mailpolicy1.example.com",
                    "mx.example.com",
                    "mailer2.example.com",
                    "mailpolicy2.example.com",
                    "mx.example.com",
                    "mailer1.example.com",
                    "mailpolicy1.example.com",
                    "mailbox.example.com",
                )
            },
        )
        self.assertEqual(
            by_name["mx-1-team"].expected_users, ("user1", "user2")
        )
        self.assertEqual(expected_graph_size(by_name["mx-1-single"]), (4, 3))
        self.assertEqual(expected_graph_size(by_name["mx-1-team"]), (8, 7))
        self.assertEqual(
            expected_graph_size(by_name["mx-multi-domain"]), (7, 6)
        )
        self.assertEqual(
            expected_graph_size(by_name["mailer-multi-domain"]), (5, 4)
        )
        self.assertEqual(
            expected_graph_size(by_name["parallel-shared-route"]), (7, 6)
        )
        self.assertEqual(
            expected_graph_size(by_name["revisit-origin"]), (10, 9)
        )


class ReceivedTest(unittest.TestCase):
    def test_reads_received_headers_from_oldest_to_newest(self) -> None:
        raw_message = (
            b"Received: from mx.example.com by mailbox.example.com "
            b"with ESMTP id CCC333\r\n"
            b"Received: from client.example by mx.example.com "
            b"with ESMTP id AAA111\r\n"
            b"Received: from mailbox.example.com by mailbox.example.com "
            b"with LMTP id ignored\r\n"
            b"Message-ID: <case@test.example>\r\n"
            b"\r\nbody\r\n"
        )

        hops, errors = received_hops(raw_message)

        self.assertEqual(
            hops,
            [
                ("mx.example.com", "AAA111"),
                ("mailbox.example.com", "CCC333"),
            ],
        )
        self.assertEqual(errors, [])


class RawLogTest(unittest.TestCase):
    def test_normalizes_testbed_short_hostname(self) -> None:
        self.assertEqual(normalize_host("mx"), "mx.example.com")
        self.assertEqual(
            normalize_host("mailer1.example.com"), "mailer1.example.com"
        )

    def test_reads_exim_queue_id_after_timestamp(self) -> None:
        message = (
            "2026-09-26 21:14:57.804 1xASEz-000010-1B "
            "=> single@1.example.com"
        )

        self.assertEqual(queue_id_from_message(message), "1xASEz-000010-1B")

    def test_reads_postfix_short_and_long_queue_ids(self) -> None:
        for queue_id in ("ABC123", "4jC4nY4v8fznhZN", "4jC4nc392Yznw8J"):
            with self.subTest(queue_id=queue_id):
                self.assertEqual(
                    queue_id_from_message(
                        f"{queue_id}: message-id=<case@test.example>"
                    ),
                    queue_id,
                )

    def test_does_not_treat_status_text_as_a_queue_id(self) -> None:
        for message in (
            "warning: dict_nis_init: NIS domain name not set",
            "connect from client.example[192.0.2.1]",
            "message: no queue ID",
            "NOQUEUE: reject: RCPT from client.example",
        ):
            with self.subTest(message=message):
                self.assertIsNone(queue_id_from_message(message))

    def test_correlates_exim_message_id_without_angle_brackets(self) -> None:
        message_id = "case@test.example"
        logs = [
            RawLog(
                "mailer1.example.com",
                "exim4",
                "2026-09-26 21:14:57.379 1xASEz-000010-1B "
                "<= sender@test.example id=case@test.example",
                "2026-09-26T13:14:57Z",
                "1xASEz-000010-1B",
            ),
            RawLog(
                "mailer1.example.com",
                "exim4",
                "2026-09-26 21:14:57.804 1xASEz-000010-1B "
                "=> single@1.example.com H=mailpolicy1.example.com "
                '[192.0.2.2] C="250 Ok: queued as BBB222"',
                "2026-09-26T13:14:58Z",
                "1xASEz-000010-1B",
            ),
        ]

        nodes, edges = build_raw_graphs(logs, {message_id})[message_id]

        self.assertEqual(
            nodes,
            {
                ("mailer1.example.com", "1xASEz-000010-1B"),
                ("mailpolicy1.example.com", "BBB222"),
            },
        )
        self.assertEqual(
            edges,
            {
                (
                    ("mailer1.example.com", "1xASEz-000010-1B"),
                    ("mailpolicy1.example.com", "BBB222"),
                )
            },
        )

    def test_correlates_queue_ids_without_mailtrace_parser(self) -> None:
        message_id = "case@test.example"
        logs = [
            RawLog(
                "mx.example.com",
                "postfix/cleanup",
                "AAA111: message-id=<case@test.example>",
                "2026-01-01T00:00:00Z",
                "AAA111",
            ),
            RawLog(
                "mx.example.com",
                "postfix/smtp",
                "AAA111: to=<single@1.example.com>, "
                "relay=mailer1.example.com[192.0.2.1]:25, "
                "status=sent (250 OK id=1ABCDEF-123456-78)",
                "2026-01-01T00:00:01Z",
                "AAA111",
            ),
            RawLog(
                "mailer1.example.com",
                "exim4",
                "1ABCDEF-123456-78 => single@1.example.com "
                "H=mailpolicy1.example.com [192.0.2.2] "
                'C="250 2.0.0 Ok: queued as BBB222"',
                "2026-01-01T00:00:02Z",
                "1ABCDEF-123456-78",
            ),
        ]

        nodes, edges = build_raw_graphs(logs, {message_id})[message_id]

        self.assertEqual(
            edges,
            {
                (
                    ("mx.example.com", "AAA111"),
                    ("mailer1.example.com", "1ABCDEF-123456-78"),
                ),
                (
                    ("mailer1.example.com", "1ABCDEF-123456-78"),
                    ("mailpolicy1.example.com", "BBB222"),
                ),
            },
        )
        self.assertEqual(
            nodes,
            {
                ("mx.example.com", "AAA111"),
                ("mailer1.example.com", "1ABCDEF-123456-78"),
                ("mailpolicy1.example.com", "BBB222"),
            },
        )

    def test_parses_postfix_and_split_exim_delay_logs(self) -> None:
        message_id = "case@test.example"
        logs = [
            RawLog(
                "mx.example.com",
                "postfix/cleanup",
                "AAA111: message-id=<case@test.example>",
                "2026-01-01T00:00:00Z",
                "AAA111",
            ),
            RawLog(
                "mx.example.com",
                "postfix/smtp",
                "AAA111: relay=mailer1.example.com, status=sent "
                "(250 OK id=1ABCDEF-123456-78), "
                "delays=0.1/0.2/0.3/0.4",
                "2026-01-01T00:00:01Z",
                "AAA111",
            ),
            RawLog(
                "mailer1.example.com",
                "exim4",
                "1ABCDEF-123456-78 <= sender@test.example RT=0.05s",
                "2026-01-01T00:00:02Z",
                "1ABCDEF-123456-78",
            ),
            RawLog(
                "mailer1.example.com",
                "exim4",
                "1ABCDEF-123456-78 => user@test.example DT=0.20s",
                "2026-01-01T00:00:03Z",
                "1ABCDEF-123456-78",
            ),
            RawLog(
                "mailer1.example.com",
                "exim4",
                "1ABCDEF-123456-78 Completed QT=0.50s",
                "2026-01-01T00:00:04Z",
                "1ABCDEF-123456-78",
            ),
        ]

        delays = build_raw_delays(logs, {message_id})[message_id]

        self.assertEqual(
            delays[("mx.example.com", "AAA111")],
            [
                {
                    "before_qmgr": 0.1,
                    "in_qmgr": 0.2,
                    "conn_setup": 0.3,
                    "transmission": 0.4,
                }
            ],
        )
        self.assertEqual(
            delays[("mailer1.example.com", "1ABCDEF-123456-78")],
            [
                {
                    "receive_time": 0.05,
                    "queue_time": 0.25,
                    "deliver_time": 0.2,
                }
            ],
        )


class MembershipAblationTest(unittest.TestCase):
    def setUp(self) -> None:
        timestamp = "2026-01-01T00:00:00Z"
        self.groups = {}
        self.entry_hops = set()
        for number in (1, 2):
            message_id = f"case-{number}@test.example"
            # The same queue ID on different hosts belongs to different messages.
            host = f"mx{number}.example.com"
            exim_host = f"mailer{number}.example.com"
            exim_queue = f"1ABCDE-00000{number}-01"
            self.entry_hops.add((host, "AAA111"))
            self.groups[message_id] = [
                LogEntry(
                    timestamp,
                    host,
                    "postfix/cleanup",
                    "AAA111",
                    f"message-id=<{message_id}>",
                    event_type=EventType.MESSAGE_ID,
                ),
                LogEntry(
                    timestamp,
                    host,
                    "postfix/smtp",
                    "AAA111",
                    f"status=sent (250 OK id={exim_queue})",
                    queued_as=exim_queue,
                    relay_host=exim_host,
                ),
                LogEntry(
                    timestamp,
                    exim_host,
                    "exim4",
                    exim_queue,
                    f"<= sender@test.example id={message_id} RT=0.05s",
                ),
                *(
                    LogEntry(
                        timestamp,
                        exim_host,
                        "exim4",
                        exim_queue,
                        f'=> user@test.example C="250 queued as {queue_id}" DT=0.02s',
                        queued_as=queue_id,
                        relay_host=host,
                    )
                    for queue_id in ("BBB222", "CCC333")
                ),
                *(
                    LogEntry(
                        timestamp,
                        host,
                        "postfix/cleanup",
                        queue_id,
                        f"message-id=<{message_id}>",
                    )
                    for queue_id in ("BBB222", "CCC333")
                ),
            ]

    def test_removes_postfix_evidence_without_mutating_original(self) -> None:
        original = self.groups["case-1@test.example"][0]

        stripped = strip_message_id_evidence(original)

        self.assertEqual(stripped.message, "")
        self.assertEqual(stripped.mail_id, "AAA111")
        self.assertEqual(stripped.event_type, EventType.UNKNOWN)
        self.assertEqual(original.message, "message-id=<case-1@test.example>")
        self.assertEqual(original.event_type, EventType.MESSAGE_ID)

    def test_removes_exim_id_but_preserves_receive_time(self) -> None:
        original = self.groups["case-1@test.example"][2]

        stripped = strip_message_id_evidence(original)

        self.assertNotIn("id=", stripped.message)
        self.assertIn("<= sender@test.example", stripped.message)
        self.assertIn("RT=0.05s", stripped.message)
        self.assertEqual(stripped.mail_id, original.mail_id)

    def test_preserves_smtp_reply_ids_and_handoff_records(self) -> None:
        original = [log for logs in self.groups.values() for log in logs]

        stripped = [strip_message_id_evidence(log) for log in original]

        self.assertEqual(handoff_records(stripped), handoff_records(original))
        self.assertIn("250 OK id=1ABCDE-000001-01", stripped[1].message)
        self.assertEqual(len(stripped), len(original))

    def test_entrypoint_seed_resolves_reversed_branches_and_revisited_host(
        self,
    ) -> None:
        expected, seeds = ablate_message_ids(
            self.groups, self.entry_hops, keep_entrypoint=True
        )
        mixed_logs = [log for logs in expected.values() for log in logs][::-1]

        actual = group_logs_by_message_id(mixed_logs)

        self.assertEqual(len(seeds), 2)
        self.assertEqual(set(actual), set(expected))
        for message_id in expected:
            self.assertCountEqual(actual[message_id], expected[message_id])
        self.assertEqual(
            sum(
                strip_message_id_evidence(log).message != log.message
                for log in mixed_logs
            ),
            2,
        )

    def test_cached_mapping_starts_with_only_entrypoints(self) -> None:
        expected, seeds = ablate_message_ids(
            self.groups, self.entry_hops, keep_entrypoint=False
        )
        queue_mapping = {}
        group_logs_by_message_id(seeds, queue_mapping)
        self.assertEqual(
            set(queue_mapping), {("mx1", "AAA111"), ("mx2", "AAA111")}
        )
        mixed_logs = [log for logs in expected.values() for log in logs][::-1]

        actual = group_logs_by_message_id(mixed_logs, queue_mapping)

        for message_id in expected:
            self.assertCountEqual(actual[message_id], expected[message_id])
        self.assertEqual(len(queue_mapping), 8)
        self.assertTrue(
            all(
                strip_message_id_evidence(log).message == log.message
                for log in mixed_logs
            )
        )

    def test_all_evidence_removed_without_mapping_remains_unresolved(
        self,
    ) -> None:
        expected, _ = ablate_message_ids(
            self.groups, self.entry_hops, keep_entrypoint=False
        )
        mixed_logs = [log for logs in expected.values() for log in logs]

        self.assertEqual(group_logs_by_message_id(mixed_logs), {})

    def test_removes_postfix_handoff_tokens_and_cached_queue_id(self) -> None:
        for service, token in (
            ("postfix/smtp", "250 OK id=1ABCDE-000001-01"),
            ("postfix/smtp", "250 Ok: queued as BBB222"),
            ("postfix/local", "forwarded as CCC333"),
        ):
            with self.subTest(service=service, token=token):
                message = (
                    "to=<user@test.example>, delays=0.1/0.2/0.3/0.4, "
                    f"status=sent ({token})"
                )
                original = LogEntry(
                    "2026-01-01T00:00:00Z",
                    "mx",
                    service,
                    "AAA111",
                    message,
                    queued_as="BBB222",
                    relay_host="mailer1.example.com",
                )

                stripped = strip_handoff_evidence(original)

                self.assertIsNone(stripped.queued_as)
                self.assertIsNone(extract_next_mail_id(stripped))
                self.assertIn("to=<user@test.example>", stripped.message)
                self.assertIn("delays=0.1/0.2/0.3/0.4", stripped.message)
                self.assertEqual(stripped.mail_id, "AAA111")
                self.assertEqual(stripped.datetime, original.datetime)
                self.assertEqual(stripped.relay_host, original.relay_host)
                self.assertEqual(original.queued_as, "BBB222")
                self.assertEqual(original.message, message)

    def test_removes_exim_reply_id_and_preserves_acceptance_id(self) -> None:
        acceptance = self.groups["case-1@test.example"][2]
        delivery = LogEntry(
            acceptance.datetime,
            acceptance.hostname,
            "exim4",
            acceptance.mail_id,
            '=> user@test.example C="250 OK id=AAA111" DT=0.02s QT=0.20s',
            queued_as="AAA111",
        )

        stripped_acceptance = strip_handoff_evidence(acceptance)
        stripped_delivery = strip_handoff_evidence(delivery)

        self.assertEqual(stripped_acceptance.message, acceptance.message)
        self.assertNotIn("id=AAA111", stripped_delivery.message)
        self.assertIn("DT=0.02s QT=0.20s", stripped_delivery.message)
        self.assertIsNone(extract_next_mail_id(stripped_delivery))

    def test_handoff_ablation_preserves_message_ids_at_every_hop(self) -> None:
        original = [log for group in self.groups.values() for log in group]

        stripped = [strip_handoff_evidence(log) for log in original]

        self.assertEqual(
            message_id_records(stripped), message_id_records(original)
        )
        self.assertEqual(sum(message_id_records(stripped).values()), 8)
        self.assertEqual(handoff_records(stripped), {})
        self.assertEqual(len(stripped), len(original))

    def test_handoff_ablation_keeps_membership_and_nodes_without_edges(
        self,
    ) -> None:
        expected = {
            message_id: [strip_handoff_evidence(log) for log in group]
            for message_id, group in self.groups.items()
        }
        mixed_logs = [log for group in expected.values() for log in group][
            ::-1
        ]

        grouped = group_logs_by_message_id(mixed_logs)
        trace_count, graphs = export_span_graphs(grouped)

        self.assertEqual(trace_count, 2)
        self.assertEqual(set(grouped), set(expected))
        for message_id, group in expected.items():
            self.assertCountEqual(grouped[message_id], group)
            nodes, edges, attributes, _ = graphs[message_id]
            self.assertEqual(
                nodes,
                {(normalize_host(log.hostname), log.mail_id) for log in group},
            )
            self.assertEqual(edges, set())
            self.assertTrue(
                all(not row.get("email.next_queue_id") for row in attributes)
            )
            self.assertTrue(
                all(row["email.handoff"] != "explicit" for row in attributes)
            )


if __name__ == "__main__":
    unittest.main()
