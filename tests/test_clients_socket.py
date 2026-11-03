"""Clients against a real local HTTP server: real sockets, one connection, nothing left open (rule 2)."""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from whiparound import discord
from whiparound.dispatcharr import DispatcharrClient
from whiparound.docket import TeamarrClient
from whiparound.espn import EspnClient
from whiparound.http import ClientError, make_http_client, redact
from whiparound.sport import NHL
from whiparound.standings import StandingsClient, split_url


class State:
    def __init__(self):
        self.lock = threading.Lock()
        self.open_conns = 0
        self.max_conns = 0
        self.logins = 0
        self.requests = []
        self.token = "tok1"
        self.channel_running = True


def make_handler(state):
    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def setup(self):
            super().setup()
            with state.lock:
                state.open_conns += 1
                state.max_conns = max(state.max_conns, state.open_conns)

        def finish(self):
            super().finish()
            with state.lock:
                state.open_conns -= 1

        def log_message(self, *a):
            pass

        def _send(self, code, body):
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _authed(self):
            return self.headers.get("Authorization") == f"Bearer {state.token}"

        def _body(self):
            return json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")

        def do_GET(self):
            state.requests.append(("GET", self.path, None))
            if self.path.startswith("/apis/site/v2/sports/football/college-football/scoreboard"):
                assert "groups=80" in self.path and "dates=20261017" in self.path
                return self._send(200, {"events": [{"id": "1", "name": "A at B", "shortName": "A @ B",
                                                    "date": "2026-10-17T16:00Z",
                                                    "status": {"period": 1, "clock": 900.0,
                                                               "type": {"id": "2", "state": "in"}},
                                                    "competitions": [{"competitors": [
                                                        {"homeAway": "home", "score": "0", "team": {"id": "1"}},
                                                        {"homeAway": "away", "score": "0", "team": {"id": "2"}}]}]}]})
            if self.path.startswith("/apis/site/v2/sports/hockey/nhl/scoreboard"):
                assert "groups" not in self.path and ("dates=20261016" in self.path or "dates=20261017" in self.path)
                return self._send(200, {"events": [{"id": "9", "name": "A at B", "shortName": "A @ B",
                                                    "date": "2026-10-17T23:00Z",
                                                    "status": {"period": 1, "clock": 0.0,
                                                               "type": {"id": "22", "state": "in"}},
                                                    "competitions": [{"competitors": [
                                                        {"homeAway": "home", "score": "1", "team": {"id": "1"}},
                                                        {"homeAway": "away", "score": "0", "team": {"id": "2"}}]}]}]})
            if self.path == "/apis/v2/sports/hockey/nhl/standings":
                return self._send(200, {"children": [{"standings": {"entries": [
                    {"team": {"id": "7"}, "stats": [{"name": "playoffSeed", "value": 1.0},
                                                    {"name": "pointsFor", "value": 63.0},
                                                    {"name": "gamesPlayed", "value": 16.0}]}]}}]})
            if self.path == "/apis/v2/sports/hockey/nhl/standings-bad":
                return self._send(503, {})
            if self.path.startswith("/apis/site/v2/sports/football/college-football/rankings"):
                return self._send(200, {"rankings": [{"type": "ap", "ranks": [{"current": 1, "team": {"id": "61"}}]}]})
            if self.path == "/api/v1/channels/managed":
                return self._send(200, {"channels": [{"event_id": "1", "logo_url": "http://x/y.png"}], "total": 1})
            if not self._authed():
                return self._send(401, {"detail": "auth"})
            if self.path == "/api/channels/channels/1001/":
                return self._send(200, {"id": 1001, "name": "Texas vs Florida", "uuid": "u-1",
                                        "streams": [{"id": 5, "url": "http://u:p@prov/5.ts"}, 6, 7]})
            if self.path == "/proxy/ts/status/u-9":
                if not state.channel_running:
                    return self._send(404, {"error": "Channel not found"})
                return self._send(200, {"state": "active", "stream_id": 5, "client_count": 1,
                                        "url": "http://u:p@prov/5.ts"})
            self._send(404, {})

        def do_POST(self):
            body = self._body()
            state.requests.append(("POST", self.path, body))
            if self.path == "/api/accounts/token/":
                if body.get("password") != "pw":
                    return self._send(401, {"detail": "bad"})
                state.logins += 1
                return self._send(200, {"access": state.token, "refresh": "r"})
            if self.path == "/hook/abc":
                return self._send(204, {})
            if not self._authed():
                return self._send(401, {"detail": "auth"})
            if self.path == "/proxy/ts/change_stream/u-9":
                return self._send(200, {"status": "success", "message": "Stream changed",
                                        "stream_id": body["stream_id"], "url": "http://u:p@prov/9.ts"})
            self._send(404, {})

        def do_PATCH(self):
            body = self._body()
            state.requests.append(("PATCH", self.path, body))
            if not self._authed():
                return self._send(401, {"detail": "auth"})
            if self.path == "/api/channels/channels/90001/":
                return self._send(200, {"id": 90001, "streams": body["streams"], "url": "http://x"})
            self._send(404, {})
    return H


@pytest.fixture
def server():
    state = State()
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(state))
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield state, f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()
    t.join()


def wait_closed(state):
    for _ in range(100):
        if state.open_conns == 0:
            return True
        time.sleep(0.02)
    return False


def test_espn_client(server):
    state, url = server
    with make_http_client(url, 5) as http:
        espn = EspnClient(http)
        games = espn.scoreboard("20261017")
        assert len(games) == 1 and games[0].event_id == "1" and games[0].live
        assert espn.rankings() == {"61": 1}
    assert state.max_conns == 1 and wait_closed(state)


def test_espn_client_nhl_profile(server):
    state, url = server
    with make_http_client(url, 5) as http:
        games = EspnClient(http, NHL).scoreboard(["20261016", "20261017"])   # two GETs, merged by event id
        assert len(games) == 1 and games[0].halftime and games[0].rules.regulation_periods == 3
        assert EspnClient(http, NHL).rankings() == {}   # the NHL profile has no poll and makes no call for one
    assert [r[1] for r in state.requests if "rankings" in r[1]] == []
    assert state.max_conns == 1 and wait_closed(state)


def test_standings_client(server):
    state, url = server
    base, path = split_url(f"{url}/apis/v2/sports/hockey/nhl/standings")
    assert base == url and path == "/apis/v2/sports/hockey/nhl/standings"
    with make_http_client(base, 10) as http:
        got = StandingsClient(http, path).standings()
        assert got.ranks == {"7": 1} and got.goals_per_60 == pytest.approx(63 / 16)
        with pytest.raises(ClientError) as e:
            StandingsClient(http, "/apis/v2/sports/hockey/nhl/standings-bad").standings()
        assert "503" in str(e.value) and "127.0.0.1" not in str(e.value)
    assert state.max_conns == 1 and wait_closed(state)


def test_teamarr_client(server):
    state, url = server
    with make_http_client(url, 5) as http:
        rows = TeamarrClient(http).managed()
        assert rows[0]["event_id"] == "1"
    assert state.max_conns == 1 and wait_closed(state)


def test_dispatcharr_client_login_relogin_and_switch(server):
    state, url = server
    with make_http_client(url, 5) as http:
        dc = DispatcharrClient(http, "cfb-changer", "pw")
        ch = dc.channel(1001)
        assert ch == {"id": 1001, "name": "Texas vs Florida", "uuid": "u-1", "streams": [5, 6, 7]}
        assert state.logins == 1
        dc.set_streams(90001, [5, 6, 7])
        st = dc.status("u-9")
        assert st == {"state": "active", "stream_id": 5, "client_count": 1}
        reply = dc.change_stream("u-9", 5)
        assert reply == {"status": "success", "message": "Stream changed", "stream_id": 5}
        # token expires: one relogin, call still succeeds
        state.token = "tok2"
        assert dc.channel(1001)["streams"] == [5, 6, 7]
        assert state.logins == 2
        state.channel_running = False
        assert dc.status("u-9") is None
        patch = [r for r in state.requests if r[0] == "PATCH"][0]
        assert patch[2] == {"streams": [5, 6, 7]}
    assert state.max_conns == 1 and wait_closed(state)


def test_dispatcharr_bad_password_raises_without_url(server):
    state, url = server
    with make_http_client(url, 5) as http:
        dc = DispatcharrClient(http, "cfb-changer", "wrong")
        with pytest.raises(ClientError) as e:
            dc.channel(1001)
        assert "401" in str(e.value) and "://" not in str(e.value) and "127.0.0.1" not in str(e.value)
    assert wait_closed(state)


def test_discord_post(server):
    state, url = server
    with make_http_client(url, 5) as http:
        discord.post(http, f"{url}/hook/abc", "→ A @ B (listed team, leverage 0.98, initial); was nothing")
        posted = [r for r in state.requests if r[1] == "/hook/abc"][0]
        assert posted[2]["content"].startswith("→ A @ B")
        with pytest.raises(ClientError) as e:
            discord.post(http, f"{url}/hook/missing", "x")
        assert "/hook/missing" not in str(e.value) and "127.0.0.1" not in str(e.value)
    assert state.max_conns == 1 and wait_closed(state)


def test_redact_strips_urls():
    msg = "GET failed for http://user:secret@host:9191/api/x?token=abc and https://discord.com/api/webhooks/1/zzz"
    out = redact(msg)
    assert "secret" not in out and "zzz" not in out and out.count("<url>") == 2
    assert str(ClientError("boom https://a.b/c")) == "boom <url>"
