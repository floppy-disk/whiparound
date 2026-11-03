"""The NHL's Poisson win probability (ESPN publishes none for hockey) and what hangs off it."""
from dataclasses import replace

import pytest

from whiparound.leverage import (TIER_REST, Params, closeness, decided, hockey_leader_wp, home_win_prob, leverage,
                                  score)
from whiparound.sport import NHL
from conftest import make_channel, make_game

P = NHL.params


def hockey(**kw):
    kw.setdefault("home_wp", None)
    kw.setdefault("period", 3)
    kw.setdefault("clock_s", 600.0)
    return replace(make_game(**kw), rules=NHL.rules)


def test_wp_known_values():
    assert hockey_leader_wp(0, 1800, 3.0) == 0.5
    assert hockey_leader_wp(1, 0, 3.0) == 1.0
    # one goal up, 20 minutes left, 3 goals/60: mu = 1. D ~ Skellam(1, 1): P(D<=0) + P(D=1)/2
    # = (1 + e^-2 I0(2))/2 + e^-2 I1(2)/2 = 0.5 + 0.1542 + 0.1077 (Bessel series, worked by hand) = 0.7619
    assert hockey_leader_wp(1, 1200, 3.0) == pytest.approx(0.762, abs=5e-4)
    assert 0.75 < hockey_leader_wp(1, 1200, 3.0) < 0.85
    assert hockey_leader_wp(3, 600, 3.0) >= 0.97
    assert hockey_leader_wp(3, 600, 3.0) == pytest.approx(0.995, abs=5e-4)


def test_wp_is_monotone_in_margin_and_in_time_left():
    by_margin = [hockey_leader_wp(m, 1800, 3.0) for m in range(0, 7)]
    assert by_margin == sorted(by_margin) and by_margin[0] == 0.5
    by_time = [hockey_leader_wp(1, t, 3.0) for t in (3600, 2400, 1200, 600, 300, 60, 0)]
    assert by_time == sorted(by_time) and by_time[-1] == 1.0
    assert all(0.5 <= w <= 1.0 for w in by_margin + by_time)


def test_wp_rises_with_a_lower_scoring_rate():
    assert hockey_leader_wp(1, 1200, 2.0) > hockey_leader_wp(1, 1200, 3.0) > hockey_leader_wp(1, 1200, 4.0)
    assert hockey_leader_wp(1, 1200, 0.0) == 1.0


def test_home_win_prob_sides_and_source():
    home_up = hockey(home_score=3, away_score=2, period=2, clock_s=600.0)
    wp, src = home_win_prob(home_up, P)
    assert src == "poisson" and wp == pytest.approx(hockey_leader_wp(1, 1800, 3.0))
    away_up = hockey(home_score=2, away_score=3, period=2, clock_s=600.0)
    wp2, _ = home_win_prob(away_up, P)
    assert wp2 == pytest.approx(1.0 - wp)
    assert home_win_prob(hockey(home_score=2, away_score=2), P) == (0.5, "poisson")
    # ESPN's figure wins when it is there; no model configured means nothing
    espn = hockey(home_score=3, away_score=2, home_wp=0.61)
    assert home_win_prob(espn, P) == (0.61, "espn")
    assert home_win_prob(hockey(home_score=3, away_score=2), Params()) == (None, None)
    assert home_win_prob(hockey(home_score=3, away_score=2, state="pre"), P) == (None, None)


def test_overtime_is_a_coin_flip_with_the_overtime_bonus():
    ot = hockey(period=4, clock_s=200.0, home_score=2, away_score=2)
    assert closeness(ot, P) == 1.0
    assert leverage(ot, {}, P) == pytest.approx(1.0 + P.overtime_bonus)
    so = hockey(period=5, clock_s=0.0, home_score=3, away_score=3)
    assert home_win_prob(so, P) == (0.5, "poisson")


def test_closeness_uses_the_model_and_leverage_ranks_by_it():
    tied_late = hockey(period=3, clock_s=300.0, home_score=2, away_score=2)
    one_up = hockey(period=3, clock_s=300.0, home_score=3, away_score=2)
    three_up = hockey(period=3, clock_s=300.0, home_score=5, away_score=2)
    assert closeness(tied_late, P) == 1.0
    assert 0.0 < closeness(one_up, P) < closeness(tied_late, P)
    assert closeness(three_up, P) < closeness(one_up, P)
    assert leverage(tied_late, {}, P) > leverage(one_up, {}, P) > leverage(three_up, {}, P)


def test_decided_by_the_model_and_by_the_margin_floor():
    assert decided(hockey(period=3, clock_s=300.0, home_score=5, away_score=1), P)          # 4 goals up: the floor
    assert decided(hockey(period=3, clock_s=500.0, home_score=4, away_score=1), P)          # 3 up, under 10 min
    assert not decided(hockey(period=2, clock_s=600.0, home_score=3, away_score=2), P)
    assert not decided(hockey(period=3, clock_s=600.0, home_score=3, away_score=2), P)


def test_score_logs_the_home_wp_and_where_it_came_from():
    ch = make_channel("1")
    s = score(hockey(home_score=1, away_score=2, period=2, clock_s=900.0), ch, [], {}, P)
    assert s.inputs["wp_source"] == "poisson" and 0.0 < s.inputs["home_wp"] < 0.5   # home trails
    e = score(make_game("1", home_wp=0.6), ch, [], {}, Params())
    assert e.inputs["wp_source"] == "espn" and e.inputs["home_wp"] == 0.6
    n = score(make_game("1", home_wp=None), ch, [], {}, Params())
    assert n.inputs["wp_source"] is None and n.inputs["home_wp"] is None
    assert s.tier == TIER_REST   # no listed team, no standings: the rest


def test_goals_per_60_is_a_param_the_runner_can_replace():
    g = hockey(home_score=3, away_score=2, period=3, clock_s=1200.0)
    hot = replace(P, goals_per_60=4.0)
    assert closeness(g, hot) > closeness(g, P)
