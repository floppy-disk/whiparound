"""One Discord webhook post per switch. The webhook URL is a credential: it is never logged or raised."""
import httpx

from .http import ClientError

MAX_LEN = 1900


def post(http: httpx.Client, webhook_url: str, text: str) -> None:
    if not webhook_url:
        return
    try:
        r = http.post(webhook_url, json={"content": text[:MAX_LEN]})
    except httpx.HTTPError as e:
        raise ClientError(f"discord: {type(e).__name__}") from None
    if r.status_code >= 400:
        raise ClientError(f"discord: HTTP {r.status_code}")
