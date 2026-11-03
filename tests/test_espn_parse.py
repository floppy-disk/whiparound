import json
from pathlib import Path

from whiparound.espn import parse_rankings, parse_scoreboard

FIX = Path(__file__).with_name("fixtures") / "scoreboard.json"


def test_parse_scoreboard_fields():
    games = {g.event_id: g for g in parse_scoreboard(json.loads(FIX.read_text()))}
    assert set(games) == {"401858488", "401856720", "401856724", "401856700"}
    live = games["401858488"]
    assert live.state == "in" and live.period == 3 and live.clock_s == 853.0
    assert live.home.abbrev == "OSU" and live.home.score == 21 and live.home.rank == 4
    assert live.away.abbrev == "IU" and live.away.score == 17 and live.away.rank == 5
    assert live.home_wp == 0.6109 and live.seconds_left == 1753
    assert live.last_play_type == "Rush" and live.possession == "84"
    assert live.last_play_id == "4018584881230"
    assert live.margin == 4 and live.live and not live.halftime
    assert live.date.isoformat() == "2026-10-17T16:00:00+00:00"

    half = games["401856720"]
    assert half.last_play_id is None
    assert half.halftime and half.live and half.home_wp is None and half.seconds_left is None
    assert half.home.rank == 99 and half.away.rank == 11

    pre = games["401856724"]
    assert pre.state == "pre" and pre.period == 0 and not pre.live

    post = games["401856700"]
    assert post.state == "post" and post.margin == 7 and not post.live


def test_parse_rankings_ap_only():
    data = {"rankings": [
        {"type": "usa", "ranks": [{"current": 1, "team": {"id": "61"}}]},
        {"type": "ap", "ranks": [{"current": 1, "team": {"id": "61"}}, {"current": 25, "team": {"id": "99"}},
                                 {"current": 26, "team": {"id": "7"}}]},
    ]}
    assert parse_rankings(data) == {"61": 1, "99": 25}
    assert parse_rankings({"rankings": []}) == {}
