"""ESPN's public scoreboard (college football or NHL, per the Sport profile) and the AP rankings.
Field shapes: `docs/espn-fields.md`."""
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import httpx

from .http import ClientError

if TYPE_CHECKING:
    from .sport import Sport

UNRANKED = 99


@dataclass(frozen=True)
class PeriodRules:
    """How a sport counts time, carried on every Game so the pure leverage functions need no sport argument.
    The defaults are college football's: four 15-minute quarters, ESPN status 23 = halftime."""
    period_s: float = 900.0
    regulation_periods: int = 4
    break_status_id: str = "23"      # ESPN status type id of the scheduled stop (halftime; the NHL's intermission)
    break_midgame: bool = True       # True: the stop is once, at the middle (progress 0.5). False: after every period


@dataclass(frozen=True)
class Side:
    team_id: str
    abbrev: str
    score: int
    rank: int  # 99 = unranked


@dataclass(frozen=True)
class Game:
    event_id: str
    name: str
    short_name: str
    state: str  # pre / in / post
    status_type_id: str
    period: int
    clock_s: float
    home: Side
    away: Side
    home_wp: float | None
    seconds_left: int | None
    last_play_type: str | None
    possession: str | None
    date: datetime
    last_play_id: str | None = None
    wp_carried: bool = False  # home_wp is the last one seen on an earlier poll, not this snapshot's
    rules: PeriodRules = PeriodRules()

    @property
    def margin(self) -> int:
        return abs(self.home.score - self.away.score)

    @property
    def halftime(self) -> bool:
        return self.status_type_id == self.rules.break_status_id

    @property
    def overtime(self) -> bool:
        return self.period > self.rules.regulation_periods

    @property
    def live(self) -> bool:
        return self.state == "in"


def _int(v, default=0) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


def _side(c: dict) -> Side:
    team = c.get("team") or {}
    rank = (c.get("curatedRank") or {}).get("current")
    return Side(
        team_id=str(team.get("id") or ""),
        abbrev=str(team.get("abbreviation") or ""),
        score=_int(c.get("score")),
        rank=_int(rank, UNRANKED) or UNRANKED,
    )


def parse_scoreboard(data: dict, rules: PeriodRules = PeriodRules()) -> list[Game]:
    games = []
    for ev in data.get("events") or []:
        comps = ev.get("competitions") or [{}]
        comp = comps[0]
        competitors = comp.get("competitors") or []
        home = next((c for c in competitors if c.get("homeAway") == "home"), None)
        away = next((c for c in competitors if c.get("homeAway") == "away"), None)
        if home is None or away is None:
            continue
        status = ev.get("status") or comp.get("status") or {}
        stype = status.get("type") or {}
        situation = comp.get("situation") or {}
        last_play = situation.get("lastPlay") or {}
        prob = last_play.get("probability") or {}
        home_wp = prob.get("homeWinPercentage")
        seconds_left = prob.get("secondsLeft")
        date_str = ev.get("date") or ""
        try:
            date = datetime.fromisoformat(date_str.replace("Z", "+00:00")).astimezone(timezone.utc)
        except ValueError:
            date = datetime.fromtimestamp(0, tz=timezone.utc)
        games.append(Game(
            event_id=str(ev.get("id") or ""),
            name=str(ev.get("name") or ""),
            short_name=str(ev.get("shortName") or ""),
            state=str(stype.get("state") or "pre"),
            status_type_id=str(stype.get("id") or ""),
            period=_int(status.get("period")),
            clock_s=float(status.get("clock") or 0.0),
            home=_side(home),
            away=_side(away),
            home_wp=float(home_wp) if home_wp is not None else None,
            seconds_left=_int(seconds_left, None) if seconds_left is not None else None,
            last_play_type=((last_play.get("type") or {}).get("text")) or None,
            possession=str(situation.get("possession")) if situation.get("possession") else None,
            date=date,
            last_play_id=str(last_play.get("id")) if last_play.get("id") not in (None, "") else None,
            rules=rules,
        ))
    return games


@dataclass(frozen=True)
class WpMemory:
    home_wp: float
    seen: datetime
    home_score: int
    away_score: int
    period: int


def carry_wp(games: list[Game], memory: dict[str, WpMemory], now: datetime,
             max_age_s: float = 900.0) -> list[Game]:
    """ESPN sometimes omits the win probability for minutes at a time. Fill an in-play game's missing `home_wp` from
    the last one seen within `max_age_s`, for as long as its score and period are unchanged, and flag it `wp_carried`.
    A change of either score or the period drops the entry. `memory` is updated in place; the input games are not."""
    for event_id in [k for k, m in memory.items() if (now - m.seen).total_seconds() > max_age_s]:
        del memory[event_id]
    out = []
    for g in games:
        if g.state != "in":
            memory.pop(g.event_id, None)
        elif g.home_wp is not None:
            memory[g.event_id] = WpMemory(g.home_wp, now, g.home.score, g.away.score, g.period)
        elif g.event_id in memory:
            m = memory[g.event_id]
            if (m.home_score, m.away_score, m.period) == (g.home.score, g.away.score, g.period):
                g = replace(g, home_wp=m.home_wp, wp_carried=True)
            else:
                del memory[g.event_id]
        out.append(g)
    return out


def parse_rankings(data: dict) -> dict[str, int]:
    """Team id -> AP rank (1-25). Empty if the AP poll is absent."""
    for poll in data.get("rankings") or []:
        if poll.get("type") == "ap":
            out = {}
            for r in poll.get("ranks") or []:
                team_id = str((r.get("team") or {}).get("id") or "")
                rank = _int(r.get("current"))
                if team_id and 1 <= rank <= 25:
                    out[team_id] = rank
            return out
    return {}


class EspnClient:
    def __init__(self, http: httpx.Client, sport: "Sport | None" = None):
        if sport is None:
            from .sport import NCAAF  # late: sport.py imports leverage, which imports this module
            sport = NCAAF
        self.http, self.sport = http, sport

    def _get(self, path: str, params: dict) -> dict:
        try:
            r = self.http.get(path, params=params)
        except httpx.HTTPError as e:
            raise ClientError(f"GET {path}: {type(e).__name__}: {e}") from None
        if r.status_code != 200:
            raise ClientError(f"GET {path}: HTTP {r.status_code}")
        try:
            return r.json()
        except ValueError:
            raise ClientError(f"GET {path}: not JSON") from None

    def scoreboard(self, dates: "str | list[str]") -> list[Game]:
        """`dates`: one ESPN `dates` value (YYYYMMDD, an Eastern day) or several, one GET each, merged by event id with
        the later day winning. ESPN rejects ranges on the NHL scoreboard (HTTP 400, 2026-11-03)."""
        sp = self.sport
        merged: dict[str, Game] = {}
        for day in ([dates] if isinstance(dates, str) else dates):
            for g in parse_scoreboard(self._get(sp.scoreboard_path, {**sp.scoreboard_params, "dates": day}), sp.rules):
                merged[g.event_id] = g
        return list(merged.values())

    def rankings(self) -> dict[str, int]:
        if self.sport.rankings_path is None:
            return {}
        return parse_rankings(self._get(self.sport.rankings_path, {}))
