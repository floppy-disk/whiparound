"""Control API for the Docker network: health, state, pin/unpin, the logo, and the off-hours slate stream.
No auth by design; bind it to a network that only the stack can reach."""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from .slate import SlateSource

MAX_BODY = 4096
MAX_PIN_MINUTES = 24 * 60
CHUNK = 64 * 1024


class ChangerApi:
    def __init__(self, state_fn: Callable[[], dict], pin_fn: Callable[[str, int], None],
                 unpin_fn: Callable[[], None], logo_path: Path,
                 slate_fn: "Callable[[], SlateSource] | None" = None,
                 bind: str = "0.0.0.0", port: int = 8787, log: Callable[..., object] = lambda *a, **k: None):
        self.state_fn, self.pin_fn, self.unpin_fn = state_fn, pin_fn, unpin_fn
        self.logo_path, self.slate_fn = Path(logo_path), slate_fn
        self.bind, self.port, self.log = bind, port, log
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._server = ThreadingHTTPServer((self.bind, self.port), _make_handler(self))
        self.port = self._server.server_address[1]  # the real port when 0 was asked for
        self._thread = threading.Thread(target=self._server.serve_forever, kwargs={"poll_interval": 0.05}, name="changer-api", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._server is None:
            return
        self._server.shutdown()
        self._server.server_close()
        if self._thread is not None:
            self._thread.join()
        self._server = self._thread = None


def _make_handler(api: ChangerApi):
    class Handler(BaseHTTPRequestHandler):
        status = 0

        def log_message(self, *args):  # default stderr logging off; we log one JSON line per request
            pass

        def send_response(self, code, message=None):
            self.status = code
            super().send_response(code, message)

        def _reply(self, code: int, body: bytes = b"", ctype: str = "application/json", **headers):
            self.send_response(code)
            if code != 204:
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
            for k, v in headers.items():
                self.send_header(k.replace("_", "-"), v)
            self.end_headers()
            if body:
                self.wfile.write(body)

        def _json(self, code: int, obj) -> None:
            self._reply(code, json.dumps(obj, default=str).encode())

        def _dispatch(self, method: str) -> None:
            t0 = time.monotonic()
            path = self.path.split("?", 1)[0]
            try:
                route = getattr(self, f"_{method}_{path.strip('/').replace('.', '_') or 'root'}", None)
                if route is None:
                    self._json(404, {"error": "not found"})
                else:
                    route()
            except (BrokenPipeError, ConnectionError):
                pass
            except Exception as e:
                api.log("api_error", method=method, path=path[:100], error=type(e).__name__)
                if not self.status:
                    self._json(500, {"error": "internal"})
            finally:
                api.log("api_request", method=method, path=path[:100], status=self.status,
                        ms=round((time.monotonic() - t0) * 1000))

        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

        def do_DELETE(self):
            self._dispatch("DELETE")

        def _GET_healthz(self):
            self._json(200, {"ok": True})

        def _GET_state(self):
            self._json(200, api.state_fn())

        def _POST_pin(self):
            try:
                n = int(self.headers.get("Content-Length") or 0)
                if not 0 < n <= MAX_BODY:
                    raise ValueError
                body = json.loads(self.rfile.read(n))
                event_id, minutes = body["event_id"], body["minutes"]
                if (not isinstance(event_id, str) or not event_id.strip() or len(event_id) > 32
                        or isinstance(minutes, bool) or not isinstance(minutes, int)
                        or not 1 <= minutes <= MAX_PIN_MINUTES):
                    raise ValueError
            except (ValueError, KeyError, TypeError):
                return self._json(400, {"error": "body must be {\"event_id\": str, \"minutes\": 1-1440}"})
            try:
                api.pin_fn(event_id.strip(), minutes)
            except ValueError as e:  # the runner refusing an unknown event
                return self._json(400, {"error": str(e)})
            self._reply(204)

        def _DELETE_pin(self):
            api.unpin_fn()
            self._reply(204)

        def _GET_logo_png(self):
            try:
                data = api.logo_path.read_bytes()
            except OSError:
                return self._json(404, {"error": "no logo"})
            self._reply(200, data, "image/png", Cache_Control="public, max-age=86400")

        def _GET_slate_ts(self):
            if api.slate_fn is None:
                return self._json(404, {"error": "no slate"})
            source = api.slate_fn()
            proc = source.open()
            try:
                self.close_connection = True
                self.send_response(200)
                self.send_header("Content-Type", "video/mp2t")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Connection", "close")
                self.end_headers()
                while chunk := proc.stdout.read1(CHUNK):  # whatever is ready, not a full 64 KiB
                    self.wfile.write(chunk)
                    self.wfile.flush()
            finally:
                source.close(proc)

    return Handler
