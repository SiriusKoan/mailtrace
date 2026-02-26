# Mailtrace

Mailtrace is a command-line tool for tracing emails via SSH or OpenSearch.

## Installation

For development within the mail-analyzer project:

```bash
# Enter Nix development shell from project root
cd /path/to/mail-analyzer
nix develop .#mailtrace

# Or with direnv (automatic activation)
cd rca-agent/external-tools/mailtrace
direnv allow

# Install dependencies
uv sync
```

For standalone installation:

```bash
$ pip install mailtrace
```

You can copy the example configuration file from the repository:

```
$ cp config.yaml.sample ~/.config/mailtrace.yaml
```

## Usage

```
mailtrace run \
    -c ~/.config/mailtrace.yaml \
    -h mail.example.com \
    -k user@example.com \
    --time "2025-07-21 10:00:00" \
    --time-range 10h
```

You can specify the following parameters on the command line:
- `-c`: Path to the configuration file.
- `-h`: Hostname of the mail server to begin tracing.
- `-k`: Keyword to search for, such as an email address.
- `--time`: The central time for the trace.
- `--time-range`: The duration to search before and after the central time. For example, if `--time` is "10:00" and `--time-range` is "1h", the search will cover from 9:00 to 11:00.

Password-related options are also available:
- `--login-pass`: Password for SSH login authentication.
- `--sudo-pass`: Password for sudo authentication.
- `--opensearch-pass`: Password for OpenSearch authentication.

To help prevent password leakage, you can use the following flags to enter passwords interactively at the prompt: `--ask-login-pass`, `--ask-sudo-pass`, `--ask-opensearch-pass`.

### Continuous Tracing with OpenTelemetry

The `tracing` command continuously queries logs from OpenSearch and generates distributed traces sent to an OpenTelemetry endpoint:

```bash
mailtrace tracing \
    -c ~/.config/mailtrace.yaml \
    --otel-endpoint http://localhost:4317 \
    --ask-opensearch-pass
```

#### Features

- **Continuous monitoring**: Automatically queries logs at regular intervals, controlled by `tracing.sleep_seconds` in the config file
- **Message ID tracking**: Uses message ID as trace ID to maintain email chains across multiple query cycles
  - The message ID is hashed to generate a consistent 128-bit trace ID
  - This ensures that logs from the same email, even if fetched at different times, belong to the same trace
  - For example, if an email has 3 hops and logs from hops 1-2 are fetched in one cycle, and hop 3 in the next cycle, they will still be part of the same trace
- **Queue ID tracking**: Uses queue ID as span ID for consistent span identification
  - Each queue ID is hashed to generate a consistent 64-bit span ID
  - This allows spans to be properly correlated across different fetch cycles
- **Multi-host support**: Queries all hosts defined in the clusters configuration
- **Automatic trace generation**: Groups logs by message ID and generates OpenTelemetry traces
- **Log buffering with hold rounds**: Logs for each message ID are held in a buffer for a configurable number of rounds (`tracing.hold_rounds`) after the last log is seen before being exported. This prevents truncated traces when an email's logs arrive across multiple query windows
- **Late-arrival compensation**: Each query window extends slightly into the past (`tracing.go_back_seconds`) to catch logs whose syslog timestamp predates their OpenSearch ingest time. Duplicate log entries introduced by this overlap are automatically deduplicated

#### Parameters

- `-c, --config-path`: Path to configuration file
- `--otel-endpoint`: OpenTelemetry OTLP endpoint (default: `http://localhost:4317`)
- `--opensearch-pass`: OpenSearch password
- `--ask-opensearch-pass`: Prompt for OpenSearch password interactively

Timing and buffering behaviour is controlled entirely through the `tracing` section of the config file — see [Tracing Configuration](#tracing-configuration) below.

#### Requirements

- Requires OpenSearch configuration in the config file
  - If `method` is not set to `opensearch`, a warning will be logged but the command will still use the OpenSearch configuration
  - Make sure `opensearch_config` section is properly configured with host, credentials, index, and field mappings
- Requires hosts to be configured in the `clusters` section
- Install tracing dependencies: `uv sync --group tracing`

#### Example Workflow

1. Set up your configuration with OpenSearch, clusters, and tracing settings:

```yaml
method: opensearch
clusters:
  mail-cluster:
    - smtp-relay-1.example.com
    - smtp-relay-2.example.com
    - mail-delivery.example.com
opensearch_config:
  host: localhost
  port: 9200
  username: admin
  index: mail-logs-*
  # ... other opensearch settings
tracing:
  sleep_seconds: 60
  hold_rounds: 2
  go_back_seconds: 10
```

2. Start a Jaeger instance to receive traces:

```bash
docker run -d --name jaeger \
  -p 4317:4317 \
  -p 16686:16686 \
  jaegertracing/all-in-one:latest
```

3. Run continuous tracing:

```bash
mailtrace tracing \
    -c ~/.config/mailtrace.yaml \
    --otel-endpoint http://localhost:4317 \
    --ask-opensearch-pass
```

4. View traces in Jaeger UI at `http://localhost:16686`

The tracer will continuously fetch logs every `sleep_seconds` seconds. Logs for each message ID are buffered until no new logs for that ID have been seen for `hold_rounds` consecutive rounds, ensuring complete traces even when an email's logs arrive across multiple query windows. Each query also reaches `go_back_seconds` into the past to catch logs that arrived in OpenSearch later than their syslog timestamp.

### Automatic Tracing with Graph Generation

The `trace` command automatically traces the complete mail flow and generates a Graphviz graph showing the routing path.

#### Graph Format

The generated graph uses a clean, topology-focused format:
- **Nodes**: Actual hostnames where mail was processed (e.g., `smtp-relay-1.example.com`)
- **Edges**: Mail queue IDs showing the flow between hosts (e.g., `ABC123`)
- **Clusters**: When you specify a cluster name with `-h`, the graph starts from the actual physical host where the mail was first found

#### Output to File

Generate a `.dot` file:

```bash
mailtrace graph \
    -c ~/.config/mailtrace.yaml \
    -h mail.example.com \
    -k user@example.com \
    --time "2025-07-21 10:00:00" \
    --time-range 10h \
    -o mail_trace.dot
```

Then visualize with Graphviz:

```bash
dot -Tpng mail_trace.dot -o mail_trace.png
```

#### Output to Stdout

Omit the `-o` option to output the graph directly to stdout:

```bash
mailtrace graph \
    -c ~/.config/mailtrace.yaml \
    -h mail.example.com \
    -k user@example.com \
    --time "2025-07-21 10:00:00" \
    --time-range 10h
```

Or explicitly use `-o -` for stdout:

```bash
mailtrace graph \
    -c ~/.config/mailtrace.yaml \
    -h mail.example.com \
    -k user@example.com \
    --time "2025-07-21 10:00:00" \
    --time-range 10h \
    -o -
```

#### Pipe Directly to Graphviz

You can pipe the output directly to Graphviz for instant visualization:

```bash
mailtrace graph \
    -c ~/.config/mailtrace.yaml \
    -h mail.example.com \
    -k user@example.com \
    --time "2025-07-21 10:00:00" \
    --time-range 10h | dot -Tpng > mail_trace.png
```

Or to SVG for scalable graphics:

```bash
mailtrace graph \
    -c ~/.config/mailtrace.yaml \
    -h mail.example.com \
    -k user@example.com \
    --time "2025-07-21 10:00:00" \
    --time-range 10h | dot -Tsvg > mail_trace.svg
```

#### Example Graph Output

```dot
digraph {
smtp-relay-1.example.com;
smtp-relay-2.example.com;
mail-delivery.example.com;
smtp-relay-1.example.com -> smtp-relay-2.example.com [key=0, label=8DCB211F769];
smtp-relay-2.example.com -> mail-delivery.example.com [key=1, label=9EF8A12BC3D];
}
```

This shows mail with queue ID `8DCB211F769` flowing from `smtp-relay-1` to `smtp-relay-2`, where it received new queue ID `9EF8A12BC3D` before final delivery.

## Using as a Library

Mailtrace can also be used as a Python library in your own scripts:

```python
#!/usr/bin/env python3
"""
Example: Using mailtrace as a library
"""

from mailtrace import (
    load_config,
    select_aggregator,
    trace_mail_flow_to_file,
    query_logs_by_keywords,
)

# Load configuration
config = load_config('config.yaml')

# Select the appropriate aggregator (SSHHost or OpenSearch)
aggregator_class = select_aggregator(config)

# Example 1: Trace mail flow and save to file
trace_mail_flow_to_file(
    config=config,
    aggregator_class=aggregator_class,
    start_host='mail.example.com',
    keywords=['user@example.com'],
    time='2025-07-21 10:00:00',
    time_range='10h',
    output_file='mail_trace.dot'  # Optional: omit or use None for stdout
)

# Example 2: Query logs by keywords only
logs_by_id = query_logs_by_keywords(
    config=config,
    aggregator_class=aggregator_class,
    start_host='mail.example.com',
    keywords=['user@example.com'],
    time='2025-07-21 10:00:00',
    time_range='10h'
)

# Process the results
for mail_id, (host, log_entries) in logs_by_id.items():
    print(f"Mail ID: {mail_id} (from {host})")
    for entry in log_entries:
        print(f"  {entry}")
```

### Available Library Functions

- **`load_config(config_path)`** - Load configuration from a YAML file
- **`select_aggregator(config)`** - Select the appropriate aggregator class (SSHHost or OpenSearch) based on config
- **`trace_mail_flow_to_file(config, aggregator_class, start_host, keywords, time, time_range, output_file=None)`** - Trace mail flow and output as Graphviz dot format (file or stdout)
- **`query_logs_by_keywords(config, aggregator_class, start_host, keywords, time, time_range)`** - Query logs and return mail IDs with their log entries
- **`trace_mail_flow(trace_id, aggregator_class, config, host, graph)`** - Trace a specific mail ID and build a MailGraph
- **`MailGraph()`** - Create and manipulate mail flow graphs
  - `add_hop(from_host, to_host, queue_id)` - Add a mail hop between hosts
  - `to_dot(path=None)` - Write graph to DOT format (file path or stdout if None)

## Configuration

The configuration file supports these parameters:
- `method`: Tracing method, either "ssh" or "opensearch".
- `log_level`: Logging level, one of "DEBUG", "INFO", "WARNING", "ERROR", or "CRITICAL".
- `ssh_config`: Configuration for SSH tracing.
- `opensearch_config`: Configuration for OpenSearch tracing.
- `clusters`: Named groups of hosts for high availability scenarios.
- `domain`: Domain name for hostname resolution (optional).

### SSH Configuration

Example `ssh_config` section:

```yaml
ssh_config:
  username: username
  password: ""
  private_key: /path/to/private.key
  sudo_pass: ""
  sudo: true
  timeout: 10
  ssh_config_file: ~/.ssh/config
  host_config:
    log_files:
      - /var/log/mail.log
    # log_parser: SyslogParser  # Optional - auto-detects format by default
    time_format: "%Y-%m-%dT%H:%M:%S"
  hosts:
    another.mailserver.example.com:
      log_parser: Rfc3164Parser  # Force BSD syslog format if needed
      time_format: "%b %d %H:%M:%S"
```

#### SSH Parameters

- `username`: SSH username for authentication. Required.
- `password`: SSH password for authentication. Optional if `private_key` is provided. For security, it's recommended to provide this via the CLI using the `--ask-login-pass` flag or the `MAILTRACE_SSH_PASSWORD` environment variable.
- `private_key`: Path to the SSH private key file. Optional if `password` is provided. Supports `~` expansion for home directory.
- `sudo_pass`: Password for sudo authentication when accessing logs. For security, it's recommended to provide this via the CLI using the `--ask-sudo-pass` flag or the `MAILTRACE_SUDO_PASSWORD` environment variable.
- `sudo`: Whether to use sudo for reading log files (default: `true`).
- `timeout`: SSH connection timeout in seconds (default: `10`).
- `ssh_config_file`: Path to an SSH config file (e.g., `~/.ssh/config` or a custom config file). Optional. When specified, paramiko will merge settings from this file with the above parameters. SSH config settings take precedence for `hostname`, `user`, `port`, and `identityfile`. This is similar to using the `ssh -F ./my_ssh_config` command.

#### Host Configuration

- `host_config`: Default settings applied to all hosts.
  - `log_files`: List of log file paths to read (required).
  - `log_parser`: Log parser for processing log files (default: `SyslogParser`). Available parsers:
    - `SyslogParser`: Auto-detects RFC 3164 vs RFC 5424 format (recommended)
    - `Rfc5424Parser`: Force RFC 5424 format (ISO 8601 timestamp: `2025-01-01T10:00:00+08:00`)
    - `Rfc3164Parser`: Force RFC 3164 format (BSD syslog: `Feb 1 10:00:00`)
  - `time_format`: Time format string for parsing timestamps (default: `"%Y-%m-%d %H:%M:%S"`). Used for time-based filtering.

- `hosts`: Host-specific configurations, overriding `host_config` for particular hosts. Uses the same format as `host_config`.

#### SSH Config File Example

If you use an SSH config file, you can centralize your SSH settings there. For example, in `~/.ssh/config`:

```
Host mail1.example.com
    User mailuser
    Port 2222
    IdentityFile ~/.ssh/id_rsa_mail

Host mail2.example.com
    User mailuser
    IdentityFile ~/.ssh/id_rsa_mail

Host jumphost
    HostName jump.example.com
    User jumpuser
```

Then in your mailtrace `config.yaml`:

```yaml
ssh_config:
  username: default_user
  private_key: ~/.ssh/id_rsa
  ssh_config_file: ~/.ssh/config
  sudo_pass: "mypassword"
  # ... rest of config
```

When connecting to `mail1.example.com`, mailtrace will use the `User` (mailuser) and `IdentityFile` from the SSH config file, port 2222, etc. You don't need to duplicate these settings in `config.yaml`.

### Loghost Configuration

Loghost is a centralized logging server that collects logs from multiple servers and allows admins to access logs.

To use Mailtrace if you are using a loghost, you need to use SSH as log source and configure it:

1. Set up an SSH config file that redirects SSH connections to mail servers to a loghost. For example, in `~/.ssh/config`:

```
Host loghost
    HostName logs.example.com
    User loguser
    IdentityFile ~/.ssh/id_rsa

Host mx.example.com
    HostName loghost

Host mailer.example.com
    HostName loghost

Host mailpolicy.example.com
    HostName loghost

Host mailbox.example.com
    HostName loghost
```

2. In your `config.yaml`, configure the `log_files` fields for each mail server:

```yaml
ssh_config:
  username: default_user
  private_key: ~/.ssh/id_rsa
  ssh_config_file: ~/.ssh/config
  sudo_pass: "mypassword"
  host_config:
    log_files:
      - /var/log/mail.log
    # log_parser: SyslogParser  # Optional - auto-detects format by default
    time_format: "%Y-%m-%dT%H:%M:%S"
  hosts:
    mx.example.com:
      log_files:
        - /var/log/mx/mail.log
      # log_parser and time_format inherited from host_config
    mailer.example.com:
      log_files:
        - /var/log/mailer/mail.log
      log_parser: Rfc3164Parser  # Override if this host uses different format
      time_format: "%b %d %H:%M:%S"
    mailpolicy.example.com:
      log_files:
        - /var/log/mailpolicy/mail.log
    mailbox.example.com:
      log_files:
        - /var/log/mailbox/mail.log
```

### OpenSearch Configuration

Example `opensearch_config` section:

```yaml
opensearch_config:
  host: "localhost"
  port: 9200
  username: "admin"
  password: ""
  index: "mailtrace-logs-*"
  use_ssl: true
  verify_certs: false
  time_zone: "+00:00"
  timeout: 10
  mapping:
    facility: "log.syslog.facility.name"
    hostname: "host.name"
    message: "message"
    timestamp: "@timestamp"
    service: "log.syslog.appname"
```

#### OpenSearch Parameters

- `host`: Hostname or IP address of the OpenSearch server. Required.
- `port`: Port number for OpenSearch (default: `9200`).
- `username`: OpenSearch username for authentication. Required.
- `password`: OpenSearch password for authentication. For security, it's recommended to provide this via the CLI using the `--ask-opensearch-pass` flag or the `MAILTRACE_OPENSEARCH_PASSWORD` environment variable.
- `index`: Name of the OpenSearch index or index pattern for storing/querying logs (e.g., `mailtrace-logs-*`). Required.
- `use_ssl`: Whether to use SSL/TLS for communication (default: `false`).
- `verify_certs`: Whether to verify SSL certificates (default: `false`). Set to `true` in production for security.
- `time_zone`: Timezone offset for log timestamps (default: `"+00:00"`). Used for time-based filtering.
- `timeout`: Connection timeout in seconds (default: `10`).

#### Field Mapping

The `mapping` section allows you to specify how application fields map to OpenSearch fields:

- `facility`: OpenSearch field for log facility (default: `"log.syslog.facility.name"`).
- `hostname`: OpenSearch field for hostname (default: `"host.name"`).
- `message`: OpenSearch field for log message (default: `"message"`).
- `timestamp`: OpenSearch field for log timestamp (default: `"@timestamp"`).
- `service`: OpenSearch field for service name (default: `"log.syslog.appname"`).

This allows mailtrace to work with different OpenSearch index schemas. Customize these mappings based on your actual field names in OpenSearch.

### Tracing Configuration

The optional `tracing` section controls the behaviour of the `mailtrace tracing` command. All fields have defaults so the section can be omitted entirely.

```yaml
tracing:
  sleep_seconds: 60
  hold_rounds: 2
  go_back_seconds: 10
```

#### Tracing Parameters

- `sleep_seconds`: How long to sleep between log-query iterations (default: `60`). Replaces the former `--interval` CLI flag.
- `hold_rounds`: Number of consecutive rounds in which a message ID must be absent from new query results before its buffered logs are exported as a trace (default: `2`). Increase this if email delivery across your infrastructure can span more than `sleep_seconds * hold_rounds` seconds. Setting it to `0` disables buffering and exports logs immediately, which may produce truncated traces.
- `go_back_seconds`: How far back from the previous query boundary to extend the start of each new query window (default: `10`). This compensates for logs whose syslog timestamp predates their OpenSearch ingest time. Duplicate entries captured by the overlap are automatically discarded. Set to `0` to disable the overlap.

### Clusters Configuration

You can define named clusters for high availability scenarios:

```yaml
clusters:
  mx-cluster-us:
    - mx1.us.example.com
    - mx2.us.example.com
    - mx3.us.example.com
  mx-cluster-eu:
    - mx1.eu.example.com
    - mx2.eu.example.com
```

Then you can trace across a cluster by specifying the cluster name instead of individual hostnames.

## Installation

### Standard Installation

```bash
pip install mailtrace
```

### With Tracing Support

To use the continuous tracing feature, install with the tracing dependency group:

```bash
# Using uv
uv sync --group tracing

# Using pip (after cloning the repository)
pip install -e ".[tracing]"
```

The tracing dependencies include:
- `opentelemetry-api`: OpenTelemetry API
- `opentelemetry-sdk`: OpenTelemetry SDK
- `opentelemetry-exporter-otlp-proto-grpc`: OTLP gRPC exporter for sending traces

### Module Structure

The tracing command is organized into separate modules for maintainability:

- **`models.py`**: Data models for email traces (`EmailTrace` class)
- **`otel.py`**: OpenTelemetry setup and trace generation functions
  - OTLP exporter setup
  - Tracer creation for hosts
  - Trace ID and span ID generation from message/queue IDs
  - Distributed trace span creation
- **`query.py`**: Log querying and grouping logic
  - Query logs from all hosts in clusters
  - Group logs by message ID to maintain email chains
- **`continuous.py`**: Main continuous monitoring loop
  - Periodic log querying at configured intervals
  - Trace generation and sending to OTLP endpoint
- **`__init__.py`**: Module re-exports for public API

## Environment Variables

For security, sensitive information can be provided via environment variables instead of hardcoding in the config file:

- `MAILTRACE_CONFIG`: Path to the configuration file (default: `config.yaml`).
- `MAILTRACE_SSH_PASSWORD`: SSH login password.
- `MAILTRACE_SUDO_PASSWORD`: Sudo authentication password.
- `MAILTRACE_OPENSEARCH_PASSWORD`: OpenSearch authentication password.

## How It Works

An aggregator can read the logs and find out the related ones. It then extracts information from the logs, including `hostname`, `mail_id`, etc.

With the information extracted, it can find out the next stop of the mail flow. The tracing is performed by the `do_trace` function in `aggregator/__init__.py`, the core of this tool.

## Demo

Refer to the `demo` directory for a sample configuration and demo video.
