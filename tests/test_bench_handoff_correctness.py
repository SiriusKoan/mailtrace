import unittest

from demo.docker.bench_handoff_correctness import (
    RawLog,
    build_raw_graphs,
    build_scenarios,
    expected_graph_size,
    normalize_host,
    queue_id_from_message,
    received_hops,
)


class ScenarioTest(unittest.TestCase):
    def test_builds_six_routes_with_two_entrypoint_types(self) -> None:
        scenarios = build_scenarios(10025, [20025, 20026, 20027])

        self.assertEqual(len(scenarios), 6)
        self.assertEqual(
            scenarios[0].expected_routes,
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
            scenarios[3].expected_routes,
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
        self.assertEqual(scenarios[1].expected_users, ("user1", "user2"))
        self.assertEqual(expected_graph_size(scenarios[0]), (4, 3))
        self.assertEqual(expected_graph_size(scenarios[1]), (8, 7))


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


if __name__ == "__main__":
    unittest.main()
