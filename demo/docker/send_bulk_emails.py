#!/usr/bin/env python3

import argparse
import logging
import smtplib
import socket
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from email.mime.text import MIMEText

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

CONFIGS = [
    {
        "server": "127.0.0.1",
        "port": 10025,
        "from": "user1@example.com",
        "to": ["user2@example.com"],
        "helo": None,
    },
    {
        "server": "127.0.0.1",
        "port": 20025,
        "from": "me@siriuskoan.one",
        "to": ["user1@example.com", "user1@example2.com"],
        "helo": "siriuskoan.one",
    },
]

NUM_THREADS = 64
SMTP_TIMEOUT_SECONDS = 30


def build_configs(mx_port=10025, mailpolicy_port=20025):
    """Return SMTP endpoint configurations for one benchmark stack."""
    return [
        {**CONFIGS[0], "port": mx_port},
        {**CONFIGS[1], "port": mailpolicy_port},
    ]


def generate_message_id():
    """Generate a unique RFC 5322-compliant Message-ID."""
    try:
        hostname = socket.getfqdn()
    except Exception:
        hostname = "localhost"

    unique_id = f"{int(time.time() * 1000000)}.{uuid.uuid4().hex[:16]}"

    message_id = f"<{unique_id}@{hostname}>"
    return message_id


def build_message(conf):
    msg = MIMEText("This is a stress test email.")
    msg["Subject"] = "SMTP Stress Test"
    msg["From"] = conf["from"]
    msg["To"] = ", ".join(conf["to"])
    msg["Message-ID"] = generate_message_id()

    return msg


def send_message(conf):
    msg = build_message(conf)
    try:
        with smtplib.SMTP(
            conf["server"], conf["port"], timeout=SMTP_TIMEOUT_SECONDS
        ) as server:
            if conf["helo"]:
                server.ehlo(conf["helo"])
            server.sendmail(conf["from"], conf["to"], msg.as_string())
        logger.debug(
            "Sent via port %s to %s - Message-ID: %s",
            conf["port"],
            conf["to"],
            msg["Message-ID"],
        )
        return True
    except Exception as e:
        logger.error(f"Error: {e}")
        return False


def worker(worker_index, total_emails, emails_per_sec, started_at, conf):
    succeeded = 0
    for index in range(worker_index, total_emails, NUM_THREADS):
        deadline = started_at + index / emails_per_sec
        remaining = deadline - time.monotonic()
        if remaining > 0:
            time.sleep(remaining)
        succeeded += send_message(conf)
    return succeeded


def send_emails(emails_per_sec, duration, configs=None):
    """Schedule email at the global rate and return counts and elapsed time."""
    total_emails = int(emails_per_sec * duration)
    endpoint_configs = CONFIGS if configs is None else configs
    worker_count = min(NUM_THREADS, total_emails)

    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        configs = [
            endpoint_configs[index % len(endpoint_configs)]
            for index in range(worker_count)
        ]
        started_at = time.monotonic()
        futures = [
            executor.submit(
                worker,
                worker_index,
                total_emails,
                emails_per_sec,
                started_at,
                configs[worker_index],
            )
            for worker_index in range(worker_count)
        ]
        succeeded = sum(future.result() for future in futures)
        elapsed = time.monotonic() - started_at

    return succeeded, total_emails - succeeded, elapsed


def main():
    parser = argparse.ArgumentParser(
        description="Send bulk emails with specified rate"
    )
    parser.add_argument(
        "N", type=float, help="Number of emails to send per second"
    )
    parser.add_argument("T", type=float, help="Duration in seconds")
    parser.add_argument("--mx-port", type=int, default=10025)
    parser.add_argument("--mailpolicy-port", type=int, default=20025)

    args = parser.parse_args()

    emails_per_sec = args.N
    duration = args.T

    if emails_per_sec <= 0:
        parser.error("N must be positive")
    if duration <= 0:
        parser.error("T must be positive")

    total_emails = int(emails_per_sec * duration)

    logger.info(
        f"Starting to send {emails_per_sec} emails/sec for {duration} seconds "
        f"(total: ~{total_emails} emails) with {NUM_THREADS} threads"
    )

    try:
        sent_count, failed_count, elapsed_time = send_emails(
            emails_per_sec,
            duration,
            build_configs(args.mx_port, args.mailpolicy_port),
        )
    except KeyboardInterrupt:
        logger.warning("Interrupted by user")
        return 130

    logger.info(
        "Completed! Sent %d emails, failed %d, in %.2f seconds "
        "(%.2f emails/sec)",
        sent_count,
        failed_count,
        elapsed_time,
        sent_count / elapsed_time,
    )
    return 1 if failed_count else 0


if __name__ == "__main__":
    raise SystemExit(main())
