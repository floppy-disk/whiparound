import json
from datetime import timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from whiparound.decider import Decider
from whiparound.docket import Channel
from whiparound.http import ClientError
from whiparound.leverage import Params
from whiparound.log import JsonlLog
from whiparound.runner import Runner, SnapshotRing
from conftest import make_game


def test_snapshot_ring_returns_delayed_or_oldest(t0):
    ring = SnapshotRing(45.0)
    g0, g1, g2 = [make_game("1", clock_s=c) for c in (300.0, 240.0, 180.0)]
    ring.push(t0, [g0])
    assert ring.delayed(t0)[1] == [g0]  # nothing old enough yet: the oldest
    ring.push(t0 + timedelta(seconds=60), [g1])
    ring.push(t0 + timedelta(seconds=120), [g2])
    at, games = ring.delayed(t0 + timedelta(seconds=120))
    assert games == [g1] and at == t0 + timedelta(seconds=60)
    at, games = ring.delayed(t0 + timedelta(seconds=170))
    assert games == [g2]


class FakeEspn:
    def __init__(self):
        self.games = []
        self.fail = False

    def scoreboard(self, date_et):
        assert date_et == ["20261017"]   # one Eastern day per GET; the runner passes the list
        if self.fail:
            raise ClientError("GET x: HTTP 503 http://secret")
        return list(self.games)

    def rankings(self):
        return {"h": 3}


class FakeTeamarr:
    def managed(self):
        return [{"event_id": "1", "league": "college-football", "dispatcharr_channel_id": 1001,
                 "tvg_id": "teamarr-event-1", "home_team": "Home", "away_team": "Notre Dame Fighting Irish",
                 "channel_name": "ND vs Home"},
                {"event_id": "2", "league": "college-football", "dispatcharr_channel_id": 1002,
                 "tvg_id": "teamarr-event-2", "home_team": "X", "away_team": "Y", "channel_name": "Y vs X"}]


class FakeDispatcharr:
    def __init__(self):
        self.calls = []

    def channel(self, cid):
        self.calls.append(("channel", cid))
        return {"id": cid, "uuid": f"u-{cid}", "streams": [5, 6]}

    def set_streams(self, cid, ids):
        self.calls.append(("set_streams", cid, ids))

    def status(self, cid):
        self.calls.append(("status", cid))
        return {"state": "active"}

    def change_stream(self, cid, sid):
        self.calls.append(("change_stream", cid, sid))
        return {"status": "success"}


def make_runner(tmp_path, espn, dispatcharr=None, dry_run=True, channel_id=90001):
    listed = tmp_path / "listed.txt"
    listed.write_text("Notre Dame Fighting Irish\n")
    log = JsonlLog(tmp_path / "log.jsonl")
    return Runner(espn=espn, teamarr=FakeTeamarr(), dispatcharr=dispatcharr, discord_http=None, webhook="",
                  log=log, params=Params(), listed_path=listed, channel_id=channel_id, dry_run=dry_run,
                  cycle_s=60, delay_s=0, docket_refresh_s=600, rankings_refresh_s=21600,
                  tz=ZoneInfo("America/New_York"))


def read_log(tmp_path):
    return [json.loads(l) for l in (tmp_path / "log.jsonl").read_text().splitlines()]


def test_cycle_logs_pick_and_dry_run_switch(tmp_path, t0):
    espn = FakeEspn()
    espn.games = [make_game("1", home_wp=0.5), make_game("2", home_wp=0.5)]
    r = make_runner(tmp_path, espn)
    r.cycle(t0)
    events = [e["event"] for e in read_log(tmp_path)]
    assert events == ["docket", "rankings", "cycle", "switch"]
    cyc = read_log(tmp_path)[2]
    assert cyc["pick"]["event_id"] == "1" and cyc["pick"]["tier_word"] == "listed team"
    assert cyc["reason"] == "initial" and cyc["switched"] and cyc["candidates"] == 2
    assert cyc["alternatives"][0]["event_id"] == "2"
    sw = read_log(tmp_path)[3]
    assert sw["dry_run"] and not sw["applied"]
    assert "http" not in (tmp_path / "log.jsonl").read_text()


def test_espn_error_holds_and_redacts(tmp_path, t0):
    espn = FakeEspn()
    espn.games = [make_game("1", home_wp=0.5)]
    r = make_runner(tmp_path, espn)
    r.cycle(t0)
    espn.fail = True
    r.cycle(t0 + timedelta(seconds=60))
    last = read_log(tmp_path)[-1]
    assert last["event"] == "espn_error" and last["held"] == "1" and "secret" not in json.dumps(last)
    assert r.decider.current == "1"


def test_real_switch_calls_dispatcharr_in_order(tmp_path, t0):
    espn = FakeEspn()
    espn.games = [make_game("1", home_wp=0.5)]
    dc = FakeDispatcharr()
    r = make_runner(tmp_path, espn, dispatcharr=dc, dry_run=False)
    r.cycle(t0)
    assert dc.calls == [("channel", 1001), ("set_streams", 90001, [5, 6]), ("channel", 90001), ("status", "u-90001"),
                        ("change_stream", "u-90001", 5)]
    sw = read_log(tmp_path)[-1]
    assert sw["event"] == "switch" and sw["applied"] and sw["detail"]["streams"] == 2 and sw["detail"]["running"]


def test_no_channel_id_never_calls_dispatcharr(tmp_path, t0):
    espn = FakeEspn()
    espn.games = [make_game("1", home_wp=0.5)]
    dc = FakeDispatcharr()
    r = make_runner(tmp_path, espn, dispatcharr=dc, dry_run=False, channel_id=None)
    r.cycle(t0)
    assert dc.calls == []


def test_upset_alert_logged_once_across_cycles(tmp_path, t0):
    espn = FakeEspn()  # rankings(): {"h": 3}; home "h" is #3 and trails an unranked team in Q4
    espn.games = [make_game("1", period=4, clock_s=300.0, home_score=10, away_score=17, home_wp=0.4)]
    r = make_runner(tmp_path, espn)
    r.cycle(t0)
    r.cycle(t0 + timedelta(seconds=60))
    alerts = [e for e in read_log(tmp_path) if e["event"] == "upset_alert"]
    assert len(alerts) == 1
    assert alerts[0]["event_id"] == "1" and alerts[0]["home_rank"] == 3 and alerts[0]["away_rank"] == 99
    cyc = [e for e in read_log(tmp_path) if e["event"] == "cycle"][0]
    assert cyc["pick"]["upset"] is True
    # the game leaves the live set: the alert is forgotten
    espn.games = [make_game("1", state="post", period=4, clock_s=0.0, home_score=10, away_score=17)]
    r.cycle(t0 + timedelta(seconds=120))
    assert r.upset_alerted == set()


def test_upset_alert_discord_text(tmp_path, t0, monkeypatch):
    posted = []
    monkeypatch.setattr("whiparound.runner.discord_post", lambda http, hook, text: posted.append(text))
    espn = FakeEspn()
    espn.games = [make_game("1", period=4, clock_s=305.0, home_score=10, away_score=17, home_wp=0.4)]
    r = make_runner(tmp_path, espn)
    r.webhook = "https://example.invalid/hook"
    r.cycle(t0)
    assert [t for t in posted if "Upset" in t] == ["⚠ Upset alert: AWY 17 – #3 HOM 10, Q4 5:05 [dry run]"]


def test_play_age_grows_and_resets_on_a_new_play(tmp_path, t0):
    espn = FakeEspn()
    espn.games = [make_game("1", last_play_id="p1"), make_game("2", last_play_id="q1")]
    r = make_runner(tmp_path, espn)
    r.cycle(t0)
    assert r.play_age_s("1", t0) == 0.0 and r.play_age_s("9", t0) is None
    r.cycle(t0 + timedelta(seconds=60))
    assert r.play_age_s("1", t0 + timedelta(seconds=60)) == 60.0
    r.cycle(t0 + timedelta(seconds=120))
    assert r.play_age_s("1", t0 + timedelta(seconds=120)) == 120.0
    espn.games = [make_game("1", last_play_id="p2")]
    r.cycle(t0 + timedelta(seconds=180))
    assert r.play_age_s("1", t0 + timedelta(seconds=180)) == 0.0
    assert set(r.last_play_seen) == {"1"}   # game 2 left the snapshot


def test_break_hop_end_to_end_and_cycle_hop_field(tmp_path, t0):
    espn = FakeEspn()   # game 2 is ranked (tier 1); game 1 is a listed team (tier 0)
    espn.games = [make_game("2", home_wp=0.6, period=3, last_play_id="q1")]
    r = make_runner(tmp_path, espn)
    r.cycle(t0)                                           # picks 2
    espn.games = [make_game("2", home_wp=0.6, period=3, last_play_id="q1", last_play_type="Timeout"),
                  make_game("1", home_wp=0.5, period=3, last_play_id="p1")]
    r.cycle(t0 + timedelta(seconds=60))                   # the timeout arms the window; no play on 2 for 60 s
    cyc = [e for e in read_log(tmp_path) if e["event"] == "cycle"][-1]
    assert cyc["hop"] == {"current_in_break": True, "in_play": ["1"], "hopped_from": None}  # 1 is also the top pick
    assert cyc["reason"] == "hop" and cyc["pick"]["event_id"] == "1" and cyc["switched"]
    sw = read_log(tmp_path)[-1]
    assert sw["event"] == "switch" and sw["reason"] == "hop"


def test_cadence_follows_candidates_and_logs_the_flip(tmp_path, t0):
    espn = FakeEspn()
    espn.games = []
    r = make_runner(tmp_path, espn)
    assert r.next_interval_s() == 60                      # nothing seen yet
    r.cycle(t0)
    assert r.next_interval_s() == 120.0
    r.next_interval_s()
    espn.games = [make_game("1")]
    r.cycle(t0 + timedelta(seconds=120))
    assert r.next_interval_s() == 60
    flips = [e for e in read_log(tmp_path) if e["event"] == "cadence"]
    assert [e["cycle_s"] for e in flips] == [120.0, 60]


def test_detector_attaches_only_for_a_viewer_and_detaches_when_alone(tmp_path, t0):
    """2026-10-17 22:10Z: the detector's own connection kept the channel alive after the viewer left."""
    from datetime import timedelta

    class Det:
        def __init__(self): self.started = None; self.stopped = False
        def start(self, url): self.started = url
        def stop(self): self.stopped = True
        def events(self, since): return []

    class DC(FakeDispatcharr):
        def __init__(self): super().__init__(); self.clients = 0; self.running = True
        def status(self, uid):
            return {"state": "active", "clients": [{} for _ in range(self.clients)]} if self.running else None

    espn = FakeEspn(); espn.games = [make_game("1", home_wp=0.5)]
    dc = DC()
    r = make_runner(tmp_path, espn, dispatcharr=dc, dry_run=False)
    made = []
    r.detector_factory = lambda: made.append(Det()) or made[-1]
    r.tend_detector(t0)                      # channel running, 0 clients: nobody watching -> no detector
    assert not made
    dc.clients = 1                           # a viewer
    r.tend_detector(t0 + timedelta(seconds=20))
    assert len(made) == 1 and r.detector is made[0]
    dc.clients = 2                           # viewer + us
    r.tend_detector(t0 + timedelta(seconds=40))
    assert r.detector is made[0]
    dc.clients = 1                           # only us
    r.tend_detector(t0 + timedelta(seconds=60))
    assert r.detector is made[0]             # one quiet cycle: still attached
    r.tend_detector(t0 + timedelta(seconds=80))
    assert r.detector is None and made[0].stopped
    events = [e for e in read_log(tmp_path) if e["event"] in ("detector_attach", "detector_detach")]
    assert [e["event"] for e in events] == ["detector_attach", "detector_detach"] and events[1]["reason"] == "no viewer left"


# -- stream memory (2026-10-24: Pitt @ Miami, three dead feeds ahead of the working one) ----------------------------
class FailoverDispatcharr(FakeDispatcharr):
    """Event 1's channel lists four streams; `playing` is the stream_id the proxy reports for the changer channel."""
    def __init__(self):
        super().__init__()
        self.playing = 1
        self.state = "active"
        self.fail_patch = False

    def channel(self, cid):
        out = super().channel(cid)
        if cid == 1001:
            out["streams"] = [1, 2, 3, 4]
        return out

    def set_streams(self, cid, ids):
        if self.fail_patch and self.calls and any(c[0] == "set_streams" for c in self.calls):
            raise ClientError("PATCH x: HTTP 500 http://secret")
        super().set_streams(cid, ids)

    def status(self, cid):
        self.calls.append(("status", cid))
        return {"state": self.state, "stream_id": self.playing, "clients": []}


def memory_runner(tmp_path, dc, dry_run=False):
    espn = FakeEspn()
    espn.games = [make_game("1", home_wp=0.5)]
    r = make_runner(tmp_path, espn, dispatcharr=dc, dry_run=dry_run)
    r.detector_factory = lambda: (_ for _ in ()).throw(AssertionError("no viewer, no detector"))
    return r


def top_pick(r):
    from whiparound.leverage import rank_candidates
    return rank_candidates(r.latest_games, r.docket, r.listed, r.ranks, r.params)[0]


def test_switch_applies_the_ordered_list_and_cuts_to_its_first(tmp_path, t0):
    dc = FailoverDispatcharr()
    r = memory_runner(tmp_path, dc)
    r.streams.good["1"] = 4
    r.streams.dead.update({("1", 1): t0, ("1", 2): t0})
    r.cycle(t0)
    assert ("set_streams", 90001, [4, 3, 1, 2]) in dc.calls and ("change_stream", "u-90001", 4) in dc.calls
    sw = read_log(tmp_path)[-1]
    assert sw["event"] == "switch" and sw["detail"]["order_changed"] is True
    assert r.applied_event_id == "1" and r.applied_streams == [4, 3, 1, 2]


def test_switch_without_memory_reports_order_unchanged(tmp_path, t0):
    dc = FailoverDispatcharr()
    r = memory_runner(tmp_path, dc)
    r.cycle(t0)
    assert ("set_streams", 90001, [1, 2, 3, 4]) in dc.calls and ("change_stream", "u-90001", 1) in dc.calls
    assert read_log(tmp_path)[-1]["detail"]["order_changed"] is False


def test_failover_in_status_is_learned_and_the_next_switch_starts_on_the_working_stream(tmp_path, t0):
    dc = FailoverDispatcharr()
    r = memory_runner(tmp_path, dc)
    r.cycle(t0)                                                    # applies [1, 2, 3, 4], cuts to 1
    dc.calls.clear()
    dc.playing = 4                                                 # Dispatcharr failed over past 1, 2, 3
    r.cycle(t0 + timedelta(seconds=20))
    assert ("set_streams", 90001, [4, 1, 2, 3]) in dc.calls
    assert not any(c[0] == "change_stream" for c in dc.calls)      # the viewer is already on #4
    learned = [e for e in read_log(tmp_path) if e["event"] == "stream_learned"]
    assert learned and learned[-1]["event_id"] == "1" and learned[-1]["good"] == 4
    assert learned[-1]["dead"] == [1, 2, 3] and learned[-1]["reordered"] is True
    assert r.applied_streams == [4, 1, 2, 3]
    dc.calls.clear()
    r.cycle(t0 + timedelta(seconds=40))                            # now playing the applied first: nothing to learn
    assert not any(c[0] in ("set_streams", "change_stream") for c in dc.calls)
    assert len([e for e in read_log(tmp_path) if e["event"] == "stream_learned"]) == 1
    dc.calls.clear()
    r.switch(top_pick(r), "2", "test", [], t0 + timedelta(seconds=60))   # back to the same game later
    assert ("set_streams", 90001, [4, 1, 2, 3]) in dc.calls and ("change_stream", "u-90001", 4) in dc.calls
    text = (tmp_path / "log.jsonl").read_text()
    assert "http" not in text and "secret" not in text


def test_status_while_not_active_or_for_another_event_is_ignored(tmp_path, t0):
    dc = FailoverDispatcharr()
    r = memory_runner(tmp_path, dc)
    r.cycle(t0)
    dc.calls.clear()
    dc.playing, dc.state = 4, "connecting"
    r.cycle(t0 + timedelta(seconds=20))
    dc.state = "active"
    r.applied_event_id = "2"                                       # the list on the channel is for a different game
    r.cycle(t0 + timedelta(seconds=40))
    assert not any(c[0] in ("set_streams", "change_stream") for c in dc.calls)
    assert r.streams.good == {} and r.streams.dead == {}
    assert not [e for e in read_log(tmp_path) if e["event"] == "stream_learned"]


def test_a_stream_not_in_the_applied_list_is_ignored(tmp_path, t0):
    dc = FailoverDispatcharr()
    r = memory_runner(tmp_path, dc)
    r.cycle(t0)
    dc.calls.clear()
    dc.playing = 99                                                # a stale reply from the previous game
    r.cycle(t0 + timedelta(seconds=20))
    assert not any(c[0] == "set_streams" for c in dc.calls) and r.streams.dead == {}


def test_patch_failure_is_logged_and_retried_next_cycle(tmp_path, t0):
    dc = FailoverDispatcharr()
    r = memory_runner(tmp_path, dc)
    r.cycle(t0)
    dc.playing, dc.fail_patch = 4, True
    r.cycle(t0 + timedelta(seconds=20))
    errs = [e for e in read_log(tmp_path) if e["event"] == "dispatcharr_error"]
    assert errs[-1]["what"] == "set_streams" and "secret" not in json.dumps(errs[-1])
    assert r.applied_streams == [1, 2, 3, 4]
    assert [e for e in read_log(tmp_path) if e["event"] == "stream_learned"][-1]["reordered"] is False
    dc.fail_patch = False
    r.cycle(t0 + timedelta(seconds=40))
    assert r.applied_streams == [4, 1, 2, 3]


def test_dry_run_learns_nothing_and_calls_nothing(tmp_path, t0):
    dc = FailoverDispatcharr()
    r = memory_runner(tmp_path, dc, dry_run=True)
    r.cycle(t0)
    dc.playing = 4
    r.cycle(t0 + timedelta(seconds=20))
    assert dc.calls == [] and r.applied_streams == [] and r.streams.good == {} and r.streams.dead == {}
    assert not [e for e in read_log(tmp_path) if e["event"] == "stream_learned"]
