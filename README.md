# Mailtrace

Mailtrace is a command-line tool for tracing emails via SSH or OpenSearch.

## Installation

```
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
    log_parser: NoSpaceInDatetimeParser
    time_format: "%Y-%m-%dT%H:%M:%S"
  hosts:
    another.mailserver.example.com:
      log_parser: DayOfWeekParser
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
  - `log_parser`: Log parser for processing log files (required). Available parsers: `NoSpaceInDatetimeParser`, `DayOfWeekParser`, etc.
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

## Demo

Refer to the `demo` directory for a sample configuration and demo video.
