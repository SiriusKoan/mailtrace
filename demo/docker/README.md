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
