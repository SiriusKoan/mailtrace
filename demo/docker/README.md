# Docker Demo

## Start the environment

```shell
uv sync --all-groups
docker compose -f demo/docker/docker-compose.yml up -d --build
```

All Postfix services enable `enable_long_queue_ids = yes` to avoid queue ID
reuse during benchmark runs. Rebuild the mail images to apply this setting to
existing containers.

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

The same run also tests Message-ID evidence ablation, recorded in the JSON
`membership_ablation` section. Every mode preserves log count, queue IDs, handoff
records, and delays. Only Postfix `message-id=<...>` and Exim acceptance
`id=...@...` fields are removed; queue IDs in SMTP replies remain intact.

- `entrypoint-only` retains one entry queue Message-ID seed per message and
  regroups mixed logs through production code to test downstream propagation.
- `entrypoint-only-reversed` reverses the same ablated input to test propagation
  across multiple passes independently of input order.
- `cached-entrypoint` processes only entry Message-ID records in the first batch,
  then removes all Message-ID evidence in the second batch while retaining the
  first batch's queue mappings to test cross-batch fallback and propagation.
- `no-seed-control` removes all Message-ID evidence and uses empty mappings;
  message groups and generated traces are expected to be empty.

The first three modes compare per-message log membership and use mailbox Received
chains and raw-log oracles to verify nodes, edges, span attributes, delays, and
durations. Original groups only select the run's input. Production regrouping
receives neither those original groups nor precomputed downstream mappings.

The `handoff_ablation` section records a separate `no-handoff-tokens` mode. It
retains Message-ID evidence at every queue hop while removing `queued as QUEUEID`,
`forwarded as QUEUEID`, and SMTP reply `id=QUEUEID` tokens, and clearing parsed
`queued_as` fields. Each hop's own queue ID, relay information, log count, and
delay records remain intact.

This mode expects all log membership and nodes to remain, with zero handoff edges
and no `email.next_queue_id` or `email.handoff=explicit` span attributes. Mailbox
and raw-log oracles remain complete; an empty `expected_trace_edges` field
separately records the ablated expectation. Repeated hosts may have `implicit`
handoff attributes but must still produce no edges between hops.

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

These commands expect the Docker demo environment to be running, except for
`bench_resource_rates.py` and daemon correctness with `--reset-environment`,
which manage their own Compose lifecycle.

### Tracing performance and structure

`bench_tracing.py` sends each configured batch of messages, queries the
corresponding OpenSearch logs, builds and exports OTLP spans, and measures the
query, span-building, flush, total, and per-trace times. It also compares the
host/stage presence in actual exported spans with the structure inferred from
the complete log set, reporting exact, malformed, and missing traces. Span
generation uses production `export_traces`, including queue hops, delivery
wrappers, and Exim delay fields split across log records. Repeated stages on a
host are counted once; these checks do not measure complete handoff topology.

Correctness comparison, email sending, and indexing waits are outside the
reported phase times. Local span recording is included in export work. The
default OTLP endpoint is `http://localhost:14317`, matching the Docker demo's
published gRPC port. Repeated runs report the mean.

Each batch uses a unique Message-ID prefix. `--wait` is the maximum indexing
wait per run. The script queries immediately and waits one second between
checks until the expected message count is present or the deadline expires,
without resending emails or exporting incomplete batches. Polling waits and
discarded queries are excluded from phase timings. The legacy `--max-retries`
option is accepted but ignored. Once the expected count is complete, malformed
and missing traces remain part of the reported results.

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

`handoff_edge_recall` measures directed handoff edges from production tracing
spans, with nodes identified by `(hostname, queue ID)`. Each trial divides the
number of retained baseline edges across all messages by the total baseline
edge count, then results are averaged across trials. Missing traces keep their
baseline edges in the denominator. A baseline with no edges has recall `1.0`.
The JSON field is `handoff_edge_recall_mean`; the existing `edge_recall` measures
root-to-host and host-to-delay-stage structural edges.

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

`handoff_edge_recall` also uses directed handoff edges from production spans.
It excludes baseline edges incident to the removed host, then divides retained
baseline edges across all messages by the remaining baseline edge count.
Missing traces keep their remaining baseline edges in the denominator. A
remaining baseline with no edges has recall `1.0`. The JSON field is
`retained_handoff_edge_recall_mean`.

This example sends 100 messages through the mailer-entrypoint team-alias path
and evaluates every host found in the resulting logs:

```shell
uv run python demo/docker/bench_host_ablation.py \
  --size 100 \
  --traffic-path mailer-team
```

### Production daemon correctness

`bench_daemon_correctness.py` starts the production tracing CLI for each
polling-parameter set, sends test messages, and builds baseline traces from
complete OpenSearch logs in one production `export_traces` call. Baseline spans
are kept in memory and are not written to Tempo. The script reads daemon traces
from Tempo and compares host sets and delay-stage names and order. Every
committed repeat prints its exact, malformed, and missing counts. `runs.json`
and `aggregate.json` expose partial results, including each setting's current
sample count, mean exact-match percentage, and sample standard deviation.

Start the complete 28-setting experiment directly from this script:

```shell
uv run --group tracing python demo/docker/bench_daemon_correctness.py \
  --reset-environment \
  --relay-delay-seconds 40 \
  --sleep-seconds 10 \
  --hold-values 0 1 2 3 4 5 10 \
  --go-back-values 0 3 5 10 \
  --runs 10 --size 10 --wait 100 \
  --output-dir "results/daemon-correctness-$(date -u +%Y%m%dT%H%M%SZ)"
```

`--reset-environment` takes ownership of the demo's `docker` Compose project.
It builds the mail images once, pins all eleven service images, and starts a
fresh OpenSearch, Tempo, and mail stack for each repeat of ten emails. Before
and after every repeat it removes the project's containers, networks, and
volumes. Cleanup must succeed before the next repeat starts. Container memory
limits are disabled; raw logs, signatures, traces, and cleanup records remain
in the output directory. Fresh runs require a new directory. The script writes
`progress.json` before setup and updates it at least every five seconds while
the process can run, including the active phase, work plan, attempts, committed
results, and Linux memory/I/O pressure. `status.json` is a compact view of the
same state. Updates use atomic replacement, so concurrent readers see complete
JSON and process crashes retain the last committed snapshot. Power-loss
durability is not claimed.

Resume an interrupted, failed, or abruptly terminated run with this single
command, using its existing output directory:

```shell
uv run --group tracing python demo/docker/bench_daemon_correctness.py \
  --resume results/EXPERIMENT/progress.json
```

Inspect live progress and partial measured results:

```shell
watch -n 5 cat results/EXPERIMENT/status.json
cat results/EXPERIMENT/aggregate.json
```

Resume validates the implementation (including uncommitted source changes),
configuration, dependencies, workload plan, seeds, pinned environment
definition, and hashes of committed artifacts before resetting resources. It keeps the original run
ID and results, skips committed repeats, stops identified orphaned daemons,
then cleans and recreates the demo stack before replaying unfinished work.
It reuses the saved image IDs and stops if a pinned image is unavailable.
A completed compatible run performs no reset or measurement. Changed or
corrupt experiments are rejected and require a separate fresh run; output
from older versions without a compatibility manifest remains available but
cannot be resumed. Only the active, uncommitted repeat may need replaying;
there are no automatic retries or duplicate committed results. Each replay
uses a separate attempt directory and retains earlier diagnostics.

Route selection uses `--seed` (default `0`) plus the repeat number, so every
parameter setting receives the same route choices for corresponding repeats.
Message IDs remain unique per attempt. Container CPU quotas and image IDs are
recorded; Docker RAM and swap limits remain disabled. Each measurement uses
a cold stack, excluding image preparation and environment reset from its
reported duration. Shared-host caches and other workloads remain outside the
experiment's control. Before starting containers, the script waits up to
15 minutes for available memory and Linux pressure metrics to recover; it
does not change host settings or remove unrelated resources. Infrastructure
failures stop the batch and remain visible rather than becoming zero scores.

The daemon must complete its first successful query before sending begins.
Each repeat has a unique Message-ID prefix, and its baseline includes only
those messages.
每次寄信結束後，程式先等待 `--wait` 指定的秒數，`--reset-environment` 也使用相同設定。
之後最多等待 15 分鐘，確認所有收件者的投遞紀錄及佇列完成紀錄齊全，
再從回溯時間結束後觀察 `hold_rounds + 2` 次成功查詢。
Zero correctness scores are retained without retries. Expected connection errors while OpenSearch starts are retried without stack
trace spam. Ctrl+C records an `interrupted` state and finishes cleanup; further
Ctrl+C signals during cleanup do not skip resource removal.

Recovery validation used two repeats of two messages with a 40-second relay
delay: SIGKILL during the second repeat, explicit resume from the saved
`running` checkpoint, and an uninterrupted run with matching seeds. The first
commit stayed unchanged, the orphan daemon stopped, cleanup left no demo
resources, and an explicitly separate Compose-project sentinel survived.
All four committed raw-log baselines matched their saved scoring artifacts.

Without `--reset-environment`, the script uses an already running demo stack.

To spread a message's logs across polling rounds, add an outbound SMTP delay to
all three Exim mailers after starting the demo stack:

```shell
MAILTRACE_RELAY_DELAY_SECONDS=40 \
docker compose -f demo/docker/docker-compose.yml \
  up -d --build mailer1 mailer2 mailer3

uv run python demo/docker/bench_daemon_correctness.py \
  --size 10 --runs 1 --wait 100 \
  --params 10,0,30 10,5,30
```

`MAILTRACE_RELAY_DELAY_SECONDS` accepts non-negative integer seconds and defaults
to `0`, which disables the delay. Exim accepts each message immediately, then its
[`transport_filter`](https://www.exim.org/exim-html-current/doc/html/spec_html/ch-generic_options_for_transports.html)
waits before streaming the unchanged message to the mailpolicy. Single routes
cross one delayed mailer; team aliases cross two, so a 40-second setting adds
about 40 or 80 seconds to end-to-end delivery. Increase the benchmark's `--wait`
to cover the longest route, otherwise its baseline can exclude late delivery
logs. Recreate the mailers with the variable set to `0` to disable the delay.

`go_back_seconds` overlaps log query windows to recover records indexed after
their timestamps. `hold_rounds` keeps a message buffered until that many polling
rounds pass without new logs. Holding cannot recover records missed by queries.
A look-back long enough to contain a whole fast route can also reconstruct it
with zero hold. A 40-second gap exceeds a 30-second look-back and exercises the
need to keep earlier records buffered until later delivery logs arrive.

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

### Timestamp and mailtrace accuracy against a Received oracle

`bench_handoff_timestamp.py` reuses the 16 routing scenarios, SMTP/IMAP/OpenSearch
collection routines, and Received header parser from `bench_handoff_correctness.py`.
The union of recipient Received chains provides an independent oracle. Both methods
are scored against it; mailtrace accuracy is calculated from its actual output.

Both methods receive the same logs grouped by Message-ID and queue ID handoffs.
Mailtrace runs production `export_traces` and reconstructs topology from exported
spans. Timestamp ordering builds a linear topology from each queue hop's earliest
raw log timestamp, breaking ties by hostname and queue ID without using handoff
evidence to determine order. This comparison measures topology reconstruction on
shared message groups; it does not compare message grouping algorithms.

Nodes are `(hostname, queue ID)` pairs, preserving branches and repeated hosts.
Complete topology accuracy requires both nodes and edges to exactly match the
oracle. Edge precision, recall, and F1 use micro-averaged TP/FP/FN across all
scenarios. Every sent message remains in the denominator, including missing traces,
branches, and ambiguous handoffs. Invalid Received headers, missing mailbox copies,
or routes that differ from the scenario manifest fail as infrastructure errors.
The JSON includes missing and extra nodes and edges for each method. Percentage
point differences are timestamp minus mailtrace.

Run after starting the Docker demo above. The default output is
`demo/docker/bench_handoff_timestamp_result.json`. Accuracy differences do not
change the exit code; infrastructure or oracle failures return a non-zero status:

```shell
uv run python demo/docker/bench_handoff_timestamp.py
```

Connection options match the correctness benchmark. Additional options are
`--result` (or `--output`), `--print-result`, and `--self-test`, which requires no
Docker services. Legacy fixed-dataset results lack a Received oracle and must be
regenerated before using the new summary format.

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
waits for the generated trace count to reach the submitted email count, and
then reports any remaining Postfix and Exim queues. Its report records
submitted, sent, and failed email counts; generated
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

Trace completion progress is printed to stderr immediately when sending starts
and every `--trace-poll-interval` seconds while sending and waiting for traces
(default: 120 seconds). Sending-phase progress is marked `(sending emails)`;
reaching the trace target does not stop the sender early. Sender and trace log
paths are printed at the start of each rate; sender output stays in its log file.
For progress every 10 seconds:

```shell
uv run python demo/docker/bench_resource_rates.py --trace-poll-interval 10
```

## Additional traffic generators

```shell
demo/docker/send_bulk_emails.sh 100
uv run python demo/docker/send_bulk_emails.py 20 60
```

Both tools distribute messages across the configured routes described above.
