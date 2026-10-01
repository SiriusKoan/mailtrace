# Docker Demo

## Start the environment

```shell
uv sync --all-groups
docker compose -f demo/docker/docker-compose.yml up -d --build
```

Main services and entrypoints:

| Service | Hostname | Host port |
|---|---|---:|
| MX | `mx.example.com` | SMTP `10025` |
| Mailer 1 | `mailer1.example.com` | SMTP `20025` |
| Mailer 2 | `mailer2.example.com` | SMTP `20026` |
| Mailer 3 | `mailer3.example.com` | SMTP `20027` |
| Mailbox | `mailbox.example.com` | IMAP `10143` |
| OpenSearch | `opensearch.example.com` | HTTPS `9200` |
| Tempo | `tempo.example.com` | HTTP `3200`, OTLP gRPC `14317` |

## Mail topology

Messages sent through the MX entrypoint follow these routes:

```text
mx -> mailerN -> mailpolicyN -> mailbox
                             -> mx -> mailerM -> mailpolicyM -> mailbox  (alias branch)
```

Messages sent through a mailer entrypoint follow these routes:

```text
mailerN -> mailpolicyN -> mailbox
                      -> mx -> mailerM -> mailpolicyM -> mailbox  (alias branch)
```

Each `mailpolicyN` uses a separate Postfix `virtual_alias_maps` file and does
not treat the public test domains as local domains. Regular aliases target
`delivery.example.com`, which the current `mailpolicyN` sends directly to the
mailbox. The second target of each `team` alias belongs to the next public test
domain, so it returns to the MX and passes through the next mailer and mail
policy pair.

| Recipient | Alias targets |
|---|---|
| `single@N.example.com` | `user1@delivery.example.com` |
| `team@1.example.com` | `user1@delivery.example.com`, `alias-from-1@2.example.com` |
| `team@2.example.com` | `user1@delivery.example.com`, `alias-from-2@3.example.com` |
| `team@3.example.com` | `user1@delivery.example.com`, `alias-from-3@1.example.com` |
| `parallel@1.example.com` | `parallel-a@2.example.com`, `parallel-b@2.example.com` |
| `revisit-start@1.example.com` | `revisit-middle@2.example.com` |
| `revisit-middle@2.example.com` | `revisit-return@1.example.com` |
| `revisit-return@1.example.com` | `user1@delivery.example.com` |

## Handoff correctness validation

`bench_handoff_correctness.py` sends sixteen fixed messages. The first twelve
cover every combination of MX or mailer entrypoint, domains 1 through 3, and
single-recipient or team aliases:

| Entrypoint | Domain | Alias | Expected routes |
|---|---:|---|---|
| MX | `N` | `single` | `user1: mx -> mailerN -> mailpolicyN -> mailbox` |
| MX | `N` | `team` | `user1: mx -> mailerN -> mailpolicyN -> mailbox`<br>`user2: mx -> mailerN -> mailpolicyN -> mx -> mailerM -> mailpolicyM -> mailbox` |
| Mailer N | `N` | `single` | `user1: mailerN -> mailpolicyN -> mailbox` |
| Mailer N | `N` | `team` | `user1: mailerN -> mailpolicyN -> mailbox`<br>`user2: mailerN -> mailpolicyN -> mx -> mailerM -> mailpolicyM -> mailbox` |

Here, `N` is 1, 2, or 3, while `M` is the next domain in the `1 -> 2 -> 3 -> 1`
cycle. Four additional scenarios exercise shared queues and repeated hosts:

| Scenario | Envelope recipients | Expected routes |
|---|---|---|
| `mx-multi-domain` | `single@1.example.com`, `alias-from-1@2.example.com` | `user1: mx -> mailer1 -> mailpolicy1 -> mailbox`<br>`user2: mx -> mailer2 -> mailpolicy2 -> mailbox` |
| `mailer-multi-domain` | `single@1.example.com`, `alias-from-1@2.example.com` | `user1: mailer1 -> mailpolicy1 -> mailbox`<br>`user2: mailer1 -> mailpolicy2 -> mailbox` |
| `parallel-shared-route` | `parallel@1.example.com` | `user1/user2: mx -> mailer1 -> mailpolicy1 -> mx -> mailer2 -> mailpolicy2 -> mailbox` |
| `revisit-origin` | `revisit-start@1.example.com` | `user1: mx -> mailer1 -> mailpolicy1 -> mx -> mailer2 -> mailpolicy2 -> mx -> mailer1 -> mailpolicy1 -> mailbox` |

Run it with:

```shell
uv run python demo/docker/bench_handoff_correctness.py
```

The script validates `mailtrace.tracing.builder.export_traces` against three
sources of evidence:

- It reads raw messages from the mailbox over IMAP and parses the `Received`
  chain.
- It queries raw OpenSearch documents directly and uses independent regular
  expressions to parse queue handoffs, Postfix `delays=a/b/c/d`, and Exim
  `RT/QT/DT` fields.
- It invokes the production query, grouping, and `export_traces` code, projects
  the resulting spans into a handoff graph, then compares delay values and span
  durations for every `(Message-ID, host, queue-id)` delivery.

The script records failed correctness checks but returns status code `0`.
Infrastructure failures such as SMTP, IMAP, OpenSearch, or configuration errors
return a non-zero status code.

## Resource benchmark

The resource benchmark Compose file is an override for the main Compose file:

```shell
docker compose \
  -f demo/docker/docker-compose.yml \
  -f demo/docker/docker-compose.resource-benchmark.yml \
  up -d --build
```

The override only changes the isolated network, host ports, health checks,
Vector configuration, and the mailtrace daemon. `bench_resource_rates.py`
automatically uses both Compose files.

## Benchmark scripts

Except for `bench_resource_rates.py`, which manages its own isolated Compose
stack, these commands expect the Docker demo environment to be running.

### Tracing performance and structure

`bench_tracing.py` sends each configured batch of messages, queries the
corresponding OpenSearch logs, builds and exports OTLP spans, and measures the
query, span-building, flush, total, and per-trace times. It also compares the
generated host/stage structure with the structure inferred from the complete
log set, reporting exact, malformed, and missing traces.

The following example uses the default batch sizes of 10, 50, 100, and 1,000
messages and runs each size ten times:

```shell
uv run python demo/docker/bench_tracing.py --runs 10
```

### Random missing-log robustness

`bench_random_missing_logs.py` builds a full-log baseline, randomly removes
each configured fraction of log entries, and reconstructs traces from the
remaining logs. Across repeated trials it reports exact, malformed, and
missing trace percentages plus mean span, delay-stage, and edge recall.

This example sends 100 messages per trial, runs ten trials, and includes a 0%
control in addition to missing-log ratios from 10% through 70%:

```shell
uv run python demo/docker/bench_random_missing_logs.py \
  --size 100 \
  --trials 10 \
  --missing-ratios 0 0.1 0.2 0.3 0.4 0.5 0.6 0.7
```

### Host-ablation robustness

`bench_host_ablation.py` builds a full-log trace baseline and then removes all
logs from one host at a time. It classifies each reconstructed trace as
unchanged, missing only the removed host, changed on other hosts, or
disappeared, and reports retained span, stage, and edge recall.

This example sends 100 messages through the mailer-entrypoint team-alias path
and evaluates every host found in the resulting logs:

```shell
uv run python demo/docker/bench_host_ablation.py \
  --size 100 \
  --traffic-path mailer-team
```

### Production daemon correctness

`bench_daemon_correctness.py` starts the production tracing CLI for each
polling-parameter set, sends test messages, derives expected traces from
complete OpenSearch logs, reads generated traces from Tempo, and compares
their structures. After each parameter set finishes, the script prints that
set's mean and sample standard deviation. It prints the complete aggregate
table again after all parameter sets finish.

This example evaluates the full combination of the listed hold-round and
look-back values, running each parameter set ten times:

```shell
uv run python demo/docker/bench_daemon_correctness.py \
  --runs 10 \
  --hold-values 0 1 2 3 4 5 10 \
  --go-back-values 0 3 5 10 30
```

### Handoff correctness

`bench_handoff_correctness.py` sends 16 fixed routing scenarios covering MX
and mailer entrypoints, all three domains, aliases, shared routes, and repeated
hosts. It cross-checks mailbox `Received` chains, independently parsed raw
OpenSearch handoffs and delays, and spans produced by `export_traces`. The JSON
report includes route, graph, handoff-attribute, delay-value, and span-duration
checks. Failed correctness checks are recorded in the report but return status
code `0`; infrastructure failures return a non-zero status.

Run all scenarios with the default ports and timeout:

```shell
uv run python demo/docker/bench_handoff_correctness.py
```

### Timestamp-only handoff comparison

`bench_handoff_timestamp.py` compares the current queue-handoff topology with
a timestamp-only topology over its fixed one-day OpenSearch dataset. It
replays production query windows and lifecycle behavior, then reports complete
host-order and topology matches plus edge precision, recall, and F1. Synthetic
topology and queue-mapping validation runs before the dataset comparison.

Run the complete fixed-dataset comparison and write the default JSON result:

```shell
uv run python demo/docker/bench_handoff_timestamp.py
```

### Container resource sampler

`bench_resources.py` resolves a Docker container's cgroup v2 path and samples
CPU usage, CPU throttling, current memory, process count, and OOM events
approximately once per second. It appends samples to CSV and writes a JSON
summary, including cumulative counter deltas and process-count extrema, to
standard output when interrupted or when the container stops.

This example monitors the Compose `mailtrace` service and writes samples to
`/tmp/mailtrace-resources.csv`. Press Ctrl-C to stop it and print the summary:

```shell
uv run python demo/docker/bench_resources.py \
  --container "$(docker compose -f demo/docker/docker-compose.yml ps -q mailtrace)" \
  --output /tmp/mailtrace-resources.csv
```

### Resource usage by email rate

`bench_resource_rates.py` cleans, builds, starts, and finally removes an
isolated benchmark Compose stack. At each configured email rate it drives
traffic, monitors the `mailtrace` container through `bench_resources.py`,
waits for Postfix and Exim queues to drain, and verifies the generated trace
count. Its report records submitted, sent, and failed email counts; generated
trace and trace-batch counts; OpenSearch query count; and the total number of
log entries returned across query windows. The queried-log-entry total measures
work performed and can count an entry more than once when query windows
overlap. Each run also produces resource CSV and JSON files, an SVG CPU chart,
and sender and trace logs.

Run the default rates of 10, 20, 50, 100, 200, and 500 messages per second for
600 seconds each:

```shell
uv run python demo/docker/bench_resource_rates.py
```

## Additional traffic generators

```shell
demo/docker/send_bulk_emails.sh 100
uv run python demo/docker/send_bulk_emails.py 20 60
```

Both tools distribute messages across the configured routes described above.
