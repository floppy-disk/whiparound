"""Shared HTTP plumbing: one bounded httpx.Client per service, and URL redaction for every error message."""
import re

import httpx

URL_RE = re.compile(r"[a-z][a-z0-9+.-]*://\S+", re.IGNORECASE)


def redact(text: str) -> str:
    return URL_RE.sub("<url>", text)


def make_http_client(base_url: str, read_timeout: float) -> httpx.Client:
    return httpx.Client(
        base_url=base_url,
        limits=httpx.Limits(max_connections=1, max_keepalive_connections=1),
        timeout=httpx.Timeout(read_timeout, connect=5.0),
    )


class ClientError(Exception):
    """An HTTP failure, with any URL in the message already redacted."""

    def __init__(self, message: str):
        super().__init__(redact(message))
