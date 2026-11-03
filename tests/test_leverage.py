import pytest
from dataclasses import replace
from datetime import datetime, timezone

from whiparound.espn import Game

from whiparound.leverage import (TIER_LISTED, TIER_RANKED, TIER_REST, Params, closeness, decided, leverage, progress,
                                  rank_candidates, tier, upset)
from conftest import make_channel, make_game

P = Params()
LISTED = ["Notre Dame Fighting Irish", "Pittsburgh Panthers", "James Madison Dukes"]


def test_tied_late_fourth_is_near_one():
    g = make_game(period=4, clock_s=120.0, home_wp=0.5)
    assert closeness(g, P) == 1.0
    assert progress(g) == pytest.approx((3 + 780 / 900) / 4)  # 0.9667
    assert leverage(g, {}, P) == pytest.approx(0.4 + 0.6 * 0.96667, abs=1e-3)  # ≈ 0.98


def test_tied_first_quarter_is_low():
    g = make_game(period=1, clock_s=900.0, home_wp=0.5)
    assert progress(g) == 0.0
    assert leverage(g, {}, P) == pytest.approx(0.4)


def test_blowout_is_decided_and_near_zero():
    g = make_game(period=3, clock_s=600.0, home_score=45, away_score=10, home_wp=0.995)
    assert decided(g, P)
    assert leverage(g, {}, P) == pytest.approx(0.0, abs=0.02)


def test_decided_by_margin_late_without_wp():
    g = make_game(period=4, clock_s=500.0, home_score=27, away_score=10, home_wp=None)
    assert decided(g, P)
    g2 = make_game(period=4, clock_s=700.0, home_score=27, away_score=10, home_wp=None)
    assert not decided(g2, P)  # 17 points with 700 s left is not yet "late"
    g3 = make_game(period=2, clock_s=100.0, home_score=28, away_score=3, home_wp=None)
    assert decided(g3, P)  # 25 at any time


def test_fallback_closeness_without_wp():
    g = make_game(period=1, clock_s=900.0, home_score=7, away_score=0, home_wp=None)
    assert closeness(g, P) == pytest.approx(1 - 7 / 8)
    g2 = make_game(period=4, clock_s=0.0, home_score=7, away_score=0, home_wp=None)
    assert closeness(g2, P) == pytest.approx(1 - 7 / 32)


def test_both_ranked_bonus_and_overtime():
    base = make_game(period=4, clock_s=0.0, home_wp=0.5)
    ranked = make_game(period=4, clock_s=0.0, home_wp=0.5, home_rank=4, away_rank=5)
    assert leverage(ranked, {}, P) == pytest.approx(leverage(base, {}, P) + 0.15)
    one_ranked = make_game(period=4, clock_s=0.0, home_wp=0.5, home_rank=4)
    assert leverage(one_ranked, {}, P) == pytest.approx(leverage(base, {}, P))
    ot = make_game(period=5, clock_s=0.0, home_wp=0.5)
    assert leverage(ot, {}, P) == pytest.approx(1.0 + 0.25)
    # the weekly AP poll outranks the scoreboard's curatedRank
    assert leverage(base, {"h": 3, "a": 9}, P) == pytest.approx(leverage(base, {}, P) + 0.15)


def test_tiers():
    pitt = make_channel("1", home_team="Syracuse Orange", away_team="Pittsburgh Panthers")
    other = make_channel("2", home_team="Rice Owls", away_team="Tulsa Golden Hurricane")
    close = make_game("1", home_wp=0.5)
    assert tier(close, pitt, LISTED, {}, P) == TIER_LISTED
    assert tier(make_game("2", home_wp=0.5), other, LISTED, {}, P) == TIER_REST
    assert tier(make_game("2", home_wp=0.5, away_rank=12), other, LISTED, {}, P) == TIER_RANKED
    # a decided listed game drops all the way down
    assert tier(make_game("1", home_wp=0.99), pitt, LISTED, {}, P) == TIER_REST
    assert tier(make_game("2", home_wp=0.99, away_rank=12), other, LISTED, {}, P) == TIER_REST


def test_rank_candidates_order_and_filters():
    docket = {
        "1": make_channel("1", home_team="Syracuse Orange", away_team="Pittsburgh Panthers"),
        "2": make_channel("2", home_team="USC Trojans", away_team="Notre Dame Fighting Irish"),
        "3": make_channel("3", home_team="Rice Owls", away_team="Tulsa Golden Hurricane"),
        "4": make_channel("4", home_team="Georgia Bulldogs", away_team="Alabama Crimson Tide"),
    }
    games = [
        make_game("1", home_wp=0.5, period=4, clock_s=60.0),        # Pitt, listed #1, leverage ~0.99
        make_game("2", home_wp=0.5, period=4, clock_s=60.0),        # ND, listed #0, same leverage -> wins by order
        make_game("3", home_wp=0.5, period=4, clock_s=60.0),        # other, tier 2
        make_game("4", home_wp=0.5, period=4, clock_s=60.0, home_rank=1, away_rank=2),  # ranked, tier 1
        make_game("5", home_wp=0.5),                                # no channel -> dropped
        make_game("6", state="pre", home_wp=None),                  # not live -> dropped
    ]
    docket["5"] = make_channel("5")
    ranked = rank_candidates(games, docket, LISTED, {}, P)
    assert [s.event_id for s in ranked] == ["2", "1", "4", "3", "5"][:4] + ["5"]
    assert ranked[0].tier == TIER_LISTED and ranked[2].tier == TIER_RANKED and ranked[3].tier == TIER_REST
    assert ranked[0].inputs["home_wp"] == 0.5 and "seconds_left" in ranked[0].inputs


def test_late_close_shield_beats_a_fresh_listed_kickoff():
    """2026-10-17 19:37Z: Ole Miss kicked off (listed, progress 0) and beat OSU-IU, Q4 and 4 points. The shield reverses that."""
    from whiparound.leverage import Params, rank_candidates
    from whiparound.docket import Channel
    p = Params()
    q4 = make_game("osu", period=4, clock_s=323.0, home_score=15, away_score=19, home_wp=0.18, home_rank=5, away_rank=4)
    kick = make_game("miss", period=1, clock_s=900.0, home_score=0, away_score=0, home_wp=0.60, home_rank=9, away_rank=13)
    docket = {"osu": Channel("osu", 1, "t1", "OSU @ IU", "Indiana Hoosiers", "Ohio State Buckeyes"),
              "miss": Channel("miss", 2, "t2", "MIZ @ MISS", "Ole Miss Rebels", "Missouri Tigers")}
    ranked = rank_candidates([kick, q4], docket, ["Ole Miss Rebels"], {}, p)
    assert [s.event_id for s in ranked] == ["osu", "miss"]
    assert ranked[0].shielded and ranked[0].tier == 1 and not ranked[1].shielded and ranked[1].tier == 0
    # once the late game is no longer within one score, the listed game is back on top
    blowout = make_game("osu", period=4, clock_s=323.0, home_score=15, away_score=35, home_wp=0.02, home_rank=5, away_rank=4)
    ranked = rank_candidates([kick, blowout], docket, ["Ole Miss Rebels"], {}, p)
    assert [s.event_id for s in ranked] == ["miss", "osu"]


def test_unranked_late_close_game_is_not_shielded():
    """Rule: an unranked 4th-quarter game that is close must not displace a ranked game."""
    from whiparound.leverage import Params, rank_candidates
    from whiparound.docket import Channel
    p = Params()
    unranked_q4 = make_game("cin", period=4, clock_s=186.0, home_score=42, away_score=42, home_wp=0.54)
    ranked_q1 = make_game("miss", period=1, clock_s=800.0, home_score=7, away_score=0, home_wp=0.70, home_rank=9, away_rank=13)
    docket = {"cin": Channel("cin", 1, "t1", "CIN @ WVU", "West Virginia Mountaineers", "Cincinnati Bearcats"),
              "miss": Channel("miss", 2, "t2", "MIZ @ MISS", "Ole Miss Rebels", "Missouri Tigers")}
    ranked = rank_candidates([unranked_q4, ranked_q1], docket, [], {}, p)
    assert [s.event_id for s in ranked] == ["miss", "cin"]
    assert not ranked[1].shielded and ranked[1].tier == 2


def _upset_game(**kw):
    base = dict(period=4, clock_s=300.0, home_score=10, away_score=17, home_wp=0.4, home_rank=5, away_rank=99)
    base.update(kw)
    return make_game(**base)


def test_upset_ranked_favourite_trailing_unranked_in_q4():
    assert upset(_upset_game(), {}, P)


def test_upset_false_in_second_quarter():
    assert not upset(_upset_game(period=2), {}, P)


def test_upset_false_at_halftime():
    assert not upset(_upset_game(period=2, status_type_id="23"), {}, P)
    assert not upset(_upset_game(period=3, status_type_id="23"), {}, P)


def test_upset_false_when_favourite_leads_or_tied():
    assert not upset(_upset_game(home_score=17, away_score=10), {}, P)
    assert not upset(_upset_game(home_score=14, away_score=14), {}, P)


def test_upset_false_when_both_unranked():
    assert not upset(_upset_game(home_rank=99), {}, P)


def test_upset_false_when_rank_gap_too_small():
    assert not upset(_upset_game(home_rank=5, away_rank=9), {}, P)
    assert not upset(_upset_game(home_rank=5, away_rank=14), {}, P)  # gap 9
    assert upset(_upset_game(home_rank=5, away_rank=15), {}, P)      # gap 10


def test_upset_true_for_3_vs_20_trailing_either_side():
    assert upset(_upset_game(home_rank=3, away_rank=20), {}, P)
    assert upset(_upset_game(home_rank=20, away_rank=3, home_score=17, away_score=10), {}, P)


def test_upset_uses_ap_poll_ranks_over_scoreboard():
    assert upset(_upset_game(home_rank=99), {"h": 4}, P)


def test_upset_false_when_decided_or_not_live():
    assert not upset(_upset_game(home_score=0, away_score=28, home_wp=0.01), {}, P)
    assert not upset(_upset_game(state="post"), {}, P)


def test_upset_bonus_in_leverage():
    g = _upset_game()
    plain = _upset_game(home_rank=99)
    assert leverage(g, {}, P) == pytest.approx(leverage(plain, {}, P) + P.upset_bonus, abs=1e-4)


def test_decided_min_margin_keeps_one_goal_games_live():
    """NHL: a one-goal lead with seconds left is wp ~0.98 by the model, but a pulled goalie keeps it live (coordinator, 2026-11-02)."""
    from whiparound.sport import NHL
    from whiparound.espn import Side
    p = NHL.params
    g = Game(event_id="x", name="", short_name="", state="in", status_type_id="2", period=3, clock_s=30.0,
             home=Side("1", "A", 2, 99), away=Side("2", "B", 1, 99), home_wp=None, seconds_left=None,
             last_play_type=None, possession=None, date=datetime(2026, 11, 2, tzinfo=timezone.utc), rules=NHL.rules)
    assert not decided(g, p)
    g3 = replace(g, home=Side("1", "A", 4, 99))
    assert decided(g3, p)
