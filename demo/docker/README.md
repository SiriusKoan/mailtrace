# Demo - Docker

## Setup

Prepare the environment:

```shell
$ uv sync --all-groups
```

And make sure you have Docker and [swaks](https://linux.die.net/man/1/swaks) installed.

## Start

```shell
$ docker compose up -d
```

It will launch the demo environment, including 4 email servers, 1 OpenSearch node, and 1 OpenSearch Dashboard.

## Demo Setup

Change the `method` field in `config.yaml` to `ssh` or `opensearch`.

Change the `opensearch_config.time_zone` field in `config.yaml` to your desired time zone. By default, it is set to UTC+8.

### Using Loghost

If you want to use loghost to access logs from multiple email servers, follow these steps:

1. Set the `method` field in `config.yaml` to `ssh`.

2. In `config.yaml`, uncomment the `ssh_config_file` line that references `ssh_config_loghost`:
```yaml
ssh_config_file: demo/docker/ssh_config_loghost
```

3. In `config.yaml`, uncomment the `hosts` section under `ssh_config.host_config` and configure the log files and parsers for each email server:
```yaml
hosts:
  mx.example.com:
    log_files:
      - /var/log/mx/mail.log
    log_parser: NoSpaceInDatetimeParser
    time_format: "%Y-%m-%dT%H:%M:%S"
  mailer.example.com:
    log_files:
      - /var/log/mailer/mail.log
    log_parser: NoSpaceInDatetimeParser
    time_format: "%Y-%m-%dT%H:%M:%S"
  mailpolicy.example.com:
    log_files:
      - /var/log/mailpolicy/mail.log
    log_parser: NoSpaceInDatetimeParser
    time_format: "%Y-%m-%dT%H:%M:%S"
  mailbox.example.com:
    log_files:
      - /var/log/mailbox/mail.log
    log_parser: NoSpaceInDatetimeParser
    time_format: "%Y-%m-%dT%H:%M:%S"
```

This allows mailtrace to query multiple email servers simultaneously, aggregating logs from all configured hosts.

## Demo External to Internal (`mx`)

[![asciicast](https://asciinema.org/a/761209.svg)](https://asciinema.org/a/761209)

```shell
$ swaks \
    --to user1@example.com \
    --from me@siriuskoan.one \
    --helo siriuskoan.one \
    --server 127.0.0.1 -p 10025

$ python3 -m mailtrace run \
    -c demo/docker/config.yaml \
    -h mx.example.com \
    -k user1 \
    --time '2025-12-10 20:00:00' --time-range 1h
```

## Demo Internal to Internal (`mailer`)

[![asciicast](https://asciinema.org/a/761210.svg)](https://asciinema.org/a/761210)

```shell
$ swaks \
    --to user2@example.com \
    --from user1@example.com \
    -au user1 -ap user1 \
    --server 127.0.0.1 -p 20025

$ python3 -m mailtrace run \
    -c demo/docker/config.yaml \
    -h mailpolicy.example.com \
    -k user1 \
    --time '2025-12-10 20:00:00' --time-range 1h
```
