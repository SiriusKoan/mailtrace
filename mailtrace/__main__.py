import getpass
import re

import click

from mailtrace.utils import LogQuery, PostfixServiceType, SSHSession

from .config import load_config


@click.group()
def cli():
    pass


@cli.command()
@click.option(
    "-h", "--start-host", type=str, required=True, help="The starting host"
)
@click.option(
    "-k",
    "--key",
    type=str,
    required=True,
    help="The keyword, can be email address, domain, etc.",
    multiple=True,
)
@click.option(
    "-K", "--sudo-pass", type=str, required=False, help="The sudo password"
)
@click.option("--ask-sudo-pass", is_flag=True, help="Ask for sudo password")
@click.option("--time", type=str, required=False, help="The time")
@click.option(
    "--time-range",
    type=str,
    required=False,
    help="The time range, e.g. 1d, 10m",
)
def run(start_host, key, sudo_pass, ask_sudo_pass, time, time_range):
    if ask_sudo_pass:
        sudo_pass = getpass.getpass(prompt="Enter sudo password: ")
    config = load_config()
    if time and not time_range or time_range and not time:
        raise ValueError("Time and time-range must be provided together")
    time_range_pattern = re.compile(r"^\d+[dhm]$")
    if time_range and not time_range_pattern.match(time_range):
        raise ValueError("time_range should be in format [0-9]+[dhm]")
    config.ssh_config.sudo_pass = sudo_pass or config.ssh_config.sudo_pass
    if not sudo_pass:
        print(
            "Warning: empty sudo password is provided, no password will be used for sudo"
        )
    print("Running mailtrace...")
    session = SSHSession(start_host, config)
    base_logs = session.query_by(LogQuery(key, time, time_range))
    # print(logs)
    ids = session.get_mail_id(base_logs)
    print(ids)
    logs_by_id = {}
    for mail_id in ids:
        logs_by_id[mail_id] = session.query_by(LogQuery([mail_id]))
    for mail_id, logs in logs_by_id.items():
        print(f"Mail ID: {mail_id}")
        result = ""
        for entry in logs:
            # print(f"{entry.service} {entry.message}")
            if (
                entry.service == PostfixServiceType.SMTP.value
                or entry.service == PostfixServiceType.LMTP.value
            ):
                msg = entry.message
                match = re.search(r".*([0-9]{3})\s2\.0\.0.*", msg)
                if match:
                    code = int(match.group(1))
                    if code == 250:
                        mail_id_match = re.search(
                            r"250.*queued as ([0-9A-Z]+).*", msg
                        )
                        if mail_id_match:
                            mail_id = mail_id_match.group(1)
                            print(f"Queued as mail ID: {mail_id}")
                        relay_match = re.search(r".*relay=([^\s]+),.*", msg)
                        if relay_match:
                            mail_relay_host = relay_match.group(1)
                            print(f"Relay host: {mail_relay_host}")


if __name__ == "__main__":
    cli()
