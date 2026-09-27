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

## Additional traffic generators

```shell
demo/docker/send_bulk_emails.sh 100
uv run python demo/docker/send_bulk_emails.py 20 60
```

Both tools distribute messages across the configured routes described above.
