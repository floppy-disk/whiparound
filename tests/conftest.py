from datetime import datetime, timezone

import pytest

from whiparound.docket import Channel
from whiparound.espn import Game, Side

T0 = datetime(2026, 10, 17, 20, 0, tzinfo=timezone.utc)


def make_game(event_id="1", *, state="in", status_type_id="2", period=4, clock_s=120.0, home_score=21,
              away_score=21, home_wp=0.5, seconds_left=None, home_rank=99, away_rank=99, home_id="h", away_id="a",
              name=None, last_play_id=None, last_play_type=None) -> Game:
    return Game(
        event_id=event_id, name=name or f"Away{event_id} at Home{event_id}", short_name=f"A{event_id} @ H{event_id}",
        state=state, status_type_id=status_type_id, period=period, clock_s=clock_s,
        home=Side(home_id, "HOM", home_score, home_rank), away=Side(away_id, "AWY", away_score, away_rank),
        home_wp=home_wp, seconds_left=seconds_left, last_play_type=last_play_type, possession=None, date=T0,
        last_play_id=last_play_id,
    )


def make_channel(event_id="1", home_team="Home", away_team="Away", spanish=False) -> Channel:
    return Channel(event_id=event_id, dispatcharr_channel_id=50000 + int(event_id),
                   tvg_id=f"teamarr-event-{event_id}" + ("-spanish" if spanish else ""),
                   channel_name=f"{away_team} vs. {home_team}", home_team=home_team, away_team=away_team)


@pytest.fixture
def t0():
    return T0
