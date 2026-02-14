"""Password handling utilities for CLI."""

import getpass
import logging

from mailtrace.config import Config, Method

logger = logging.getLogger("mailtrace")


def prompt_password(
    prompt: str, ask: bool, provided: str | None
) -> str | None:
    """
    Prompt for password if asked, otherwise return provided value.

    Args:
        prompt: The prompt message to display.
        ask: Whether to prompt for password.
        provided: The pre-provided password value.

    Returns:
        The password string or None.
    """
    if ask:
        return getpass.getpass(prompt=prompt)
    return provided


def handle_passwords(
    config: Config,
    ask_login_pass: bool,
    login_pass: str | None,
    ask_sudo_pass: bool,
    sudo_pass: str | None,
    ask_opensearch_pass: bool,
    opensearch_pass: str | None,
) -> None:
    """
    Handle password input and assignment for SSH, sudo, and OpenSearch connections.

    Prompts the user for passwords if requested, assigns them to the config,
    and logs warnings for empty passwords.

    Args:
        config: The configuration object containing connection settings.
        ask_login_pass: Whether to prompt for login password.
        login_pass: The login password (may be None).
        ask_sudo_pass: Whether to prompt for sudo password.
        sudo_pass: The sudo password (may be None).
        ask_opensearch_pass: Whether to prompt for OpenSearch password.
        opensearch_pass: The OpenSearch password (may be None).
    """
    if config.method == Method.SSH:
        login_pass = prompt_password(
            "Enter login password: ", ask_login_pass, login_pass
        )
        config.ssh_config.password = login_pass or config.ssh_config.password
        if not config.ssh_config.password:
            logger.warning(
                "Empty login password - no password will be used for login"
            )

        sudo_pass = prompt_password(
            "Enter sudo password: ", ask_sudo_pass, sudo_pass
        )
        config.ssh_config.sudo_pass = sudo_pass or config.ssh_config.sudo_pass
        if not config.ssh_config.sudo_pass:
            logger.warning(
                "Empty sudo password - no password will be used for sudo"
            )

    elif config.method == Method.OPENSEARCH:
        opensearch_pass = prompt_password(
            "Enter opensearch password: ", ask_opensearch_pass, opensearch_pass
        )
        config.opensearch_config.password = (
            opensearch_pass or config.opensearch_config.password
        )
        if not config.opensearch_config.password:
            logger.warning(
                "Empty opensearch password - no password will be used for opensearch"
            )
    else:
        logger.warning(
            f"Unknown method: {config.method}. No password handling."
        )
