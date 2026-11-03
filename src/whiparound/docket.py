"""Teamarr's managed channels -> the docket: ESPN event id -> the Dispatcharr event channel for it."""
from dataclasses import dataclass

import httpx

from .http import ClientError

MANAGED_PATH = "/api/v1/channels/managed"
SPANISH_SUFFIX = "-spanish"


@dataclass(frozen=True)
class Channel:
    event_id: str
    dispatcharr_channel_id: int
    tvg_id: str
    channel_name: str
    home_team: str
    away_team: str


class TeamarrClient:
    def __init__(self, http: httpx.Client):
        self.http = http

    def managed(self) -> list[dict]:
        try:
            r = self.http.get(MANAGED_PATH)
        except httpx.HTTPError as e:
            raise ClientError(f"GET {MANAGED_PATH}: {type(e).__name__}: {e}") from None
        if r.status_code != 200:
            raise ClientError(f"GET {MANAGED_PATH}: HTTP {r.status_code}")
        body = r.json()
        if isinstance(body, dict):
            return body.get("channels") or []
        return body or []


def build_docket(rows: list[dict], league: str = "college-football") -> dict[str, Channel]:
    """Only rows of this Teamarr `league` (the Sport's `teamarr_league`) that still exist and have a Dispatcharr channel. One channel per event: the
    non-Spanish one when both exist. Only these fields are kept (rows also carry logos and plugin internals)."""
    docket: dict[str, Channel] = {}
    for row in rows:
        if (row.get("league") or "") != league or row.get("deleted_at"):
            continue
        cid = row.get("dispatcharr_channel_id")
        event_id = row.get("event_id")
        if cid in (None, "") or event_id in (None, ""):
            continue
        ch = Channel(
            event_id=str(event_id),
            dispatcharr_channel_id=int(cid),
            tvg_id=str(row.get("tvg_id") or ""),
            channel_name=str(row.get("channel_name") or row.get("event_name") or ""),
            home_team=str(row.get("home_team") or ""),
            away_team=str(row.get("away_team") or ""),
        )
        have = docket.get(ch.event_id)
        if have is None or (have.tvg_id.endswith(SPANISH_SUFFIX) and not ch.tvg_id.endswith(SPANISH_SUFFIX)):
            docket[ch.event_id] = ch
    return docket


def load_listed_teams(text: str) -> list[str]:
    out = []
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            out.append(line)
    return out
