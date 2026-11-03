"""The control API on real sockets (port 0), and the slate stream against a real ffmpeg when one is installed."""
import json
import shutil
import socket
import time
from http.client import HTTPConnection

import pytest

from whiparound.api import ChangerApi
from whiparound.slate import SlateSource, render_slate

LOGO = b"\x89PNG\r\n\x1a\nnot really a png"


class Recorder:
    def __init__(self):
        self.pins, self.unpins, self.logs = [], 0, []

    def pin(self, event_id, minutes):
        self.pins.append((event_id, minutes))

    def unpin(self):
        self.unpins += 1

    def log(self, event, **fields):
        self.logs.append((event, fields))


@pytest.fixture
def rec():
    return Recorder()


@pytest.fixture
def make_api(rec, tmp_path):
    apis = []
    logo = tmp_path / "logo.png"
    logo.write_bytes(LOGO)

    def make(slate_fn=None):
        api = ChangerApi(lambda: {"mode": "dry", "pick": "4018"}, rec.pin, rec.unpin, logo, slate_fn,
                         bind="127.0.0.1", port=0, log=rec.log)
        api.start()
        apis.append(api)
        return api

    yield make
    for api in apis:
        api.stop()


def call(api, method, path, body=None):
    conn = HTTPConnection("127.0.0.1", api.port, timeout=5)
    try:
        raw = body if isinstance(body, (bytes, type(None))) else json.dumps(body).encode()
        conn.request(method, path, body=raw)
        r = conn.getresponse()
        return r.status, dict(r.getheaders()), r.read()
    finally:
        conn.close()


def test_healthz_and_state(make_api):
    api = make_api()
    assert call(api, "GET", "/healthz")[::2] == (200, b'{"ok": true}')
    status, _, body = call(api, "GET", "/state")
    assert status == 200 and json.loads(body) == {"mode": "dry", "pick": "4018"}


def test_pin_round_trip(make_api, rec):
    api = make_api()
    assert call(api, "POST", "/pin", {"event_id": "401800001", "minutes": 30})[0] == 204
    assert rec.pins == [("401800001", 30)]
    assert call(api, "DELETE", "/pin")[0] == 204
    assert rec.unpins == 1


@pytest.mark.parametrize("body", [b"", b"not json", b"[]", {"event_id": "1"}, {"minutes": 5},
                                  {"event_id": "", "minutes": 5}, {"event_id": 1, "minutes": 5},
                                  {"event_id": "1", "minutes": "5"}, {"event_id": "1", "minutes": 0},
                                  {"event_id": "1", "minutes": True}, {"event_id": "1", "minutes": 99999}])
def test_bad_pin_is_400(make_api, rec, body):
    api = make_api()
    assert call(api, "POST", "/pin", body)[0] == 400
    assert rec.pins == []


def test_logo(make_api):
    api = make_api()
    status, headers, body = call(api, "GET", "/logo.png")
    assert status == 200 and body == LOGO
    assert headers["Content-Type"] == "image/png" and headers["Cache-Control"] == "public, max-age=86400"


def test_404_and_slate_unconfigured(make_api):
    api = make_api()
    assert call(api, "GET", "/nope")[0] == 404
    assert call(api, "GET", "/slate.ts")[0] == 404
    assert call(api, "PUT", "/state")[0] in (404, 501)


def test_one_log_line_per_request_without_body(make_api, rec):
    api = make_api()
    call(api, "POST", "/pin", {"event_id": "SECRET-ID", "minutes": 5})
    event, fields = rec.logs[-1]
    assert event == "api_request" and fields["method"] == "POST" and fields["path"] == "/pin"
    assert fields["status"] == 204 and "ms" in fields and "SECRET-ID" not in json.dumps(rec.logs)


def test_stop_closes_socket(make_api):
    api = make_api()
    port = api.port
    api.stop()
    with socket.socket() as s:
        with pytest.raises(OSError):
            s.settimeout(1)
            s.connect(("127.0.0.1", port))
    again = ChangerApi(lambda: {}, lambda *a: None, lambda: None, api.logo_path, bind="127.0.0.1", port=port)
    again.start()
    again.stop()


class TrackedSlate(SlateSource):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.procs = []

    def open(self):
        proc = super().open()
        self.procs.append(proc)
        return proc


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_slate_streams_ts_and_reaps_ffmpeg_on_disconnect(make_api, tmp_path):
    png = render_slate(["No game on"], tmp_path / "slate.png")
    source = TrackedSlate(png)
    api = make_api(lambda: source)
    want = 188 * 20
    with socket.create_connection(("127.0.0.1", api.port), timeout=10) as s:
        s.sendall(b"GET /slate.ts HTTP/1.1\r\nHost: x\r\n\r\n")
        data = b""
        while b"\r\n\r\n" not in data:
            data += s.recv(4096)
        head, _, body = data.partition(b"\r\n\r\n")
        assert head.startswith(b"HTTP/1.0 200") and b"video/mp2t" in head
        while len(body) < want:
            body += s.recv(65536)
    assert body[0] == 0x47 and body[188] == 0x47
    assert len(source.procs) == 1
    deadline = time.monotonic() + 5
    while source.procs[0].poll() is None and time.monotonic() < deadline:
        time.sleep(0.05)
    assert source.procs[0].poll() is not None
