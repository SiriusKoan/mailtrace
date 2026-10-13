#!/usr/bin/env python3
"""Compare timestamp and mailtrace topology accuracy against a Received oracle."""

from __future__ import annotations

import argparse
import json
import sys
import unittest
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_PROJECT_ROOT))

from demo.docker import bench_handoff_correctness as oracle  # noqa: E402

from mailtrace.models import LogEntry  # noqa: E402

DEFAULT_RESULT = "demo/docker/bench_handoff_timestamp_result.json"
Graph = tuple[set[oracle.Hop], set[oracle.Edge]]

METRIC_DEFINITIONS = {
    "complete_topology_match_pct": "Messages whose nodes and edges exactly match the oracle.",
    "complete_node_match_pct": "Messages whose node sets exactly match the oracle.",
    "complete_edge_match_pct": "Messages whose edge sets exactly match the oracle.",
    "edge_precision_pct": "Micro-averaged TP / (TP + FP) across all messages.",
    "edge_recall_pct": "Micro-averaged TP / (TP + FN) across all messages.",
    "edge_f1_pct": "Micro-averaged 2TP / (2TP + FP + FN) across all messages.",
}


def received_oracle(
    item: oracle.SentMessage, copies: dict[str, bytes]
) -> Graph:
    """Merge recipient Received chains after validating mailbox evidence."""
    routes = {}
    nodes: set[oracle.Hop] = set()
    edges: set[oracle.Edge] = set()
    for user, raw_message in copies.items():
        hops, errors = oracle.received_hops(raw_message)
        if errors or not hops:
            raise oracle.InfrastructureError(
                f"{item.scenario.name}: incomplete Received headers for {user}"
            )
        routes[user] = tuple(host for host, _ in hops)
        nodes.update(hops)
        edges.update(zip(hops, hops[1:]))
    if routes != item.scenario.expected_routes:
        raise oracle.InfrastructureError(
            f"{item.scenario.name}: Received routes differ from the scenario manifest"
        )
    return nodes, edges


def timestamp_graph(logs: list[LogEntry]) -> Graph:
    """Build a linear topology from each queue hop's earliest raw log timestamp."""
    first_seen: dict[oracle.Hop, datetime] = {}
    for log in logs:
        if not log.hostname or not log.mail_id:
            continue
        hop = (oracle.normalize_host(log.hostname), log.mail_id)
        timestamp = datetime.fromisoformat(log.datetime.replace("Z", "+00:00"))
        first_seen[hop] = min(first_seen.get(hop, timestamp), timestamp)
    # Break ties by host and queue ID without consulting handoff evidence.
    ordered = sorted(first_seen, key=lambda hop: (first_seen[hop], hop))
    return set(ordered), set(zip(ordered, ordered[1:]))


def evaluate_graph(expected: Graph, actual: Graph | None) -> dict[str, Any]:
    """Score predictions against the oracle, counting missing traces as failures."""
    expected_nodes, expected_edges = expected
    nodes, edges = actual if actual is not None else (set(), set())
    nodes_match = actual is not None and nodes == expected_nodes
    edges_match = actual is not None and edges == expected_edges
    return {
        "trace_present": actual is not None,
        "nodes_match": nodes_match,
        "edges_match": edges_match,
        "topology_match": nodes_match and edges_match,
        "nodes": sorted(nodes),
        "edges": sorted(edges),
        "missing_nodes": sorted(expected_nodes - nodes),
        "extra_nodes": sorted(nodes - expected_nodes),
        "missing_edges": sorted(expected_edges - edges),
        "extra_edges": sorted(edges - expected_edges),
        "true_positive_edges": len(edges & expected_edges),
        "false_positive_edges": len(edges - expected_edges),
        "false_negative_edges": len(expected_edges - edges),
    }


def percentage(numerator: int, denominator: int) -> float:
    return round(numerator / denominator * 100, 6) if denominator else 0.0


def summarize(results: list[dict[str, Any]], method: str) -> dict[str, Any]:
    rows = [result[method] for result in results]
    tp = sum(row["true_positive_edges"] for row in rows)
    fp = sum(row["false_positive_edges"] for row in rows)
    fn = sum(row["false_negative_edges"] for row in rows)
    correct = sum(row["topology_match"] for row in rows)
    return {
        "expected_trace_count": len(rows),
        "generated_trace_count": sum(row["trace_present"] for row in rows),
        "correct_trace_count": correct,
        "true_positive_edges": tp,
        "false_positive_edges": fp,
        "false_negative_edges": fn,
        "complete_topology_match_pct": percentage(correct, len(rows)),
        "complete_node_match_pct": percentage(
            sum(row["nodes_match"] for row in rows), len(rows)
        ),
        "complete_edge_match_pct": percentage(
            sum(row["edges_match"] for row in rows), len(rows)
        ),
        "edge_precision_pct": percentage(tp, tp + fp),
        "edge_recall_pct": percentage(tp, tp + fn),
        "edge_f1_pct": percentage(2 * tp, 2 * tp + fp + fn),
    }


def benchmark_comparison(
    sent: list[oracle.SentMessage],
    mailbox_messages: dict[str, dict[str, bytes]],
    production_groups: dict[str, list[LogEntry]],
) -> dict[str, Any]:
    """Score both methods on shared grouped logs against an independent oracle."""
    expected_graphs = {
        item.message_id: received_oracle(
            item, mailbox_messages.get(item.message_id, {})
        )
        for item in sent
    }
    trace_count, span_graphs = oracle.export_span_graphs(production_groups)
    results = []
    for item in sent:
        message_id = item.message_id
        expected = expected_graphs[message_id]
        span_graph = span_graphs.get(message_id)
        timestamp = timestamp_graph(production_groups.get(message_id, []))
        results.append(
            {
                "scenario": item.scenario.name,
                "message_id": message_id,
                "expected_routes": item.scenario.expected_routes,
                "oracle_nodes": sorted(expected[0]),
                "oracle_edges": sorted(expected[1]),
                "mailtrace": evaluate_graph(
                    expected,
                    span_graph[:2] if span_graph is not None else None,
                ),
                "timestamp": evaluate_graph(
                    expected, timestamp if timestamp[0] else None
                ),
            }
        )
    mailtrace = summarize(results, "mailtrace")
    timestamp = summarize(results, "timestamp")
    return {
        "oracle": "mailbox_received_headers",
        "scope": "all_sent_messages",
        "input": {
            "grouping": "shared_message_id_and_queue_id_handoff",
            "log_count": sum(len(logs) for logs in production_groups.values()),
            "message_count": len(sent),
            "exported_mailtrace_count": trace_count,
            "timestamp_ordering": "earliest_raw_log_timestamp_then_host_queue_id",
        },
        "metric_definitions": METRIC_DEFINITIONS,
        "comparison": {
            "mailtrace": mailtrace,
            "timestamp": timestamp,
            "difference_percentage_points": {
                key: round(timestamp[key] - mailtrace[key], 6)
                for key in METRIC_DEFINITIONS
            },
        },
        "results": results,
    }


def print_summary(report: dict[str, Any]) -> None:
    if report.get("oracle") != "mailbox_received_headers":
        raise ValueError(
            "This result has no Received oracle; rerun the benchmark"
        )
    comparison = report["comparison"]
    print(f"Received oracle: {report['input']['message_count']} messages")
    for method in ("mailtrace", "timestamp"):
        summary = comparison[method]
        print(
            f"{method}: {summary['correct_trace_count']}/"
            f"{summary['expected_trace_count']} exact topologies; "
            f"accuracy {summary['complete_topology_match_pct']:.6f}%"
        )
    print(
        "\nMetric                          mailtrace    timestamp    delta (pp)"
    )
    for key in METRIC_DEFINITIONS:
        print(
            f"{key:<29} {comparison['mailtrace'][key]:>10.6f}% "
            f"{comparison['timestamp'][key]:>10.6f}% "
            f"{comparison['difference_percentage_points'][key]:>+12.6f}"
        )
    print("\nScenario                 mailtrace  timestamp")
    for result in report["results"]:
        print(
            f"{result['scenario']:<23} "
            f"{'PASS' if result['mailtrace']['topology_match'] else 'FAIL':<10} "
            f"{'PASS' if result['timestamp']['topology_match'] else 'FAIL'}"
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=oracle.DEFAULT_CONFIG)
    parser.add_argument("--result", "--output", default=DEFAULT_RESULT)
    parser.add_argument("--smtp-host", default="127.0.0.1")
    parser.add_argument("--mx-port", type=int, default=10025)
    parser.add_argument(
        "--mailer-ports", type=int, nargs=3, default=[20025, 20026, 20027]
    )
    parser.add_argument("--imap-host", default="127.0.0.1")
    parser.add_argument("--imap-port", type=int, default=10143)
    parser.add_argument("--opensearch-port", type=int)
    parser.add_argument("--timeout", type=float, default=90)
    parser.add_argument(
        "--self-test", action="store_true", help="Run local unit tests only"
    )
    parser.add_argument(
        "--print-result",
        action="store_true",
        help="Print an existing Received oracle result",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.self_test:
        suite = unittest.defaultTestLoader.discover(
            str(_PROJECT_ROOT / "tests"),
            pattern="test_bench_handoff_timestamp.py",
        )
        return 0 if unittest.TextTestRunner().run(suite).wasSuccessful() else 1
    config_path = oracle.resolve_path(args.config)
    result_path = oracle.resolve_path(args.result)
    try:
        if args.print_result:
            print_summary(json.loads(result_path.read_text(encoding="utf-8")))
            return 0
        if not config_path.is_file():
            raise oracle.InfrastructureError(
                f"Config not found: {config_path}"
            )
        run_id = uuid.uuid4().hex[:12]
        scenarios = oracle.build_scenarios(args.mx_port, args.mailer_ports)
        started_at = datetime.now(UTC) - timedelta(seconds=5)
        sent = oracle.send_scenarios(scenarios, args.smtp_host, run_id)
        mailbox_messages = oracle.wait_for_mailboxes(
            sent, args.imap_host, args.imap_port, args.timeout
        )
        for item in sent:
            for user in set(oracle.ALL_USERS) - set(
                item.scenario.expected_users
            ):
                unexpected = oracle.fetch_imap_message(
                    args.imap_host, args.imap_port, user, item.message_id
                )
                if unexpected is not None:
                    mailbox_messages[item.message_id][user] = unexpected
        _, production_groups = oracle.wait_for_logs(
            config_path, started_at, sent, args.timeout, args.opensearch_port
        )
        report = benchmark_comparison(
            sent, mailbox_messages, production_groups
        )
        report["run_id"] = run_id
        report["generated_at"] = datetime.now(UTC).isoformat()
        report["config"] = str(config_path)
        result_path.parent.mkdir(parents=True, exist_ok=True)
        result_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print_summary(report)
        print(f"result_file={result_path}")
        return 0
    except Exception as exc:
        print(f"Benchmark error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
