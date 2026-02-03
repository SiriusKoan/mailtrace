# Dev OpenSearch Suite

Local OpenSearch environment pre-seeded with realistic mail log data for mailtrace development.

## Prerequisites

- Docker and Docker Compose

## Quick Start

```bash
cd mail-tools/dev
docker compose up -d
```

The seed container automatically imports NDJSON data from `seed/data/` then exits. OpenSearch Dashboards is available at <http://localhost:5601>.

Default credentials: `admin` / `Dev@2026#Mailtrace` (configured in `.env`).

## Connecting Mailtrace

Use the opensearch method with a config like:

```yaml
method: opensearch

opensearch_config:
  host: localhost
  port: 9200
  username: admin
  password: Dev@2026#Mailtrace
  index: mailtrace-logs
  use_ssl: true
  verify_certs: false
  time_zone: "+00:00"
  timeout: 10
  mapping:
    facility: data_stream.namespace
    hostname: host.name
    message: message
    timestamp: "@timestamp"
    service: appname
    queueid: postfix.queueid.keyword
    queued_as: postfix.queued_as.keyword

clusters:
  mx-cluster:
    - mx1
    - mx2
  mailer-cluster:
    - smtp1
    - smtp2
    - relay1
  maillist-cluster:
    - list1
    - list2
```

### Trace Example

Trace a cross-cluster mail flow:

```bash
# Outbound: smtp1 -> relay1 -> external
mailtrace trace -c dev/config.yaml -h mailer-cluster \
    -k "A1001CC0001" --time "2026-02-04 08:00:00" --time-range 30m

# Inbound 3-hop: mx1 -> list1 -> relay1
mailtrace trace -c dev/config.yaml -h mx-cluster \
    -k "C3001CC0003" --time "2026-02-04 08:10:00" --time-range 30m
```

### Flow Check Example

Check flow conservation for the mx-cluster:

```bash
mailtrace flow-check \
    -c dev/config.yaml \
    --cluster mx-cluster \
    --time "2026-02-04 08:30:00" \
    --time-range 1h
```

This verifies that every inbound mail to `mx1`/`mx2` reached a terminal state. All 4 inbound flows are relayed out to `list1` (complete).

Check the maillist-cluster to see problematic flows:

```bash
# Shows 3 complete + 2 problematic (deferred + bounced)
mailtrace flow-check -c dev/config.yaml --cluster maillist-cluster \
    --time "2026-02-04 08:30:00" --time-range 1h
```

## Seed Data

NDJSON files in `seed/data/`:

| File | Description |
|---|---|
| `cross-cluster.ndjson` | Complete cross-cluster flows with logs from every host in each hop |

### Scenarios in `cross-cluster.ndjson`

| # | Flow | Clusters | Outcome |
|---|------|----------|---------|
| 1 | smtp1 -> relay1 -> external | mailer | Delivered |
| 2 | smtp2 -> relay1 -> external | mailer | Delivered |
| 3 | mx1 -> list1 -> relay1 (local delivery) | mx -> maillist -> mailer | Delivered |
| 4 | mx2 -> list1 -> relay1 (local delivery) | mx -> maillist -> mailer | Delivered |
| 5 | mx1 -> list1 -> relay1 (refused) | mx -> maillist | Deferred |
| 6 | mx1 -> list1 (fan-out to 2 local rcpts) | mx -> maillist | Delivered |
| 7 | smtp1 -> relay1 -> list1 (rejected) -> bounce back | mailer -> maillist -> mailer | Bounced + NDN |

Noise entries (NOQUEUE rejects) are included across all hosts.

## Adding More Seed Data

1. Add `.ndjson` files to `seed/data/`
2. Rebuild the seed container: `docker compose build opensearch-seed`
3. Re-run: `docker compose up opensearch-seed`

Each NDJSON line should follow the production mapping format:

```json
{
  "@timestamp": "2026-01-27T12:59:03Z",
  "host": {"name": "mx1"},
  "data_stream": {"namespace": "mail"},
  "appname": "postfix/smtp",
  "message": "BE2241E005C: to=<alice@mail.example.org>, ..., status=sent",
  "postfix": {
    "queueid": {"keyword": "BE2241E005C"},
    "queued_as": {"keyword": "0AE3D360152"}
  }
}
```

## Cleanup

```bash
docker compose down -v  # Remove containers and data volume
```
