"""Custom exception classes for mailtrace application.

This module defines custom exception classes that provide more meaningful
error messages and context for various failure scenarios throughout the
application.
"""


class MailtraceError(Exception):
    """Base exception class for all mailtrace errors.

    All custom exceptions in the application should inherit from this class.
    This allows for catching all mailtrace-specific errors with a single except clause.
    """

    def __init__(self, message: str, suggestion: str = ""):
        """Initialize the exception.

        Args:
            message: The error message describing what went wrong
            suggestion: Optional suggestion for how to fix the problem
        """
        self.message = message
        self.suggestion = suggestion
        super().__init__(self.message)

    def __str__(self) -> str:
        """Return a formatted error message with suggestion if available."""
        if self.suggestion:
            return f"{self.message}{'.' if not self.message.endswith('.') else ''} {self.suggestion}"
        return self.message


class ConfigurationError(MailtraceError):
    """Raised when there's an error in the configuration.

    This includes invalid configuration values, missing required settings,
    or improperly formatted configuration files.
    """

    pass


class SSHConnectionError(MailtraceError):
    """Raised when SSH connection fails."""

    pass


class SSHCommandError(MailtraceError):
    """Raised when a remote SSH command execution fails."""

    pass


class OpenSearchConnectionError(MailtraceError):
    """Raised when OpenSearch connection fails."""

    pass


class OpenSearchQueryError(MailtraceError):
    """Raised when OpenSearch query execution fails."""

    pass


class LogParsingError(MailtraceError):
    """Raised when log parsing fails."""

    pass


class FileNotFoundError(MailtraceError):
    """Raised when a required file is not found."""

    pass


class ValidationError(MailtraceError):
    """Raised when input validation fails."""

    pass
