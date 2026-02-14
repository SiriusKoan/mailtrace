"""Config validation and diagnostic reporting for mailtrace."""

from __future__ import annotations

from dataclasses import fields
from typing import Any

from mailtrace.config import Config, OpenSearchMappingConfig

# Required mapping fields that must have a non-empty value
_REQUIRED_FIELDS = {"timestamp", "message", "hostname"}

# Nice-to-have fields that trigger warnings when missing
_NICE_TO_HAVE = {
    "facility": "facility filtering won't be applied",
    "service": ("service name won't appear in parsed output"),
    "queueid": ("queue ID lookups will fall back to" " message text search"),
    "message_id": (
        "Message-ID lookups will fall back to" " message text search"
    ),
}


def check_config(config: Config) -> dict[str, Any]:
    """Validate config and return a diagnostic report.

    Returns a dict with:
        method: The configured connection method
        errors: List of critical errors (required fields missing)
        warnings: List of warnings (nice-to-have fields missing)
        configured_fields: List of field names that are configured
        unconfigured_fields: List of field names not configured
    """
    mapping = config.opensearch_config.mapping
    errors: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []
    configured: list[str] = []
    unconfigured: list[str] = []

    for f in fields(OpenSearchMappingConfig):
        value = getattr(mapping, f.name)
        if value:
            configured.append(f.name)
        else:
            unconfigured.append(f.name)
            if f.name in _REQUIRED_FIELDS:
                errors.append(
                    {
                        "field": f.name,
                        "message": (
                            f"Required field '{f.name}'" " is not configured"
                        ),
                    }
                )
            elif f.name in _NICE_TO_HAVE:
                warnings.append(
                    {
                        "field": f.name,
                        "message": _NICE_TO_HAVE[f.name],
                    }
                )

    return {
        "method": config.method.value,
        "errors": errors,
        "warnings": warnings,
        "configured_fields": configured,
        "unconfigured_fields": unconfigured,
    }
