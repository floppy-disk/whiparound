"""The sport profile: selection, the Eastern date range, and the period arithmetic the NHL needs from it."""
from dataclasses import replace
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from whiparound.espn import parse_scoreboard
from whiparound.leverage import progress, seconds_remaining
from whiparound.sport import NCAAF, NHL, get_sport, scoreboard_dates
from conftest import make_game

ET = ZoneInfo("America/New_York")


def hockey(**kw):
    """A live NHL game: 20-minute periods, three of them, ESPN status 22 = intermission."""
    kw.setdefault("period", 2)
    kw.setdefault("clock_s", 600.0)
    kw.setdefault("home_wp", None)
    return replace(make_game(**kw), rules=NHL.rules)


def test_get_sport_default_and_names():
    assert get_sport(None) is NCAAF and get_sport("") is NCAAF and get_sport("ncaaf") is NCAAF
    assert get_sport(" NHL ") is NHL
    with pytest.raises(ValueError):
        get_sport("nfl")


def test_profiles_hold_the_brief_values():
    assert (NCAAF.period_s, NCAAF.regulation_periods, NCAAF.break_status_id, NCAAF.period_label) == (900.0, 4, "23", "Q")
    assert (NHL.period_s, NHL.regulation_periods, NHL.break_status_id, NHL.period_label) == (1200.0, 3, "22", "P")
    assert NHL.scoreboard_params == {} and NCAAF.scoreboard_params == {"groups": "80"}
    assert NHL.rankings_path is None and NHL.standings_url.startswith("https://site.web.api.espn.com/")
    assert NCAAF.standings_url is None and NCAAF.rankings_path.endswith("/college-football/rankings")
    assert (NHL.teamarr_league, NHL.log_name, NHL.logo_file) == ("nhl", "nhl-changer.jsonl", "assets/logo-nhl.png")
    assert (NCAAF.teamarr_league, NCAAF.log_name, NCAAF.logo_file) == ("college-football", "cfb-changer.jsonl",
                                                                         "assets/logo.png")
    assert NHL.params.hop_after_by_signal == {"score": 45.0, "halftime": 0.0}
    assert NHL.hop_break_types[0] == "Stoppage"
    assert NHL.params.both_ranked_rank == 4 and NCAAF.params.both_ranked_rank == 25
    assert NHL.ranked_needs_both and not NCAAF.ranked_needs_both


def test_scoreboard_dates_today_only_for_ncaaf_and_carry_before_4am_et_for_nhl():
    evening = datetime(2026, 11, 3, 3, 30, tzinfo=timezone.utc)   # 22:30 on Nov 2 in New York
    assert scoreboard_dates(NCAAF, evening, ET) == ["20261102"]
    assert scoreboard_dates(NHL, evening, ET) == ["20261102"]
    small_hours = datetime(2026, 11, 3, 6, 30, tzinfo=timezone.utc)   # 01:30 on Nov 3: last night's late games still on
    assert scoreboard_dates(NHL, small_hours, ET) == ["20261102", "20261103"]
    assert scoreboard_dates(NCAAF, small_hours, ET) == ["20261103"]
    new_year = datetime(2027, 1, 1, 6, 0, tzinfo=timezone.utc)      # 01:00 on Jan 1
    assert scoreboard_dates(NHL, new_year, ET) == ["20261231", "20270101"]


def test_parse_scoreboard_uses_the_rules_passed_in():
    data = {"events": [{"id": "1", "status": {"period": 1, "clock": 0.0, "type": {"id": "22", "state": "in"}},
                        "competitions": [{"competitors": [{"homeAway": "home", "score": "1", "team": {"id": "1"}},
                                                          {"homeAway": "away", "score": "0", "team": {"id": "2"}}]}]}]}
    assert not parse_scoreboard(data)[0].halftime            # NCAAF rules: 22 is not the halftime id
    g = parse_scoreboard(data, NHL.rules)[0]
    assert g.halftime and not g.overtime


def test_ncaaf_period_arithmetic_is_unchanged():
    g = make_game(period=4, clock_s=120.0)
    assert seconds_remaining(g) == 120.0 and progress(g) == pytest.approx((3 + 780 / 900) / 4)
    assert seconds_remaining(make_game(period=2, status_type_id="23", clock_s=0.0)) == 1800.0
    assert progress(make_game(period=2, status_type_id="23", clock_s=0.0)) == 0.5
    assert seconds_remaining(make_game(period=5)) == 0.0 and progress(make_game(period=5)) == 1.0


def test_nhl_seconds_remaining_and_progress():
    assert seconds_remaining(hockey(period=1, clock_s=1200.0)) == 3600.0
    assert seconds_remaining(hockey(period=2, clock_s=600.0)) == 1800.0
    assert seconds_remaining(hockey(period=3, clock_s=45.0)) == 45.0
    assert progress(hockey(period=2, clock_s=600.0)) == pytest.approx(0.5)
    assert progress(hockey(period=3, clock_s=0.0)) == pytest.approx(1.0)


def test_nhl_intermission_is_the_end_of_the_period_just_played():
    first = hockey(period=1, clock_s=0.0, status_type_id="22")
    second = hockey(period=2, clock_s=0.0, status_type_id="22")
    assert first.halftime and second.halftime
    assert seconds_remaining(first) == 2400.0 and seconds_remaining(second) == 1200.0
    assert progress(first) == pytest.approx(1 / 3) and progress(second) == pytest.approx(2 / 3)
    # a stale clock at the intermission does not leak into the numbers
    assert seconds_remaining(hockey(period=1, clock_s=1200.0, status_type_id="22")) == 2400.0


def test_nhl_overtime_is_period_four_and_up():
    ot = hockey(period=4, clock_s=300.0)
    assert ot.overtime and seconds_remaining(ot) == 0.0 and progress(ot) == 1.0
    assert not hockey(period=3).overtime
