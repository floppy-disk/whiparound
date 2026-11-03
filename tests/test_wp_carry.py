from datetime import timedelta

import pytest

from whiparound.espn import WpMemory, carry_wp
from whiparound.leverage import Params, rank_candidates
from conftest import T0, make_channel, make_game

P = Params()


def mem(wp=0.8, seen=T0, home_score=21, away_score=21, period=4):
    return WpMemory(wp, seen, home_score, away_score, period)


def test_missing_wp_within_window_is_filled_from_memory():
    memory = {"1": mem()}
    out = carry_wp([make_game("1", home_wp=None)], memory, T0 + timedelta(seconds=20))
    assert out[0].home_wp == 0.8
    assert out[0].wp_carried is True
    assert memory["1"] == mem()  # a carried value does not refresh its own age


def test_missing_wp_older_than_cap_is_left_none():
    memory = {"1": mem()}
    out = carry_wp([make_game("1", home_wp=None)], memory, T0 + timedelta(seconds=901))
    assert out[0].home_wp is None
    assert out[0].wp_carried is False
    assert "1" not in memory


def test_missing_wp_with_same_score_and_period_is_carried_five_minutes_on():
    memory = {"1": mem()}
    out = carry_wp([make_game("1", home_wp=None)], memory, T0 + timedelta(minutes=5))
    assert out[0].home_wp == 0.8
    assert out[0].wp_carried is True
    assert memory["1"] == mem()


def test_missing_wp_after_home_score_change_is_not_carried_and_entry_dropped():
    memory = {"1": mem()}
    out = carry_wp([make_game("1", home_wp=None, home_score=28)], memory, T0 + timedelta(seconds=20))
    assert out[0].home_wp is None
    assert out[0].wp_carried is False
    assert "1" not in memory


def test_missing_wp_after_away_score_change_is_not_carried():
    memory = {"1": mem()}
    out = carry_wp([make_game("1", home_wp=None, away_score=28)], memory, T0 + timedelta(seconds=20))
    assert out[0].home_wp is None and out[0].wp_carried is False
    assert "1" not in memory


def test_missing_wp_after_period_change_is_not_carried():
    memory = {"1": mem(period=3)}
    out = carry_wp([make_game("1", home_wp=None, period=4)], memory, T0 + timedelta(seconds=20))
    assert out[0].home_wp is None and out[0].wp_carried is False
    assert "1" not in memory


def test_missing_wp_exactly_at_cap_is_still_carried():
    out = carry_wp([make_game("1", home_wp=None)], {"1": mem()}, T0 + timedelta(seconds=900))
    assert out[0].home_wp == 0.8 and out[0].wp_carried is True


def test_finished_game_drops_memory_and_does_not_carry():
    memory = {"1": mem()}
    out = carry_wp([make_game("1", state="post", home_wp=None)], memory, T0 + timedelta(seconds=20))
    assert out[0].home_wp is None
    assert out[0].wp_carried is False
    assert "1" not in memory


def test_returns_new_objects_and_leaves_inputs_unchanged():
    missing, present = make_game("1", home_wp=None), make_game("2", home_wp=0.6)
    games = [missing, present]
    out = carry_wp(games, {"1": mem()}, T0)
    assert out is not games
    assert out[0] is not missing
    assert [g.event_id for g in out] == ["1", "2"]
    assert missing.home_wp is None and missing.wp_carried is False
    assert games == [missing, present]


def test_present_wp_is_not_flagged_and_updates_memory():
    memory = {"1": mem()}
    now = T0 + timedelta(seconds=20)
    out = carry_wp([make_game("1", home_wp=0.55)], memory, now)
    assert out[0].home_wp == 0.55
    assert out[0].wp_carried is False
    assert memory["1"] == mem(0.55, now)


def test_nothing_remembered_means_nothing_carried():
    out = carry_wp([make_game("1", home_wp=None)], {}, T0)
    assert out[0].home_wp is None and out[0].wp_carried is False


def test_carried_wp_scores_like_the_real_one():
    """The 2026-10-17 spike: a 7-7 Q2 game with no wp fell back to margin closeness and jumped to 1.0."""
    kw = dict(period=2, clock_s=600.0, home_score=7, away_score=7)
    carried = carry_wp([make_game("1", home_wp=None, **kw)], {"1": mem(0.90, T0, 7, 7, 2)}, T0 + timedelta(seconds=20))
    docket = {"1": make_channel("1")}

    def scored(g):
        return rank_candidates([g], docket, [], {}, P)[0]

    uncarried = scored(make_game("1", home_wp=None, **kw))
    assert uncarried.closeness == pytest.approx(1.0)
    real, got = scored(make_game("1", home_wp=0.90, **kw)), scored(carried[0])
    assert got.closeness == real.closeness == pytest.approx(0.2)
    assert got.leverage == real.leverage
    assert got.inputs["wp_carried"] is True and real.inputs["wp_carried"] is False
