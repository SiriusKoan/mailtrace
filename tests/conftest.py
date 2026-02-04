"""Shared test fixtures."""

import subprocess
import time
from pathlib import Path

import pytest

from mailtrace.config import (
    Config,
    Method,
    OpenSearchConfig,
    OpenSearchMappingConfig,
    SSHConfig,
)

_DEV_DIR = Path(__file__).resolve().parent.parent / "dev"
_OS_HOST = "localhost"
_OS_PORT = 9200
_OS_USER = "admin"
_OS_PASS = "Dev@2026#Mailtrace"
_INDEX = "mailtrace-logs"

_HEALTH_TIMEOUT = 60  # seconds to wait for cluster health
_SEED_TIMEOUT = 30  # seconds to wait for seed data
_POLL_INTERVAL = 2  # seconds between polls


def _create_client():
    """Create an OpenSearch client for the dev instance."""
    from opensearchpy import OpenSearch

    return OpenSearch(
        hosts=[{"host": _OS_HOST, "port": _OS_PORT}],
        http_auth=(_OS_USER, _OS_PASS),
        use_ssl=True,
        verify_certs=False,
        ssl_show_warn=False,
        timeout=5,
    )


def _is_healthy(client) -> bool:
    """Check if the cluster is green/yellow."""
    try:
        health = client.cluster.health()
        return health.get("status") in ("green", "yellow")
    except Exception:
        return False


def _is_seeded(client) -> bool:
    """Check if the index exists and has documents."""
    try:
        if not client.indices.exists(index=_INDEX):
            return False
        result = client.count(index=_INDEX)
        return result.get("count", 0) > 0
    except Exception:
        return False


def _ensure_opensearch() -> bool:
    """Start dev OpenSearch if needed; wait for health + seed.

    Returns True if the environment is ready, False on timeout.
    """
    client = _create_client()

    # Already running and seeded -- fast path
    if _is_healthy(client) and _is_seeded(client):
        return True

    # Start the dev environment
    print("Starting dev OpenSearch environment...")
    subprocess.run(
        ["docker", "compose", "up", "-d"],
        cwd=_DEV_DIR,
        check=True,
        capture_output=True,
    )

    # Wait for cluster health
    deadline = time.monotonic() + _HEALTH_TIMEOUT
    while time.monotonic() < deadline:
        if _is_healthy(client):
            break
        time.sleep(_POLL_INTERVAL)
    else:
        print("ERROR: OpenSearch health check timed out")
        return False

    # Wait for seed data
    deadline = time.monotonic() + _SEED_TIMEOUT
    while time.monotonic() < deadline:
        if _is_seeded(client):
            print("Dev OpenSearch ready and seeded")
            return True
        time.sleep(_POLL_INTERVAL)

    print("ERROR: Seed data not found after timeout")
    return False


@pytest.fixture(scope="session")
def _ensure_dev_opensearch():
    """Bootstrap dev OpenSearch if not already running."""
    if not _ensure_opensearch():
        pytest.skip("Failed to bootstrap dev OpenSearch (is Docker running?)")


@pytest.fixture(scope="session")
def dev_config(_ensure_dev_opensearch):
    """Create a Config object pointing to the local dev OpenSearch."""
    return Config(
        method=Method.OPENSEARCH,
        log_level="INFO",
        ssh_config=SSHConfig(username="dummy", password="dummy"),
        opensearch_config=OpenSearchConfig(
            host=_OS_HOST,
            port=_OS_PORT,
            username=_OS_USER,
            password=_OS_PASS,
            index=_INDEX,
            use_ssl=True,
            verify_certs=False,
            time_zone="+00:00",
            timeout=10,
            mapping=OpenSearchMappingConfig(
                facility="data_stream.namespace",
                hostname="host.name",
                message="message",
                timestamp="@timestamp",
                service="appname",
                queueid="postfix.queueid.keyword",
                queued_as="postfix.queued_as.keyword",
            ),
        ),
        clusters={
            "mx-cluster": ["mx1", "mx2"],
            "mailer-cluster": ["smtp1", "smtp2", "relay1"],
            "maillist-cluster": ["list1", "list2"],
            "edge-cluster": ["mta-edge-01", "mta-edge-02"],
            "relay-cluster": ["mta-relay-01"],
        },
    )


@pytest.fixture(scope="session")
def aggregator_class():
    """Return the OpenSearch aggregator class."""
    from mailtrace.aggregator.opensearch import OpenSearch

    return OpenSearch
