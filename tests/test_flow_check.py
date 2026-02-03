"""Tests for flow check feature."""

from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from mailtrace.__main__ import cli
from mailtrace.config import Config, Method
from mailtrace.flow_check import (
    FlowCheckResult,
    FlowStatus,
    MailFlow,
    check_cluster_flow,
    classify_terminal_state,
    do_trace_all,
    find_inbound_mails,
    is_inbound_mail,
    is_out_of_time_window,
)
from mailtrace.models import LogEntry

# --- Task 1: Data Models ---


class TestFlowCheckDataModels:
    def test_flow_status_values(self):
        assert FlowStatus.COMPLETE.value == "complete"
        assert FlowStatus.PROBLEMATIC.value == "problematic"

    def test_mail_flow_complete(self):
        flow = MailFlow(
            inbound_mail_id="ABC123",
            inbound_host="mx1.example.com",
            source="external.com",
            status=FlowStatus.COMPLETE,
            terminal_state="delivered_locally",
            branches=1,
        )
        assert flow.status == FlowStatus.COMPLETE

    def test_mail_flow_problematic(self):
        flow = MailFlow(
            inbound_mail_id="DEF456",
            inbound_host="mx1.example.com",
            source="sender.org",
            status=FlowStatus.PROBLEMATIC,
            terminal_state="incomplete",
            last_seen_host="mx2.example.com",
            last_seen_mail_id="GHI789",
        )
        assert flow.status == FlowStatus.PROBLEMATIC
        assert flow.last_seen_host == "mx2.example.com"

    def test_flow_check_result_to_dict(self):
        result = FlowCheckResult(
            cluster="mx-cluster",
            time="2025-01-15 10:00:00",
            time_range="1h",
            keywords=None,
            out_of_window_mail_ids=[],
            summary={
                "total_inbound": 0,
                "complete": 0,
                "problematic": 0,
            },
            complete_flows=[],
            problematic_flows=[],
        )
        d = result.to_dict()
        assert d["cluster"] == "mx-cluster"
        assert d["summary"]["total_inbound"] == 0


# --- Task 2: Inbound Mail Detection ---


class TestIsInboundMail:
    def test_external_client_is_inbound(self):
        cluster_hosts = ["mx1.example.com", "mx1", "10.0.0.1"]
        msg = "client=external.com[1.2.3.4]"
        assert is_inbound_mail(msg, cluster_hosts) is True

    def test_cluster_member_hostname_not_inbound(self):
        cluster_hosts = ["mx1.example.com", "mx1", "10.0.0.1"]
        msg = "client=mx1.example.com[10.0.0.1]"
        assert is_inbound_mail(msg, cluster_hosts) is False

    def test_cluster_member_ip_not_inbound(self):
        cluster_hosts = ["mx2.example.com", "mx2", "10.0.0.2"]
        msg = "client=unknown[10.0.0.2]"
        assert is_inbound_mail(msg, cluster_hosts) is False

    def test_no_client_field_returns_false(self):
        cluster_hosts = ["mx1.example.com"]
        msg = "connect from external.com[1.2.3.4]"
        assert is_inbound_mail(msg, cluster_hosts) is False

    def test_localhost_not_inbound(self):
        cluster_hosts = ["mx1.example.com"]
        msg = "client=localhost[127.0.0.1]"
        assert is_inbound_mail(msg, cluster_hosts) is False


# --- Task 3: Find Inbound Mails ---


class TestFindInboundMails:
    def test_finds_inbound_mail_ids(self):
        cluster_hosts = ["mx1.example.com", "mx1", "10.0.0.1"]
        logs = [
            LogEntry(
                datetime="2025-01-15T10:00:00",
                hostname="mx1.example.com",
                service="postfix/smtpd",
                mail_id="ABC123",
                message="client=external.com[1.2.3.4]",
            ),
            LogEntry(
                datetime="2025-01-15T10:00:01",
                hostname="mx1.example.com",
                service="postfix/cleanup",
                mail_id="ABC123",
                message="message-id=<test@example.com>",
            ),
        ]
        result = find_inbound_mails(logs, cluster_hosts)
        assert "ABC123" in result
        assert result["ABC123"]["host"] == "mx1.example.com"
        assert result["ABC123"]["source"] == "external.com"

    def test_ignores_internal_relay(self):
        cluster_hosts = [
            "mx1.example.com",
            "mx1",
            "mx2.example.com",
            "mx2",
            "10.0.0.1",
            "10.0.0.2",
        ]
        logs = [
            LogEntry(
                datetime="2025-01-15T10:00:00",
                hostname="mx2.example.com",
                service="postfix/smtpd",
                mail_id="DEF456",
                message="client=mx1.example.com[10.0.0.1]",
            ),
        ]
        result = find_inbound_mails(logs, cluster_hosts)
        assert "DEF456" not in result

    def test_no_smtpd_entries_returns_empty(self):
        cluster_hosts = ["mx1.example.com"]
        logs = [
            LogEntry(
                datetime="2025-01-15T10:00:00",
                hostname="mx1.example.com",
                service="postfix/smtp",
                mail_id="GHI789",
                message="to=<user@example.com>, status=sent",
            ),
        ]
        result = find_inbound_mails(logs, cluster_hosts)
        assert result == {}


# --- Task 4: do_trace_all ---


class TestDoTraceAll:
    def test_returns_all_relay_results(self):
        mock_agg = MagicMock()
        mock_agg.query_by.return_value = [
            LogEntry(
                datetime="2025-01-15T10:00:00",
                hostname="mx1.example.com",
                service="postfix/smtp",
                mail_id="ABC123",
                message=(
                    "to=<user1@other.com>, "
                    "relay=other.com[5.5.5.5]:25, "
                    "status=sent (250 2.0.0 Ok: queued as DEF456)"
                ),
            ),
            LogEntry(
                datetime="2025-01-15T10:00:01",
                hostname="mx1.example.com",
                service="postfix/smtp",
                mail_id="ABC123",
                message=(
                    "to=<user2@other.com>, "
                    "relay=other.com[5.5.5.5]:25, "
                    "status=sent (250 2.0.0 Ok: queued as GHI789)"
                ),
            ),
        ]
        results = do_trace_all("ABC123", mock_agg)
        assert len(results) == 2
        assert results[0].mail_id == "DEF456"
        assert results[1].mail_id == "GHI789"

    def test_returns_empty_when_no_relays(self):
        mock_agg = MagicMock()
        mock_agg.query_by.return_value = [
            LogEntry(
                datetime="2025-01-15T10:00:00",
                hostname="mx1.example.com",
                service="postfix/cleanup",
                mail_id="ABC123",
                message="message-id=<test@example.com>",
            ),
        ]
        results = do_trace_all("ABC123", mock_agg)
        assert results == []


# --- Task 5: Terminal State Classification ---


class TestClassifyTerminalState:
    def test_local_delivery_is_complete(self):
        logs = [
            LogEntry(
                datetime="2025-01-15T10:00:00",
                hostname="mx1.example.com",
                service="postfix/local",
                mail_id="ABC123",
                message="to=<user@localhost>, relay=local, "
                "status=sent (delivered to mailbox)",
            ),
        ]
        status, reason = classify_terminal_state(
            logs, cluster_hosts=["mx1.example.com"]
        )
        assert status == FlowStatus.COMPLETE
        assert reason == "delivered_locally"

    def test_virtual_delivery_is_complete(self):
        logs = [
            LogEntry(
                datetime="2025-01-15T10:00:00",
                hostname="mx1.example.com",
                service="postfix/virtual",
                mail_id="ABC123",
                message="to=<user@example.com>, relay=virtual, "
                "status=delivered (delivered to maildir)",
            ),
        ]
        status, reason = classify_terminal_state(
            logs, cluster_hosts=["mx1.example.com"]
        )
        assert status == FlowStatus.COMPLETE
        assert reason == "delivered_locally"

    def test_relay_to_external_is_complete(self):
        logs = [
            LogEntry(
                datetime="2025-01-15T10:00:00",
                hostname="mx1.example.com",
                service="postfix/smtp",
                mail_id="ABC123",
                message="to=<user@other.com>, "
                "relay=other.com[5.5.5.5]:25, "
                "status=sent (250 Ok: queued as DEF456)",
            ),
        ]
        status, reason = classify_terminal_state(
            logs,
            cluster_hosts=["mx1.example.com", "10.0.0.1"],
        )
        assert status == FlowStatus.COMPLETE
        assert reason == "relayed_out"

    def test_relay_to_cluster_member_not_terminal(self):
        logs = [
            LogEntry(
                datetime="2025-01-15T10:00:00",
                hostname="mx1.example.com",
                service="postfix/smtp",
                mail_id="ABC123",
                message="to=<user@example.com>, "
                "relay=mx2.example.com[10.0.0.2]:25, "
                "status=sent (250 Ok: queued as DEF456)",
            ),
        ]
        status, reason = classify_terminal_state(
            logs,
            cluster_hosts=[
                "mx1.example.com",
                "mx2.example.com",
                "10.0.0.1",
                "10.0.0.2",
            ],
        )
        assert status == FlowStatus.PROBLEMATIC
        assert reason == "internal_relay"

    def test_bounce_is_problematic(self):
        logs = [
            LogEntry(
                datetime="2025-01-15T10:00:00",
                hostname="mx1.example.com",
                service="postfix/bounce",
                mail_id="ABC123",
                message="sender non-delivery notification: DEF456",
            ),
        ]
        status, reason = classify_terminal_state(
            logs, cluster_hosts=["mx1.example.com"]
        )
        assert status == FlowStatus.PROBLEMATIC
        assert reason == "bounced"

    def test_no_terminal_entries_is_problematic(self):
        logs = [
            LogEntry(
                datetime="2025-01-15T10:00:00",
                hostname="mx1.example.com",
                service="postfix/cleanup",
                mail_id="ABC123",
                message="message-id=<test@example.com>",
            ),
        ]
        status, reason = classify_terminal_state(
            logs, cluster_hosts=["mx1.example.com"]
        )
        assert status == FlowStatus.PROBLEMATIC
        assert reason == "incomplete"


# --- Task 6: Out-of-Window Detection ---


class TestOutOfWindowDetection:
    def test_within_window(self):
        assert (
            is_out_of_time_window(
                "2025-01-15T10:30:00",
                "2025-01-15 10:00:00",
                "1h",
            )
            is False
        )

    def test_outside_window(self):
        assert (
            is_out_of_time_window(
                "2025-01-15T12:00:00",
                "2025-01-15 10:00:00",
                "1h",
            )
            is True
        )

    def test_at_boundary(self):
        assert (
            is_out_of_time_window(
                "2025-01-15T09:00:00",
                "2025-01-15 10:00:00",
                "1h",
            )
            is False
        )


# --- Task 7: Core Orchestrator ---


class TestCheckClusterFlow:
    @pytest.fixture
    def mock_config(self):
        config = MagicMock(spec=Config)
        config.method = Method.SSH
        config.domain = "example.com"
        config.cluster_to_hosts.return_value = [
            "mx1.example.com",
            "mx1",
            "10.0.0.1",
            "mx2.example.com",
            "mx2",
            "10.0.0.2",
        ]
        return config

    def test_empty_logs_returns_zero_inbound(self, mock_config):
        mock_agg_cls = MagicMock()
        mock_agg = MagicMock()
        mock_agg.query_by.return_value = []
        mock_agg_cls.return_value = mock_agg

        result = check_cluster_flow(
            config=mock_config,
            aggregator_class=mock_agg_cls,
            cluster="mx-cluster",
            time="2025-01-15 10:00:00",
            time_range="1h",
        )
        assert isinstance(result, FlowCheckResult)
        assert result.summary["total_inbound"] == 0

    def test_local_delivery_is_complete(self, mock_config):
        smtpd = LogEntry(
            datetime="2025-01-15T10:00:00",
            hostname="mx1.example.com",
            service="postfix/smtpd",
            mail_id="ABC123",
            message="client=external.com[1.2.3.4]",
        )
        local = LogEntry(
            datetime="2025-01-15T10:00:01",
            hostname="mx1.example.com",
            service="postfix/local",
            mail_id="ABC123",
            message="to=<user@localhost>, relay=local, "
            "status=sent (delivered to mailbox)",
        )
        mock_agg_cls = MagicMock()
        mock_agg = MagicMock()
        mock_agg.query_by.return_value = [smtpd, local]
        mock_agg_cls.return_value = mock_agg

        result = check_cluster_flow(
            config=mock_config,
            aggregator_class=mock_agg_cls,
            cluster="mx-cluster",
            time="2025-01-15 10:00:00",
            time_range="1h",
        )
        assert result.summary["total_inbound"] == 1
        assert result.summary["complete"] == 1
        assert len(result.complete_flows) == 1

    def test_cluster_not_found_raises(self, mock_config):
        mock_config.cluster_to_hosts.return_value = None
        with pytest.raises(ValueError, match="not found"):
            check_cluster_flow(
                config=mock_config,
                aggregator_class=MagicMock(),
                cluster="bad",
                time="2025-01-15 10:00:00",
                time_range="1h",
            )

    def test_intra_cluster_relay_traced_to_terminal(self, mock_config):
        """Mail relayed within cluster is traced to terminal."""
        smtpd = LogEntry(
            datetime="2025-01-15T10:00:00",
            hostname="mx1.example.com",
            service="postfix/smtpd",
            mail_id="ABC123",
            message="client=external.com[1.2.3.4]",
        )
        relay = LogEntry(
            datetime="2025-01-15T10:00:01",
            hostname="mx1.example.com",
            service="postfix/smtp",
            mail_id="ABC123",
            message=(
                "to=<user@example.com>, "
                "relay=mx2.example.com[10.0.0.2]:25, "
                "status=sent (250 Ok: queued as DEF456)"
            ),
        )
        local = LogEntry(
            datetime="2025-01-15T10:00:02",
            hostname="mx2.example.com",
            service="postfix/local",
            mail_id="DEF456",
            message="to=<user@localhost>, relay=local, "
            "status=sent (delivered to mailbox)",
        )

        call_counts: dict[str, int] = {}

        def make_agg(host, config):
            agg = MagicMock()
            call_counts.setdefault(host, 0)
            call_counts[host] += 1
            if host == "mx1.example.com":
                agg.query_by.return_value = [smtpd, relay]
            elif host == "mx2.example.com":
                if call_counts[host] == 1:
                    # Initial time-range query during host scan
                    agg.query_by.return_value = []
                else:
                    # Subsequent queries (mail_id lookup)
                    agg.query_by.return_value = [local]
            else:
                agg.query_by.return_value = []
            return agg

        mock_agg_cls = MagicMock(side_effect=make_agg)

        result = check_cluster_flow(
            config=mock_config,
            aggregator_class=mock_agg_cls,
            cluster="mx-cluster",
            time="2025-01-15 10:00:00",
            time_range="1h",
        )
        assert result.summary["total_inbound"] == 1
        assert result.summary["complete"] == 1


# --- Task 9: CLI Command ---


class TestFlowCheckCLI:
    def test_command_exists(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["flow-check", "--help"])
        assert result.exit_code == 0
        assert "cluster" in result.output

    def test_requires_cluster(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["flow-check"])
        assert result.exit_code != 0

    @patch("mailtrace.__main__.load_config")
    @patch("mailtrace.__main__.select_aggregator")
    @patch("mailtrace.__main__.check_cluster_flow")
    def test_invokes_check(self, mock_check, mock_select, mock_load):
        mock_load.return_value = MagicMock()
        mock_select.return_value = MagicMock()
        mock_check.return_value = FlowCheckResult(
            cluster="mx-cluster",
            time="2025-01-15 10:00:00",
            time_range="1h",
            keywords=None,
            out_of_window_mail_ids=[],
            summary={
                "total_inbound": 0,
                "complete": 0,
                "problematic": 0,
            },
            complete_flows=[],
            problematic_flows=[],
        )

        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "flow-check",
                "--cluster",
                "mx-cluster",
                "--time",
                "2025-01-15 10:00:00",
                "--time-range",
                "1h",
            ],
        )
        assert result.exit_code == 0
        mock_check.assert_called_once()
