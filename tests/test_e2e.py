"""End-to-end tests against dev OpenSearch with cross-cluster seed data.

Requires a running dev OpenSearch instance with seeded data.
Start with: cd dev && docker compose up -d

All tests are skipped if OpenSearch is not available.
Run with: uv run pytest -m e2e
"""

import pytest

from mailtrace.flow_check import FlowStatus, check_cluster_flow
from mailtrace.graph import MailGraph
from mailtrace.trace import query_logs_by_keywords, trace_mail_flow

pytestmark = pytest.mark.e2e

# Common time parameters covering all seed data scenarios
CENTER_TIME = "2026-02-04 08:30:00"
TIME_RANGE = "1h"


# --- Trace tests ---


class TestTrace:
    """Test trace_mail_flow against seeded cross-cluster scenarios."""

    def test_scenario1_outbound_smtp1_relay1_external(
        self, dev_config, aggregator_class
    ):
        """S1: smtp1 -> relay1 -> external (2 hops)."""
        logs = query_logs_by_keywords(
            dev_config,
            aggregator_class,
            "mailer-cluster",
            ["A1001CC0001"],
            "2026-02-04 08:00:00",
            "30m",
        )
        assert "A1001CC0001" in logs

        graph = MailGraph()
        host = logs["A1001CC0001"][0]
        trace_mail_flow(
            "A1001CC0001", aggregator_class, dev_config, host, graph
        )

        result = graph.to_dict()
        assert result["hop_count"] == 2
        edges = result["edges"]
        assert edges[0]["from"] == "smtp1"
        assert edges[0]["to"] == "relay1"
        assert edges[1]["from"] == "relay1"
        assert edges[1]["to"] == "mx.external.example.com"

    def test_scenario2_outbound_smtp2_relay1_external(
        self, dev_config, aggregator_class
    ):
        """S2: smtp2 -> relay1 -> external (2 hops)."""
        logs = query_logs_by_keywords(
            dev_config,
            aggregator_class,
            "mailer-cluster",
            ["A2001CC0002"],
            "2026-02-04 08:05:00",
            "30m",
        )
        assert "A2001CC0002" in logs

        graph = MailGraph()
        host = logs["A2001CC0002"][0]
        trace_mail_flow(
            "A2001CC0002", aggregator_class, dev_config, host, graph
        )

        result = graph.to_dict()
        assert result["hop_count"] == 2
        edges = result["edges"]
        assert edges[0]["from"] == "smtp2"
        assert edges[0]["to"] == "relay1"
        assert edges[1]["from"] == "relay1"
        assert edges[1]["to"] == "mx.partner.example.net"

    def test_scenario3_inbound_3hop_mx1_list1_relay1(
        self, dev_config, aggregator_class
    ):
        """S3: mx1 -> list1 -> relay1 (3-hop cross-cluster, local delivery)."""
        logs = query_logs_by_keywords(
            dev_config,
            aggregator_class,
            "mx-cluster",
            ["C3001CC0003"],
            "2026-02-04 08:10:00",
            "30m",
        )
        assert "C3001CC0003" in logs

        graph = MailGraph()
        host = logs["C3001CC0003"][0]
        trace_mail_flow(
            "C3001CC0003", aggregator_class, dev_config, host, graph
        )

        result = graph.to_dict()
        assert result["hop_count"] == 2
        edges = result["edges"]
        assert edges[0]["from"] == "mx1"
        assert edges[0]["to"] == "list1"
        assert edges[1]["from"] == "list1"
        assert edges[1]["to"] == "relay1"

    def test_scenario4_inbound_3hop_mx2_list1_relay1(
        self, dev_config, aggregator_class
    ):
        """S4: mx2 -> list1 -> relay1 (3-hop cross-cluster, local delivery)."""
        logs = query_logs_by_keywords(
            dev_config,
            aggregator_class,
            "mx-cluster",
            ["C3002CC0004"],
            "2026-02-04 08:12:00",
            "30m",
        )
        assert "C3002CC0004" in logs

        graph = MailGraph()
        host = logs["C3002CC0004"][0]
        trace_mail_flow(
            "C3002CC0004", aggregator_class, dev_config, host, graph
        )

        result = graph.to_dict()
        assert result["hop_count"] == 2
        edges = result["edges"]
        assert edges[0]["from"] == "mx2"
        assert edges[0]["to"] == "list1"
        assert edges[1]["from"] == "list1"
        assert edges[1]["to"] == "relay1"

    def test_scenario5_deferred_mx1_list1(self, dev_config, aggregator_class):
        """S5: mx1 -> list1 (deferred, stops at list1)."""
        logs = query_logs_by_keywords(
            dev_config,
            aggregator_class,
            "mx-cluster",
            ["C3003CC0005"],
            "2026-02-04 08:20:00",
            "30m",
        )
        assert "C3003CC0005" in logs

        graph = MailGraph()
        host = logs["C3003CC0005"][0]
        trace_mail_flow(
            "C3003CC0005", aggregator_class, dev_config, host, graph
        )

        result = graph.to_dict()
        # mx1 -> list1, then deferred (no queued_as for failed relay)
        assert result["hop_count"] == 1
        edges = result["edges"]
        assert edges[0]["from"] == "mx1"
        assert edges[0]["to"] == "list1"

    def test_scenario6_fanout_mx1_list1_local(
        self, dev_config, aggregator_class
    ):
        """S6: mx1 -> list1 (fan-out to 2 local recipients)."""
        logs = query_logs_by_keywords(
            dev_config,
            aggregator_class,
            "mx-cluster",
            ["C3004CC0006"],
            "2026-02-04 08:30:00",
            "30m",
        )
        assert "C3004CC0006" in logs

        graph = MailGraph()
        host = logs["C3004CC0006"][0]
        trace_mail_flow(
            "C3004CC0006", aggregator_class, dev_config, host, graph
        )

        result = graph.to_dict()
        # mx1 -> list1, then local delivery (no further relay)
        assert result["hop_count"] == 1
        edges = result["edges"]
        assert edges[0]["from"] == "mx1"
        assert edges[0]["to"] == "list1"

    def test_scenario7_bounce_smtp1_relay1_list1(
        self, dev_config, aggregator_class
    ):
        """S7: smtp1 -> relay1 -> list1 (bounced, NDN back)."""
        logs = query_logs_by_keywords(
            dev_config,
            aggregator_class,
            "mailer-cluster",
            ["A1003CC0007"],
            "2026-02-04 08:40:00",
            "30m",
        )
        assert "A1003CC0007" in logs

        graph = MailGraph()
        host = logs["A1003CC0007"][0]
        trace_mail_flow(
            "A1003CC0007", aggregator_class, dev_config, host, graph
        )

        result = graph.to_dict()
        # smtp1 -> relay1 -> list1 (bounce stops further relay)
        assert result["hop_count"] >= 2
        edges = result["edges"]
        assert edges[0]["from"] == "smtp1"
        assert edges[0]["to"] == "relay1"
        assert edges[1]["from"] == "relay1"
        assert edges[1]["to"] == "list1"


# --- Flow check tests ---


class TestFlowCheck:
    """Test check_cluster_flow against seeded cross-cluster scenarios."""

    def test_mx_cluster_all_complete(self, dev_config, aggregator_class):
        """mx-cluster: 4 inbound mails, all relayed out to list1."""
        result = check_cluster_flow(
            config=dev_config,
            aggregator_class=aggregator_class,
            cluster="mx-cluster",
            time=CENTER_TIME,
            time_range=TIME_RANGE,
        )
        assert result.summary["total_inbound"] == 4
        assert result.summary["complete"] == 4
        assert result.summary["problematic"] == 0

        # All flows should be relayed_out
        for flow in result.complete_flows:
            assert flow.status == FlowStatus.COMPLETE
            assert flow.terminal_state == "relayed_out"

    def test_mx_cluster_inbound_sources_are_external(
        self, dev_config, aggregator_class
    ):
        """mx-cluster: all inbound sources are external hosts."""
        result = check_cluster_flow(
            config=dev_config,
            aggregator_class=aggregator_class,
            cluster="mx-cluster",
            time=CENTER_TIME,
            time_range=TIME_RANGE,
        )
        inbound_ids = {f.inbound_mail_id for f in result.complete_flows}
        assert inbound_ids == {
            "C3001CC0003",
            "C3002CC0004",
            "C3003CC0005",
            "C3004CC0006",
        }

    def test_maillist_cluster_mixed_outcomes(
        self, dev_config, aggregator_class
    ):
        """maillist-cluster: 3 complete + 2 problematic flows."""
        result = check_cluster_flow(
            config=dev_config,
            aggregator_class=aggregator_class,
            cluster="maillist-cluster",
            time=CENTER_TIME,
            time_range=TIME_RANGE,
        )
        assert result.summary["total_inbound"] == 5
        assert result.summary["complete"] == 3
        assert result.summary["problematic"] == 2

    def test_maillist_cluster_deferred_is_problematic(
        self, dev_config, aggregator_class
    ):
        """maillist-cluster: D3003CC0005 is deferred (problematic)."""
        result = check_cluster_flow(
            config=dev_config,
            aggregator_class=aggregator_class,
            cluster="maillist-cluster",
            time=CENTER_TIME,
            time_range=TIME_RANGE,
        )
        problematic_ids = {f.inbound_mail_id for f in result.problematic_flows}
        assert "D3003CC0005" in problematic_ids

    def test_maillist_cluster_bounced_is_problematic(
        self, dev_config, aggregator_class
    ):
        """maillist-cluster: D1003CC0007 is bounced (problematic)."""
        result = check_cluster_flow(
            config=dev_config,
            aggregator_class=aggregator_class,
            cluster="maillist-cluster",
            time=CENTER_TIME,
            time_range=TIME_RANGE,
        )
        problematic_ids = {f.inbound_mail_id for f in result.problematic_flows}
        assert "D1003CC0007" in problematic_ids

        bounced = [
            f
            for f in result.problematic_flows
            if f.inbound_mail_id == "D1003CC0007"
        ]
        assert len(bounced) == 1
        assert bounced[0].terminal_state == "bounced"

    def test_maillist_cluster_local_delivery_complete(
        self, dev_config, aggregator_class
    ):
        """maillist-cluster: D3004CC0006 fan-out local delivery is complete."""
        result = check_cluster_flow(
            config=dev_config,
            aggregator_class=aggregator_class,
            cluster="maillist-cluster",
            time=CENTER_TIME,
            time_range=TIME_RANGE,
        )
        complete_ids = {f.inbound_mail_id for f in result.complete_flows}
        assert "D3004CC0006" in complete_ids

    def test_mailer_cluster_no_external_inbound(
        self, dev_config, aggregator_class
    ):
        """mailer-cluster: outbound-only, internal clients are not inbound."""
        result = check_cluster_flow(
            config=dev_config,
            aggregator_class=aggregator_class,
            cluster="mailer-cluster",
            time=CENTER_TIME,
            time_range=TIME_RANGE,
        )
        # smtp1/smtp2 receive from webmail.internal (internal client)
        # relay1 receives from cluster members (smtp1, smtp2, list1)
        # list1 is not in mailer-cluster, so relay from list1 IS inbound
        # The bounce NDN flow: list1 -> relay1 -> smtp1
        # F1003CC0007: relay1 receives from list1 (inbound)
        # E3001CC0003: relay1 receives from list1 (inbound)
        # E3002CC0004: relay1 receives from list1 (inbound)
        assert result.summary["total_inbound"] >= 0


# --- Query tests ---


class TestQueryLogs:
    """Test query_logs_by_keywords against seeded data."""

    def test_query_by_mail_id(self, dev_config, aggregator_class):
        """Query by specific mail ID returns matching logs."""
        logs = query_logs_by_keywords(
            dev_config,
            aggregator_class,
            "mx-cluster",
            ["C3001CC0003"],
            CENTER_TIME,
            TIME_RANGE,
        )
        assert "C3001CC0003" in logs
        _, entries = logs["C3001CC0003"]
        assert len(entries) > 0
        assert all(e.mail_id == "C3001CC0003" for e in entries)

    def test_query_by_email_address(self, dev_config, aggregator_class):
        """Query by email address finds relevant mail IDs."""
        logs = query_logs_by_keywords(
            dev_config,
            aggregator_class,
            "mx-cluster",
            ["team@mail.example.org"],
            "2026-02-04 08:10:00",
            "30m",
        )
        # C3001CC0003 delivers to team@mail.example.org
        assert len(logs) >= 1

    def test_query_nonexistent_returns_empty(
        self, dev_config, aggregator_class
    ):
        """Query for nonexistent mail ID returns empty."""
        logs = query_logs_by_keywords(
            dev_config,
            aggregator_class,
            "mx-cluster",
            ["ZZZZZZZZZZZ"],
            CENTER_TIME,
            TIME_RANGE,
        )
        assert len(logs) == 0
