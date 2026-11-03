"""NHL standings -> playoff seeds and lambda, and the middle tier built on them."""
import json
from pathlib import Path

import pytest

from whiparound.leverage import (TIER_LISTED, TIER_RANKED, TIER_REST, Params, leverage, rank_candidates, score, tier,
                                  upset)
from whiparound.sport import NHL
from whiparound.standings import parse_standings
from conftest import make_channel, make_game

FIX = Path(__file__).with_name("fixtures") / "standings.json"
P = NHL.params
LISTED = ["Washington Capitals"]


def nhl_game(event_id="1", home_id="h", away_id="a", **kw):
    kw.setdefault("home_wp", None)
    kw.setdefault("period", 2)
    kw.setdefault("clock_s", 600.0)
    return make_game(event_id, home_id=home_id, away_id=away_id, **kw)


def test_parse_standings_fixture():
    data = json.loads(FIX.read_text())
    st = parse_standings(data)
    assert st.ranks["7"] == 1 and st.ranks["21"] == 8 and st.ranks["2"] == 9 and st.ranks["29"] == 16
    assert len(st.ranks) == 12
    # goals for 63+55+52+60+43+29+64+52+54+44+42+47 = 605 over 179 games played, per team per game
    assert st.goals_per_60 == pytest.approx(605 / 179)


def test_parse_standings_guards():
    assert parse_standings({}).goals_per_60 is None and parse_standings({}).ranks == {}
    zero = {"children": [{"standings": {"entries": [
        {"team": {"id": "1"}, "stats": [{"name": "playoffSeed", "value": 0}, {"name": "pointsFor", "value": 0},
                                         {"name": "gamesPlayed", "value": 0}]},
        {"team": {"id": "2"}, "stats": []}]}}]}
    st = parse_standings(zero)
    assert st.goals_per_60 is None and st.ranks == {"1": 99, "2": 99}   # a missing seed is unranked


def test_ranked_tier_needs_both_sides_in_a_playoff_spot():
    ranks = {"h": 3, "a": 8, "x": 9, "y": 12}
    ch = make_channel("1", home_team="Tampa Bay Lightning", away_team="Boston Bruins")
    assert tier(nhl_game(home_id="h", away_id="a"), ch, LISTED, ranks, P) == TIER_RANKED
    assert tier(nhl_game(home_id="h", away_id="x"), ch, LISTED, ranks, P) == TIER_REST   # one side out: not enough
    assert tier(nhl_game(home_id="x", away_id="y"), ch, LISTED, ranks, P) == TIER_REST
    # NCAAF still means either side
    assert tier(nhl_game(home_id="h", away_id="x"), ch, LISTED, ranks, Params(top_rank=8)) == TIER_RANKED
    listed = make_channel("1", home_team="Washington Capitals", away_team="Boston Bruins")
    assert tier(nhl_game(home_id="x", away_id="y"), listed, LISTED, ranks, P) == TIER_LISTED


def test_both_ranked_bonus_needs_both_in_the_top_four():
    base = nhl_game(home_id="h", away_id="a", period=3, clock_s=600.0)
    assert leverage(base, {"h": 4, "a": 4}, P) == pytest.approx(leverage(base, {}, P) + P.both_ranked_bonus)
    assert leverage(base, {"h": 4, "a": 5}, P) == pytest.approx(leverage(base, {}, P))
    assert leverage(base, {"h": 1, "a": 9}, P) == pytest.approx(leverage(base, {}, P))


def test_upset_alert_uses_the_nhl_gap_and_period():
    def g(**kw):
        return nhl_game(home_id="h", away_id="a", period=3, clock_s=900.0, home_score=1, away_score=2, **kw)
    assert upset(g(), {"h": 2, "a": 9}, P)            # seed 2 trails a seed 9: 7 spots
    assert not upset(g(), {"h": 2, "a": 6}, P)        # only 4 spots
    assert upset(g(), {"h": 2}, P)                    # opponent missing from the standings = unranked
    assert not upset(nhl_game(home_id="h", away_id="a", period=2, home_score=1, away_score=2), {"h": 2, "a": 9}, P)
    assert not upset(g(), {"h": 9, "a": 12}, P)       # the favourite must itself be in a playoff spot


def test_ranked_game_outranks_an_unranked_one_with_the_same_leverage():
    docket = {"1": make_channel("1", home_team="A", away_team="B"), "2": make_channel("2", home_team="C", away_team="D")}
    games = [nhl_game("1", home_id="x", away_id="y"), nhl_game("2", home_id="h", away_id="a")]
    ranked = rank_candidates(games, docket, [], {"h": 2, "a": 7}, P)
    assert [s.event_id for s in ranked] == ["2", "1"] and ranked[0].tier == TIER_RANKED
    assert score(games[1], docket["2"], [], {"h": 2, "a": 7}, P).inputs["home_rank"] == 2
