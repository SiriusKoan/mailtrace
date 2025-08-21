# Mailtrace

This tool provides command-line access to trace mails via SSH or OpenSearch.

## Installation

```
$ pip install mailtrace
```

## Usage

```
mailtrace run \
    -h mail.example.com \
    -k user@example.com \
    --time "2025-07-21 10:00:00" \
    --time-range 10h
```

The parameters can be specified in CLI:
- `-h`: The hostname of the mail server to start tracing from.
- `-k`: The keyword to search for, e.g., the email address.
- `--time`: The central time for the trace.
- `--time-range`: The duration to search before and after the central time. For example, if `--time` is "10:00" and `--time-range` is "1h", the search will cover from 9:00 to 11:00.

Also, password-related parameters can be specified in CLI:
- `--login-pass`: The password for SSH login authentication.
- `--sudo-pass`: The password for sudo authentication.
- `--opensearch-pass`: The password for OpenSearch authentication.

To prevent password leakage, it is recommended to use the flags to type passwords in prompts: `--ask-login-pass`, `--ask-sudo-pass`, `--ask-opensearch-pass`.

### Config
