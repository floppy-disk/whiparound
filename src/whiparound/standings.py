"""ESPN's NHL standings (a different host from the scoreboard): playoff seed per team, and the league's scoring rate.
The seed stands in for the AP rank in the tier logic; the rate is the Poisson model's lambda."""
from dataclasses import dataclass

import httpx

from .espn import UNRANKED
from .http import ClientError


@dataclass(frozen=True)
class Standings:
    ranks: dict[str, int]              # team id -> playoffSeed in its conference; absent teams count as UNRANKED
    goals_per_60: float | None         # goals for per team per game (a game is 60 minutes); None when nothing was played


def split_url(url: str) -> tuple[str, str]:
    """`https://host/path` -> ("https://host", "/path"), so the client's base_url is the host and the path stays a path."""
    u = httpx.URL(url)
    return f"{u.scheme}://{u.netloc.decode()}", u.raw_path.decode()


def _num(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def parse_standings(data: dict) -> Standings:
    """From `children[*].standings.entries[*]`: `team.id` and the stats named playoffSeed, pointsFor, gamesPlayed."""
    ranks: dict[str, int] = {}
    goals = games = 0.0
    for child in data.get("children") or []:
        for entry in (child.get("standings") or {}).get("entries") or []:
            team_id = str((entry.get("team") or {}).get("id") or "")
            stats = {s.get("name"): _num(s.get("value")) for s in entry.get("stats") or []}
            if team_id:
                seed = stats.get("playoffSeed")
                ranks[team_id] = int(seed) if seed else UNRANKED
            goals += stats.get("pointsFor") or 0.0
            games += stats.get("gamesPlayed") or 0.0
    return Standings(ranks, goals / games if games > 0 else None)


class StandingsClient:
    def __init__(self, http: httpx.Client, path: str):
        self.http, self.path = http, path

    def standings(self) -> Standings:
        try:
            r = self.http.get(self.path)
        except httpx.HTTPError as e:
            raise ClientError(f"GET {self.path}: {type(e).__name__}: {e}") from None
        if r.status_code != 200:
            raise ClientError(f"GET {self.path}: HTTP {r.status_code}")
        try:
            body = r.json()
        except ValueError:
            raise ClientError(f"GET {self.path}: not JSON") from None
        if not isinstance(body, dict):
            raise ClientError(f"GET {self.path}: unexpected shape")
        return parse_standings(body)
