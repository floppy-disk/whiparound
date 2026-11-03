"""TEAM_WEIGHTING=0: the listed teams and the ranks count for nothing; the late-close shield still applies."""
import json
from dataclasses import replace
from zoneinfo import ZoneInfo

from whiparound.leverage import (TIER_LISTED, TIER_REST, Params, late_close, leverage, listed_index, rank_candidates,
                                  score, tier, upset)
from whiparound.log import JsonlLog
from whiparound.runner import Runner
from conftest import make_channel, make_game

ON = Params()
OFF = replace(Params(), team_weighting=False)
LISTED = ["Pittsburgh Panthers", "Notre Dame Fighting Irish"]


def test_listed_index_and_tier_ignore_the_list():
    pitt = make_channel("1", home_team="Syracuse Orange", away_team="Pittsburgh Panthers")
    assert listed_index(pitt, LISTED, ON) == 0 and listed_index(pitt, LISTED) == 0
    assert listed_index(pitt, LISTED, OFF) == len(LISTED)
    g = make_game("1", home_wp=0.5)
    assert tier(g, pitt, LISTED, {}, ON) == TIER_LISTED and tier(g, pitt, LISTED, {}, OFF) == TIER_REST
    assert score(g, pitt, LISTED, {}, OFF).listed_index == len(LISTED)


def test_ranks_count_for_nothing():
    other = make_channel("2", home_team="Rice Owls", away_team="Tulsa Golden Hurricane")
    g = make_game("2", home_wp=0.5, period=4, clock_s=0.0, home_rank=4, away_rank=5)
    assert leverage(g, {}, ON) > leverage(g, {}, OFF)                               # no both-ranked bonus
    assert leverage(g, {}, OFF) == leverage(make_game("2", home_wp=0.5, period=4, clock_s=0.0), {}, ON)
    assert score(g, other, [], {"h": 3}, OFF).inputs["home_rank"] == 99
    assert tier(g, other, [], {}, OFF) == TIER_REST


def test_no_upset_alert_with_weighting_off():
    g = make_game("1", period=3, clock_s=300.0, home_score=7, away_score=10, home_rank=3, home_wp=0.3)
    assert upset(g, {}, ON) and not upset(g, {}, OFF)


def test_a_listed_game_does_not_outrank_a_closer_unlisted_one():
    docket = {"1": make_channel("1", home_team="Syracuse Orange", away_team="Pittsburgh Panthers"),
              "2": make_channel("2", home_team="Rice Owls", away_team="Tulsa Golden Hurricane")}
    listed_game = make_game("1", period=2, clock_s=600.0, home_score=10, away_score=3, home_wp=0.80)  # listed, not close
    close_game = make_game("2", period=2, clock_s=600.0, home_wp=0.5)
    assert [s.event_id for s in rank_candidates([listed_game, close_game], docket, LISTED, {}, ON)] == ["1", "2"]
    off = rank_candidates([listed_game, close_game], docket, LISTED, {}, OFF)
    assert [s.event_id for s in off] == ["2", "1"] and {s.tier for s in off} == {TIER_REST}


def test_shield_still_applies_and_covers_any_close_late_game():
    docket = {"1": make_channel("1", home_team="Rice Owls", away_team="Tulsa Golden Hurricane"),
              "2": make_channel("2", home_team="Utah State", away_team="Fresno State")}
    kick = make_game("2", period=1, clock_s=900.0, home_wp=0.5, home_score=0, away_score=0)
    late = make_game("1", period=4, clock_s=500.0, home_score=21, away_score=17, home_wp=0.75)
    assert not late_close(late, TIER_REST, ON)                       # weighting on: an unranked game is not shielded
    assert late_close(late, TIER_REST, OFF)
    ranked = rank_candidates([kick, late], docket, [], {}, OFF)
    assert ranked[0].event_id == "1" and ranked[0].shielded and not ranked[1].shielded
    assert not rank_candidates([kick, late], docket, [], {}, ON)[0].shielded


def test_a_decided_game_is_not_shielded_even_with_weighting_off():
    decided_late = make_game("1", period=4, clock_s=20.0, home_score=21, away_score=17, home_wp=0.995)
    assert not late_close(decided_late, TIER_REST, OFF)


def test_cycle_line_carries_team_weighting(tmp_path, t0):
    class Espn:
        def scoreboard(self, dates):
            return [make_game("1", home_wp=0.5)]

        def rankings(self):
            return {}

    class Teamarr:
        def managed(self):
            return [{"event_id": "1", "league": "college-football", "dispatcharr_channel_id": 1001,
                     "tvg_id": "t", "home_team": "Home", "away_team": "Pittsburgh Panthers", "channel_name": "x"}]

    for params, want in ((ON, True), (OFF, False)):
        listed = tmp_path / "l.txt"
        listed.write_text("Pittsburgh Panthers\n")
        path = tmp_path / f"{want}.jsonl"
        r = Runner(espn=Espn(), teamarr=Teamarr(), dispatcharr=None, discord_http=None, webhook="",
                   log=JsonlLog(path), params=params, listed_path=listed, channel_id=None, dry_run=True, cycle_s=60,
                   delay_s=0, docket_refresh_s=600, rankings_refresh_s=21600, tz=ZoneInfo("America/New_York"))
        r.cycle(t0)
        cyc = [json.loads(l) for l in path.read_text().splitlines() if '"cycle"' in l][0]
        assert cyc["team_weighting"] is want
        assert cyc["pick"]["tier_word"] == ("listed team" if want else "other")


def test_use_ranks_off_keeps_listed_teams_but_drops_the_ranked_tier():
    """USE_RANKS=0 (the human, 2026-11-03): playoff spots / AP rank count for nothing; the listed list still does."""
    from dataclasses import replace
    from whiparound.leverage import TIER_LISTED, TIER_REST, Params, tier, both_ranked, upset
    from conftest import make_channel, make_game
    p = replace(Params(), use_ranks=False)
    ranks = {"h": 1, "a": 2}
    g = make_game("1", period=3, clock_s=600.0, home_score=10, away_score=7, home_wp=0.6)
    assert tier(g, make_channel("1", home_team="X", away_team="Y"), [], ranks, p) == TIER_REST
    assert tier(g, make_channel("1", home_team="X", away_team="Y"), ["X"], ranks, p) == TIER_LISTED
    assert not both_ranked(g, ranks, p) and not upset(g, ranks, p)
    assert tier(g, make_channel("1", home_team="X", away_team="Y"), [], ranks, Params()) != TIER_REST
