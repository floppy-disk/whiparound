"""The runner under the NHL profile: dates, standings refresh, lambda, text."""
import json
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from whiparound.http import ClientError
from whiparound.log import JsonlLog
from whiparound.runner import Runner
from whiparound.sport import NHL
from whiparound.standings import Standings
from dataclasses import replace
from conftest import make_game

T = datetime(2026, 11, 3, 3, 30, tzinfo=timezone.utc)   # 22:30 on Nov 2 in New York


class FakeEspn:
    def __init__(self, games=()):
        self.games, self.dates = list(games), []

    def scoreboard(self, dates):
        self.dates.append(dates)
        return list(self.games)

    def rankings(self):
        raise AssertionError("the NHL profile has no poll")


class FakeStandings:
    def __init__(self, result=None):
        self.result, self.calls = result, 0

    def standings(self):
        self.calls += 1
        if self.result is None:
            raise ClientError("GET /x: HTTP 503")
        return self.result


class FakeTeamarr:
    def managed(self):
        return [{"event_id": "1", "league": "nhl", "dispatcharr_channel_id": 1003, "tvg_id": "teamarr-event-1",
                 "home_team": "Washington Capitals", "away_team": "Boston Bruins", "channel_name": "BOS vs WSH"},
                {"event_id": "2", "league": "college-football", "dispatcharr_channel_id": 1002, "tvg_id": "t2",
                 "home_team": "X", "away_team": "Y", "channel_name": "Y vs X"}]


def make_runner(tmp_path, espn, standings, params=None, **kw):
    listed = tmp_path / "listed.txt"
    listed.write_text("Washington Capitals\n")
    return Runner(espn=espn, teamarr=FakeTeamarr(), dispatcharr=None, discord_http=None, webhook="",
                  log=JsonlLog(tmp_path / "log.jsonl"), params=params or NHL.params, listed_path=listed,
                  channel_id=None, dry_run=True, cycle_s=60, delay_s=0, docket_refresh_s=600,
                  rankings_refresh_s=21600, tz=ZoneInfo("America/New_York"), sport=NHL, standings=standings, **kw)


def read_log(tmp_path):
    return [json.loads(l) for l in (tmp_path / "log.jsonl").read_text().splitlines()]


def hockey(event_id="1", **kw):
    kw.setdefault("home_wp", None)
    return replace(make_game(event_id, **kw), rules=NHL.rules)


def test_cycle_queries_todays_day_and_the_nhl_docket(tmp_path):
    espn = FakeEspn([hockey("1", period=3, clock_s=300.0, home_score=2, away_score=2)])
    r = make_runner(tmp_path, espn, FakeStandings(Standings({"h": 2, "a": 5}, 3.4)))
    r.cycle(T)
    assert espn.dates == [["20261102"]]   # T is an evening: today only; the previous day joins before 04:00 ET
    log = read_log(tmp_path)
    assert [e["event"] for e in log] == ["docket", "rankings", "cycle", "switch"]
    assert log[0]["channels"] == 1                                  # the college-football row is not ours
    assert log[1]["source"] == "standings" and log[1]["ranked"] == 2 and log[1]["goals_per_60"] == 3.4
    cyc = log[2]
    assert cyc["pick"]["tier_word"] == "listed team" and cyc["pick"]["inputs"]["wp_source"] == "poisson"
    assert cyc["pick"]["inputs"]["home_wp"] == 0.5


def test_lambda_is_replaced_from_the_standings_and_kept_when_the_fetch_fails(tmp_path):
    espn = FakeEspn()
    standings = FakeStandings(Standings({}, 3.4))
    r = make_runner(tmp_path, espn, standings)
    r.refresh_rankings(T)
    assert r.params.goals_per_60 == 3.4 and r.decider.p.goals_per_60 == 3.4
    standings.result = None
    r.refresh_rankings(T)
    assert r.params.goals_per_60 == 3.4
    assert read_log(tmp_path)[-1] == {**read_log(tmp_path)[-1], "event": "espn_error", "what": "standings"}
    standings.result = Standings({"7": 1}, None)                     # nothing played yet: keep the last rate
    r.refresh_rankings(T)
    assert r.params.goals_per_60 == 3.4 and r.ranks == {"7": 1}


def test_state_and_discord_text_name_the_sport(tmp_path, monkeypatch):
    posts = []
    monkeypatch.setattr("whiparound.runner.discord_post", lambda http, hook, text: posts.append(text))
    espn = FakeEspn([hockey("1", period=3, clock_s=300.0, home_score=1, away_score=2, home_id="h", away_id="a",
                            home_rank=99)])
    r = make_runner(tmp_path, espn, FakeStandings(Standings({"h": 2, "a": 11}, 3.0)))
    r.webhook = "hook"
    assert r.state()["sport"] == "nhl"
    r.cycle(T)
    alerts = [e for e in read_log(tmp_path) if e["event"] == "upset_alert"]
    assert len(alerts) == 1 and alerts[0]["home_rank"] == 2 and alerts[0]["away_rank"] == 11
    assert any("P3 5:00" in t and t.startswith("⚠ Upset alert") for t in posts), posts
