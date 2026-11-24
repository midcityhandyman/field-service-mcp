"""Adapter error taxonomy.

Adapters raise these instead of leaking vendor HTTP details. The MCP layer
turns each one into a tool error whose text tells the calling model what
happened and whether retrying makes sense.
"""

from __future__ import annotations


class AdapterError(Exception):
    """Base class for every failure an adapter reports."""

    retryable = False
    hint = "This is not expected to succeed if retried unchanged."

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code

    def for_model(self) -> str:
        """Text shown to the calling model."""
        return f"{self.message} {self.hint}".strip()


class NotFoundError(AdapterError):
    hint = "Check the id with the matching list tool before retrying."


class InvalidRequestError(AdapterError):
    hint = "Fix the arguments and call the tool again."


class ConflictError(AdapterError):
    hint = "Look up the schedule or the current record, then pick different values."


class AuthError(AdapterError):
    hint = "The server's credentials for the field service system are wrong or missing. A person must fix the configuration."


class UpstreamError(AdapterError):
    retryable = True
    hint = "The field service system is unavailable. Retrying later may work."

