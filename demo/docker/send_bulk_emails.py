#!/usr/bin/env python3

import random
import smtplib
import sys
import time
import argparse
from email.mime.text import MIMEText
from concurrent.futures import ThreadPoolExecutor
from threading import Event, Lock
import logging
import socket
import uuid

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
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

NUM_THREADS = 8
sent_count = 0
sent_lock = Lock()
stop_event = Event()


def generate_message_id():
    """Generate a RFC 5322 compliant Message-ID.

    Format: <local-part@domain>
    where local-part is typically a unique identifier (timestamp + UUID)
    and domain is the hostname
    """
    try:
        hostname = socket.getfqdn()
    except Exception:
        hostname = "localhost"

    # Generate unique local part using UUID and random component
    unique_id = f"{int(time.time() * 1000000)}.{uuid.uuid4().hex[:16]}"

    message_id = f"<{unique_id}@{hostname}>"
    return message_id


def send_random_mail():
    conf = random.choice(CONFIGS)

    msg = MIMEText("This is a stress test email.")
    msg["Subject"] = "SMTP Stress Test"
    msg["From"] = conf["from"]
    msg["To"] = ", ".join(conf["to"])
    msg["Message-ID"] = generate_message_id()

    try:
        with smtplib.SMTP(conf["server"], conf["port"]) as server:
            if conf["helo"]:
                server.ehlo(conf["helo"])
            server.sendmail(conf["from"], conf["to"], msg.as_string())
            logger.info(f"Sent via port {conf['port']} to {conf['to']} - Message-ID: {msg['Message-ID']}")
            return True
    except Exception as e:
        logger.error(f"Error: {e}")
        return False


def worker(emails_per_sec, duration):
    """Worker thread that sends emails at the specified rate."""
    global sent_count
    start_time = time.time()
    interval = 1.0 / emails_per_sec
    next_send_time = start_time

    while not stop_event.is_set():
        current_time = time.time()
        elapsed = current_time - start_time

        # Check if we've exceeded the duration
        if elapsed >= duration:
            break

        # Send email if it's time
        if current_time >= next_send_time:
            send_random_mail()
            with sent_lock:
                sent_count += 1
            next_send_time += interval
        else:
            # Sleep a bit to avoid busy waiting
            time.sleep(0.001)


def main():
    parser = argparse.ArgumentParser(
        description="Send bulk emails with specified rate"
    )
    parser.add_argument(
        "N",
        type=float,
        help="Number of emails to send per second"
    )
    parser.add_argument(
        "T",
        type=float,
        help="Duration in seconds"
    )

    args = parser.parse_args()

    emails_per_sec = args.N
    duration = args.T

    total_emails = int(emails_per_sec * duration)

    logger.info(
        f"Starting to send {emails_per_sec} emails/sec for {duration} seconds "
        f"(total: ~{total_emails} emails) with {NUM_THREADS} threads"
    )

    start_time = time.time()

    try:
        with ThreadPoolExecutor(max_workers=NUM_THREADS) as executor:
            # Submit workers for each thread
            futures = [
                executor.submit(worker, emails_per_sec / NUM_THREADS, duration)
                for _ in range(NUM_THREADS)
            ]

            # Wait for all futures to complete
            for future in futures:
                future.result()

    except KeyboardInterrupt:
        logger.warning("Interrupted by user")
        stop_event.set()

    elapsed_time = time.time() - start_time
    logger.info(
        f"Completed! Sent {sent_count} emails in {elapsed_time:.2f} seconds "
        f"({sent_count/elapsed_time:.2f} emails/sec)"
    )


if __name__ == "__main__":
    main()
