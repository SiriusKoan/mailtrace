"""Seed OpenSearch with NDJSON mail log data."""

import json
import os
import sys
import time
from pathlib import Path

from opensearchpy import OpenSearch, helpers

OPENSEARCH_HOST = os.environ.get("OPENSEARCH_HOST", "opensearch")
OPENSEARCH_PORT = int(os.environ.get("OPENSEARCH_PORT", "9200"))
OPENSEARCH_PASSWORD = os.environ.get("OPENSEARCH_PASSWORD", "")
INDEX_NAME = "mailtrace-logs"
DATA_DIR = Path("/app/data")

# Max retries waiting for OpenSearch
MAX_RETRIES = 30
RETRY_INTERVAL = 5


def create_client() -> OpenSearch:
    return OpenSearch(
        hosts=[{"host": OPENSEARCH_HOST, "port": OPENSEARCH_PORT}],
        http_auth=("admin", OPENSEARCH_PASSWORD),
        use_ssl=True,
        verify_certs=False,
        ssl_show_warn=False,
    )


def wait_for_opensearch(client: OpenSearch) -> None:
    """Wait until OpenSearch cluster is ready."""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            health = client.cluster.health()
            status = health.get("status")
            if status in ("green", "yellow"):
                print(f"OpenSearch ready (status={status})")
                return
            print(f"Attempt {attempt}/{MAX_RETRIES}: cluster status={status}")
        except Exception as e:
            print(f"Attempt {attempt}/{MAX_RETRIES}: {e}")
        time.sleep(RETRY_INTERVAL)
    print("ERROR: OpenSearch not ready after max retries", file=sys.stderr)
    sys.exit(1)


def create_index(client: OpenSearch) -> None:
    """Create the mailtrace-logs index with appropriate mappings."""
    if client.indices.exists(index=INDEX_NAME):
        print(f"Index '{INDEX_NAME}' already exists, skipping creation")
        return

    mapping = {
        "settings": {
            "number_of_shards": 1,
            "number_of_replicas": 0,
        },
        "mappings": {
            "properties": {
                "@timestamp": {"type": "date"},
                "host": {
                    "properties": {
                        "name": {"type": "keyword"},
                    }
                },
                "data_stream": {
                    "properties": {
                        "namespace": {"type": "keyword"},
                    }
                },
                "appname": {"type": "keyword"},
                "message": {"type": "text"},
                "postfix": {
                    "properties": {
                        "queueid": {
                            "properties": {
                                "keyword": {"type": "keyword"},
                            }
                        },
                        "queued_as": {
                            "properties": {
                                "keyword": {"type": "keyword"},
                            }
                        },
                        "message-id": {"type": "keyword"},
                    }
                },
            }
        },
    }

    client.indices.create(index=INDEX_NAME, body=mapping)
    print(f"Created index '{INDEX_NAME}'")


def bulk_import(client: OpenSearch, filepath: Path) -> int:
    """Import an NDJSON file into OpenSearch. Returns document count."""
    actions = []
    with open(filepath) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            doc = json.loads(line)
            actions.append({"_index": INDEX_NAME, "_source": doc})

    if not actions:
        return 0

    success, errors = helpers.bulk(client, actions, raise_on_error=False)
    if errors:
        print(f"  WARNING: {len(errors)} errors during bulk import of {filepath.name}")
        for err in errors[:5]:
            print(f"    {err}")
    return success


def main() -> None:
    print("Starting OpenSearch seed...")
    client = create_client()
    wait_for_opensearch(client)
    create_index(client)

    ndjson_files = sorted(DATA_DIR.glob("*.ndjson"))
    if not ndjson_files:
        print("No .ndjson files found in data/")
        return

    total = 0
    for filepath in ndjson_files:
        count = bulk_import(client, filepath)
        print(f"  Imported {count} documents from {filepath.name}")
        total += count

    # Refresh index to make documents searchable immediately
    client.indices.refresh(index=INDEX_NAME)
    print(f"Seed complete: {total} documents imported into '{INDEX_NAME}'")


if __name__ == "__main__":
    main()
