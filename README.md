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

### SSH Configuration

Example `ssh_config` section:

```yaml
ssh_config:
  username: username
  password: ""
  private_key: /path/to/private.key
  sudo_pass: ""
  sudo: true
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

- `username`: SSH username.
- `password`: SSH password. For security, it's recommended to provide this via the CLI using the `--ask-login-pass` flag.
- `private_key`: Path to the SSH private key file.
- `sudo_pass`: Sudo password. For security, it's recommended to provide this via the CLI using the `--ask-sudo-pass` flag.
- `sudo`: Whether to use sudo for reading logs.
- `host_config`: Default settings for hosts.
  - `log_files`: List of log files to read.
  - `log_parser`: Log parser for processing log files.
  - `time_format`: Time format in logs, used for time comparison.
- `hosts`: Host-specific configurations, using the same format as `host_config`.

### OpenSearch Configuration

Example `opensearch_config` section:

```yaml
opensearch_config:
  host: ""
  port: 9200
  username: username
  password: ""
  index: ""
  use_ssl: false
  verify_certs: false
  time_zone: "+00:00"
```

- `host`: Hostname or IP address of the OpenSearch server.
- `port`: Port number for OpenSearch.
- `username`: OpenSearch username.
- `password`: OpenSearch password. For security, it's recommended to provide this via the CLI using the `--ask-opensearch-pass` flag.
- `index`: Name of the OpenSearch index for storing logs.
- `use_ssl`: Whether to use SSL for communication.
- `verify_certs`: Whether to verify SSL certificates.
- `time_zone`: Time zone of the OpenSearch server.
