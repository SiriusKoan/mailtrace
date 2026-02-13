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
mailtrace trace \
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
mailtrace trace \
    -c ~/.config/mailtrace.yaml \
    -h mail.example.com \
    -k user@example.com \
    --time "2025-07-21 10:00:00" \
    --time-range 10h
```

Or explicitly use `-o -` for stdout:

```bash
mailtrace trace \
    -c ~/.config/mailtrace.yaml \
    -h mail.example.com \
    -k user@example.com \
    --time "2025-07-21 10:00:00" \
    --time-range 10h \
    -o -
```

#### Pipe Directly to Graphviz

You can pipe the output directly to Graphviz for instant visualization. Use `2>/dev/null` to suppress logging output (which goes to stderr):

```bash
mailtrace trace \
    -c ~/.config/mailtrace.yaml \
    -h mail.example.com \
    -k user@example.com \
    --time "2025-07-21 10:00:00" \
    --time-range 10h 2>/dev/null | dot -Tpng > mail_trace.png
```

Or to SVG for scalable graphics:

```bash
mailtrace trace \
    -c ~/.config/mailtrace.yaml \
    -h mail.example.com \
    -k user@example.com \
    --time "2025-07-21 10:00:00" \
    --time-range 10h 2>/dev/null | dot -Tsvg > mail_trace.svg
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

### Cluster Flow Conservation Check

The `flow-check` command verifies that every inbound mail to a cluster reaches a terminal state (delivered locally or relayed out), flagging incomplete or bounced flows as problematic.

```bash
mailtrace flow-check \
    -c ~/.config/mailtrace.yaml \
    --cluster mx-cluster \
    --time "2026-01-25 13:00:00" \
    --time-range 1h
```

#### How It Works

1. Queries all hosts in the specified cluster for log entries within the time window
2. Identifies inbound mails (from clients outside the cluster)
3. For each inbound mail, classifies its terminal state:
   - **`delivered_locally`**: Delivered via `postfix/local` or `postfix/virtual`
   - **`relayed_out`**: Relayed to a host outside the cluster
   - **`bounced`**: Non-delivery notification generated
   - **`incomplete`**: No terminal state found in the logs
   - **`internal_relay`**: Relayed within the cluster (traced recursively until terminal)
4. Returns a JSON report with summary and per-flow details

#### Options

- `--cluster` (required): Cluster name as defined in the config file
- `--time`: Reference time center (`YYYY-MM-DD HH:MM:SS`). Defaults to now
- `--time-range`: Duration to search before and after the reference time (e.g., `1h`, `30m`). Default: `1h`
- `-k, --key`: Optional keyword filter (email address or domain). Can be specified multiple times
- `-o, --output`: Output file path for JSON result. Defaults to stdout

#### Output Format

```json
{
  "cluster": "mx-cluster",
  "time": "2026-01-25 13:00:00",
  "time_range": "1h",
  "keywords": null,
  "out_of_window_mail_ids": [],
  "summary": {
    "total_inbound": 4,
    "complete": 4,
    "problematic": 0
  },
  "complete_flows": [
    {
      "inbound_mail_id": "A90771E005C",
      "inbound_host": "csmx1",
      "source": "unknown",
      "status": "complete",
      "terminal_state": "relayed_out",
      "branches": 1
    }
  ],
  "problematic_flows": []
}
```

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
- **`check_cluster_flow(config, aggregator_class, cluster, time, time_range, keywords=None)`** - Check flow conservation for a cluster, returns `FlowCheckResult`
- **`MailGraph()`** - Create and manipulate mail flow graphs
  - `add_hop(from_host, to_host, queue_id)` - Add a mail hop between hosts
  - `to_dot(path=None)` - Write graph to DOT format (file path or stdout if None)
- **`create_server(config, port=8080)`** - Create an MCP server instance
- **`run_server(config, transport='stdio', port=8080)`** - Run the MCP server

## MCP Server (LLM Integration)

Mailtrace includes an MCP (Model Context Protocol) server that allows LLM assistants like Claude to search mail logs and trace mail flows.

### Starting the MCP Server

```bash
# stdio transport (for Claude Code, Cursor, etc.)
uvx --from /path/to/mailtrace mailtrace mcp --config ~/.config/mailtrace.yaml

# SSE transport (for remote access)
uvx --from /path/to/mailtrace mailtrace mcp --config ~/.config/mailtrace.yaml --transport sse --port 8080
```

### Available MCP Resources

**`mailtrace://clusters`** - List available clusters

Returns JSON mapping cluster names to their member hosts:
```json
{
  "mx-cluster": ["mx1.example.com", "mx2.example.com"],
  "smtp-cluster": ["smtp1.example.com", "smtp2.example.com"]
}
```

### Available MCP Tools

**`mailtrace_query_logs`** - Search mail logs by email address or domain

Parameters:
- `host` (required): Mail server hostname to query
- `keywords` (required): List of email addresses or domains to search
- `time`: Reference time (format: `YYYY-MM-DD HH:MM:SS`)
- `time_range`: Time range (e.g., `10h`, `30m`, `1d`)

**`mailtrace_trace_mail`** - Trace mail through the relay chain

Parameters:
- `host` (required): Starting mail server hostname
- `mail_id`: Mail queue ID to trace directly (provide either this or `keywords`)
- `keywords`: Email addresses or domains to search for (finds mail IDs automatically)
- `time`: Reference time for log search
- `time_range`: Time range for log search

**`mailtrace_check_flow`** - Check cluster flow conservation

Verifies that all inbound mail to a cluster reached a terminal state (delivered locally or relayed out).

Parameters:
- `cluster` (required): Cluster name as defined in config
- `time`: Reference time (`YYYY-MM-DD HH:MM:SS`). Defaults to now
- `time_range`: Time range (e.g., `1h`, `30m`). Default: `1h`
- `keywords`: Optional keyword filter (email addresses or domains)

Returns JSON with summary, complete/problematic flows, and out-of-window mail IDs.

### Claude Code Configuration

Add to your Claude Code MCP settings:

```json
{
  "mcpServers": {
    "mailtrace": {
      "command": "uvx",
      "args": ["--from", "/path/to/mailtrace", "mailtrace", "mcp", "--config", "/path/to/config.yaml"]
    }
  }
}
```

### Testing with MCP Inspector

You can test the MCP tools interactively using the MCP Inspector:

```bash
# Create a config file for the inspector
cat > /tmp/mcp-config.json << 'EOF'
{
  "mcpServers": {
    "mailtrace": {
      "command": "uvx",
      "args": ["--from", "/path/to/mailtrace", "mailtrace", "mcp", "--config", "/path/to/config.yaml"]
    }
  }
}
EOF

# Run the inspector
npx @modelcontextprotocol/inspector --config /tmp/mcp-config.json --server mailtrace
```

This opens a web interface where you can manually trigger `mailtrace_query_logs` and `mailtrace_trace_mail` with custom parameters.

### Security Considerations

- **SSE transport has no authentication**: When using `--transport sse`, the server listens on a network port without authentication. Only use SSE transport on trusted networks or behind a reverse proxy with authentication.
- **Cluster resource exposes hostnames**: The `mailtrace://clusters` resource returns internal server hostnames. Ensure MCP clients are trusted.
- **Credentials from config file**: Authentication credentials (SSH passwords, OpenSearch passwords) are read from the config file. The MCP server never exposes these credentials in responses.
- **Input validation**: All inputs are validated using Pydantic with whitespace stripping and required field checks.

### Example LLM Workflow

**Option 1: Direct trace with keywords** (recommended for simplicity)
```
mailtrace_trace_mail(host="mail.example.com", keywords=["user@example.com"], time="2025-01-24 10:00:00", time_range="2h")
```

**Option 2: Two-step workflow** (when you need to inspect logs first)
1. Query logs to find mail IDs:
   ```
   mailtrace_query_logs(host="mail.example.com", keywords=["user@example.com"], time="2025-01-24 10:00:00", time_range="2h")
   ```

2. Trace a specific mail ID:
   ```
   mailtrace_trace_mail(host="mail.example.com", mail_id="ABC123DEF", time="2025-01-24 10:00:00", time_range="2h")
   ```

The trace returns a Graphviz DOT graph showing the mail flow path.

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

## Environment Variables

For security, sensitive information can be provided via environment variables instead of hardcoding in the config file:

- `MAILTRACE_CONFIG`: Path to the configuration file (default: `config.yaml`).
- `MAILTRACE_SSH_PASSWORD`: SSH login password.
- `MAILTRACE_SUDO_PASSWORD`: Sudo authentication password.
- `MAILTRACE_OPENSEARCH_PASSWORD`: OpenSearch authentication password.

## How It Works

An aggregator can read the logs and find out the related ones. It then extracts information from the logs, including `hostname`, `mail_id`, etc.

With the information extracted, it can find out the next stop of the mail flow. The tracing is performed by the `do_trace` function in `aggregator/__init__.py`, the core of this tool.

### Message-ID Based Batch Fetching

Mailtrace uses Message-ID headers to efficiently trace mail across multiple hosts. Instead of querying each queue-ID individually (N+1 queries), it:

1. Searches by keyword (time-restricted) to find initial queue-IDs and Message-IDs
2. Queries by Message-ID (no time restriction) to discover all related queue-IDs across hops
3. Batch-fetches logs for all discovered queue-IDs in a single query

This reduces the typical query count from **1 + N** (where N = number of queue-IDs) to **M + 2** (where M = unique Message-IDs, typically 1).

### Message-ID Prerequisite

For efficient cross-host tracing, mail messages must have a `Message-ID` header. Postfix adds this by default via the `cleanup` daemon when `always_add_missing_headers = yes` (the default setting).

If your Postfix has this set to `no`, messages arriving without a `Message-ID` header will not receive one, and mailtrace will fall back to less efficient per-queue-ID queries:

```
# In Postfix main.cf (this is the default)
always_add_missing_headers = yes
```

## Demo

Refer to the `demo` directory for a sample configuration and demo video.
