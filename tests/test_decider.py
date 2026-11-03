from datetime import timedelta

from whiparound.decider import Decider
from whiparound.leverage import Params, PlayState, score
from conftest import make_channel, make_game

P = Params()
LISTED = ["Notre Dame Fighting Irish"]


def scored(event_id, **kw):
    home_team = kw.pop("home_team", "Home")
    away_team = kw.pop("away_team", "Away")
    g = make_game(event_id, **kw)
    return score(g, make_channel(event_id, home_team=home_team, away_team=away_team), LISTED, {}, P)


def test_initial_pick_and_still_best(t0):
    d = Decider(P)
    a = scored("1", home_wp=0.5)
    b = scored("2", home_wp=0.8)
    dec = d.step(t0, [a, b])
    assert dec.switched and dec.reason == "initial" and dec.pick.event_id == "1"
    dec = d.step(t0 + timedelta(seconds=60), [a, b])
    assert not dec.switched and dec.reason == "still_best"


def test_dwell_holds_then_hysteresis_decides(t0):
    d = Decider(P)
    cur = scored("1", home_wp=0.5)
    d.step(t0, [cur])
    # challenger slightly better, same tier: held by dwell, then by hysteresis (gap 0.05 < 0.15)
    chal = scored("2", home_wp=0.5, period=4, clock_s=10.0)
    cur2 = scored("1", home_wp=0.5, period=4, clock_s=800.0)   # lower progress -> lower leverage
    assert chal.tier == cur2.tier and 0 < chal.leverage - cur2.leverage < 0.15
    dec = d.step(t0 + timedelta(seconds=300), [chal, cur2])
    assert not dec.switched and dec.reason == "dwell"
    dec = d.step(t0 + timedelta(seconds=601), [chal, cur2])
    assert not dec.switched and dec.reason == "hysteresis"
    # a big enough gap switches
    far = scored("3", home_wp=0.5, period=4, clock_s=10.0)
    weak = scored("1", home_wp=0.72, period=3, clock_s=800.0)
    assert far.leverage - weak.leverage >= 0.15
    dec = d.step(t0 + timedelta(seconds=602), [far, weak])
    assert dec.switched and dec.reason == "leverage" and dec.pick.event_id == "3" and dec.previous == "1"


def test_better_tier_switches_after_dwell_only(t0):
    d = Decider(P)
    other = scored("1", home_wp=0.5, period=3, clock_s=600.0)
    d.step(t0, [other])
    nd = scored("2", home_wp=0.9, period=1, clock_s=800.0, away_team="Notre Dame Fighting Irish")
    assert nd.tier < other.tier and nd.leverage < other.leverage
    dec = d.step(t0 + timedelta(seconds=100), [nd, other])
    assert not dec.switched and dec.reason == "dwell"
    dec = d.step(t0 + timedelta(seconds=700), [nd, other])
    assert dec.switched and dec.reason == "better_tier" and dec.pick.event_id == "2"


def test_halftime_and_final_are_free_switches(t0):
    d = Decider(P)
    d.step(t0, [scored("1", home_wp=0.5)])
    half = scored("1", status_type_id="23", period=2, clock_s=0.0, home_wp=None, home_score=10, away_score=13)
    other = scored("2", home_wp=0.6, period=2)
    dec = d.step(t0 + timedelta(seconds=30), [other, half])
    assert dec.switched and dec.reason == "halftime" and dec.pick.event_id == "2"
    d2 = Decider(P)
    d2.step(t0, [scored("1", home_wp=0.5)])
    # ESPN still lists it live in `state` for our ranking only when live; emulate a post game that is still ranked
    final = scored("1", home_wp=None, home_score=31, away_score=24)
    final = final.__class__(**{**final.__dict__, "game": final.game.__class__(**{**final.game.__dict__, "state": "post"})})
    dec = d2.step(t0 + timedelta(seconds=30), [scored("3", home_wp=0.6), final])
    assert dec.switched and dec.reason == "final"


def test_current_gone_takes_top(t0):
    d = Decider(P)
    d.step(t0, [scored("1", home_wp=0.5)])
    dec = d.step(t0 + timedelta(seconds=30), [scored("2", home_wp=0.6)])
    assert dec.switched and dec.reason == "current_gone" and dec.pick.event_id == "2"
    dec = d.step(t0 + timedelta(seconds=60), [])
    assert dec.switched and dec.pick is None and dec.reason == "nothing_live"


def test_lock_in_holds_a_close_finish(t0):
    d = Decider(P)
    d.step(t0, [scored("1", home_wp=0.5)])
    cur = scored("1", period=4, clock_s=90.0, home_score=21, away_score=17, home_wp=0.55)
    better = scored("2", period=5, clock_s=0.0, home_wp=0.5)  # overtime, clearly higher leverage
    assert better.leverage - cur.leverage >= 0.15
    dec = d.step(t0 + timedelta(seconds=900), [better, cur])
    assert not dec.switched and dec.reason == "lock_in"
    # a two-score margin releases the lock
    cur_wide = scored("1", period=4, clock_s=90.0, home_score=21, away_score=10, home_wp=0.9)
    dec = d.step(t0 + timedelta(seconds=901), [better, cur_wide])
    assert dec.switched and dec.reason == "leverage"


def test_pin_overrides_until_expiry_or_absence(t0):
    d = Decider(P)
    a, b = scored("1", home_wp=0.5, period=3, clock_s=600.0), scored("2", home_wp=0.95, period=3, clock_s=600.0)
    d.step(t0, [a, b])
    d.pin("2", t0 + timedelta(minutes=30))
    dec = d.step(t0 + timedelta(seconds=60), [a, b])
    assert dec.switched and dec.reason == "pinned" and dec.pick.event_id == "2"
    dec = d.step(t0 + timedelta(minutes=31), [a, b])
    assert dec.reason in ("dwell", "hysteresis", "leverage", "better_tier") and d.pinned is None
    d.pin("9", t0 + timedelta(hours=1))
    dec = d.step(t0 + timedelta(minutes=32), [a, b])
    assert dec.reason != "pinned"  # a pin on a game that is not live is ignored


# -- break hop ------------------------------------------------------------------------------------------------------
PLAY, BREAK, IDLE = PlayState(True, False), PlayState(False, True), PlayState(False, False)


def _two(**kw):
    """Games 1 (better) and 2, the same tier (unranked, not listed)."""
    a = scored("1", home_wp=0.5, period=4, clock_s=600.0, **kw)
    b = scored("2", home_wp=0.6, period=3, clock_s=600.0, **kw)
    assert a.tier == b.tier and a.leverage > b.leverage
    return a, b


def test_hop_to_same_tier_in_play_game(t0):
    d = Decider(P)
    a, b = _two()
    d.step(t0, [a, b])
    assert d.current == "1"
    dec = d.step(t0 + timedelta(seconds=60), [a, b], {"1": BREAK, "2": PLAY})
    assert dec.switched and dec.hopped and dec.reason == "hop" and dec.pick.event_id == "2" and dec.previous == "1"
    assert d.hopped_from == "1"


def test_no_hop_to_a_lower_tier_game(t0):
    d = Decider(P)
    nd = scored("1", home_wp=0.5, period=4, clock_s=600.0, away_team="Notre Dame Fighting Irish")
    low = scored("2", home_wp=0.5, period=4, clock_s=600.0)
    assert low.tier > nd.tier
    d.step(t0, [nd, low])
    dec = d.step(t0 + timedelta(seconds=60), [nd, low], {"1": BREAK, "2": PLAY})
    assert not dec.switched and not dec.hopped and dec.reason == "still_best" and d.hopped_from is None
    # off the top pick, the normal dwell path decides instead
    d2 = Decider(P)
    d2.step(t0, [low])
    dec = d2.step(t0 + timedelta(seconds=60), [nd, low], {"2": BREAK, "1": IDLE})
    assert dec.reason == "dwell" and not dec.hopped


def test_hop_rerank_returns_to_the_top_game_without_dwell(t0):
    d = Decider(P)
    a, b = _two()
    d.step(t0, [a, b])
    d.step(t0 + timedelta(seconds=60), [a, b], {"1": BREAK, "2": PLAY})
    dec = d.step(t0 + timedelta(seconds=70), [a, b], {"1": IDLE, "2": PLAY})  # inside the minimum stay: hold
    assert not dec.switched and dec.reason == "hop_stay" and d.current == "2"
    dec = d.step(t0 + timedelta(seconds=155), [a, b], {"1": IDLE, "2": PLAY})  # top still between plays: hold, no dwell/switch
    assert not dec.switched and dec.reason == "hop_hold" and d.current == "2"
    dec = d.step(t0 + timedelta(seconds=160), [a, b], {"1": PLAY, "2": PLAY})
    assert dec.switched and dec.hopped and dec.reason == "hop_rerank" and dec.pick.event_id == "1"
    assert d.hopped_from is None
    assert d.step(t0 + timedelta(seconds=220), [a, b], {"1": PLAY, "2": PLAY}).reason == "still_best"


def test_hop_interval_limits_a_second_hop(t0):
    d = Decider(P)
    a, b = _two()
    d.step(t0, [a, b])
    d.step(t0 + timedelta(seconds=60), [a, b], {"1": BREAK, "2": PLAY})            # hop 1 -> 2
    d.step(t0 + timedelta(seconds=160), [a, b], {"1": PLAY, "2": PLAY})            # rerank back to 1 (after the stay)
    assert d.current == "1"
    dec = d.step(t0 + timedelta(seconds=200), [a, b], {"1": BREAK, "2": PLAY})     # 140 s after the first hop
    assert not dec.hopped and dec.reason == "still_best" and d.current == "1"
    dec = d.step(t0 + timedelta(seconds=250), [a, b], {"1": BREAK, "2": PLAY})     # 190 s after it
    assert dec.hopped and dec.reason == "hop"


def test_destination_in_break_hops_again_when_allowed(t0):
    d = Decider(P)
    a, b = _two()
    c = scored("3", home_wp=0.7, period=3, clock_s=600.0)
    d.step(t0, [a, b, c])
    d.step(t0 + timedelta(seconds=60), [a, b, c], {"1": BREAK, "2": PLAY, "3": PLAY})
    dec = d.step(t0 + timedelta(seconds=300), [a, b, c], {"1": IDLE, "2": BREAK, "3": PLAY})
    assert dec.hopped and dec.reason == "hop" and dec.pick.event_id == "3" and d.hopped_from == "2"


def test_normal_switch_clears_hopped_from(t0):
    d = Decider(P)
    a, b = _two()
    d.step(t0, [a, b])
    d.step(t0 + timedelta(seconds=60), [a, b], {"1": BREAK, "2": PLAY})
    assert d.hopped_from == "1"
    dec = d.step(t0 + timedelta(seconds=90), [b])   # the top game is gone: current_gone is a normal take of the new top
    assert dec.reason == "still_best"
    d2 = Decider(P)
    d2.step(t0, [a, b])
    d2.step(t0 + timedelta(seconds=60), [a, b], {"1": BREAK, "2": PLAY})
    dec = d2.step(t0 + timedelta(seconds=90), [a])
    assert dec.reason == "current_gone" and d2.hopped_from is None


def test_play_state_none_is_unchanged(t0):
    d = Decider(P)
    a, b = _two()
    d.step(t0, [a, b])
    dec = d.step(t0 + timedelta(seconds=60), [a, b])
    assert dec.reason == "still_best" and not dec.hopped and d.hopped_from is None and d.last_hop_at is None


def test_on_a_hop_destination_a_better_in_play_game_below_the_top_is_taken(t0):
    """2026-10-17 21:34Z: on Ole Miss (hop), Alabama-Tennessee (top) at halftime, Auburn-Georgia (ranked above Ole Miss)
    in play: move to Auburn-Georgia and stay in hop mode (not the top pick); then back to the top when it plays."""
    d = Decider(P)
    # all tier "other" here (no listed, no ranks); leverage via win probability: 0.5 tied > 0.6 > 0.9
    top = scored("71", home_wp=0.5, period=2); mid = scored("72", home_wp=0.6, period=2); dest = scored("73", home_wp=0.9, period=2)  # Q2: no lock-in
    ranked = [top, mid, dest]
    d.step(t0, ranked, {"71": PlayState(True, False), "72": PlayState(False, False), "73": PlayState(True, False)})
    r = d.step(t0 + timedelta(seconds=20), ranked, {"71": PlayState(False, True), "72": PlayState(False, False), "73": PlayState(True, False)})
    assert r.reason == "hop" and r.pick.event_id == "73"
    r = d.step(t0 + timedelta(seconds=60), ranked, {"71": PlayState(False, True), "72": PlayState(True, False), "73": PlayState(True, False)})
    assert r.reason == "hop_stay" and r.pick.event_id == "73"  # inside the minimum stay
    r = d.step(t0 + timedelta(seconds=115), ranked, {"71": PlayState(False, True), "72": PlayState(True, False), "73": PlayState(True, False)})
    assert r.reason == "hop_rerank" and r.pick.event_id == "72" and d.hopped_from is not None
    r = d.step(t0 + timedelta(seconds=135), ranked, {"71": PlayState(True, False), "72": PlayState(True, False), "73": PlayState(True, False)})
    assert r.reason == "hop_rerank" and r.pick.event_id == "71" and d.hopped_from is None


def test_hop_destination_is_kept_for_the_minimum_stay(t0):
    """2026-10-17 21:43Z: hopped on a score and bounced back 20 s later on the extra point. Stay at least hop_min_stay_s."""
    d = Decider(P)
    top = scored("81", home_wp=0.5, period=2); dest = scored("82", home_wp=0.9, period=2)
    ranked = [top, dest]
    d.step(t0, ranked, {"81": PlayState(True, False), "82": PlayState(True, False)})
    r = d.step(t0 + timedelta(seconds=20), ranked, {"81": PlayState(False, True), "82": PlayState(True, False)})
    assert r.reason == "hop"
    r = d.step(t0 + timedelta(seconds=40), ranked, {"81": PlayState(True, False), "82": PlayState(True, False)})
    assert r.reason == "hop_stay" and r.pick.event_id == "82"
    r = d.step(t0 + timedelta(seconds=20 + 95), ranked, {"81": PlayState(True, False), "82": PlayState(True, False)})
    assert r.reason == "hop_rerank" and r.pick.event_id == "81"
