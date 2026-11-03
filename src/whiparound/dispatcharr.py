"""Dispatcharr's REST and live-proxy API as the changer's admin user. Bearer JWT; relogin once on 401.
Nothing here returns, logs or raises a response body: channel and stream rows carry provider URLs."""
import httpx

from .http import ClientError

TOKEN_PATH = "/api/accounts/token/"          # Django SimpleJWT; verify against the source before deploy
CHANNEL_PATH = "/api/channels/channels/{id}/"
STATUS_PATH = "/proxy/ts/status/{uuid}"            # the live proxy is keyed by the channel UUID, not its id
CHANGE_STREAM_PATH = "/proxy/ts/change_stream/{uuid}"
CHANNEL_KEYS = ("id", "name", "uuid")
STATUS_KEYS = ("state", "stream_id", "client_count", "clients")


class DispatcharrClient:
    def __init__(self, http: httpx.Client, user: str, password: str):
        self.http = http
        self._user = user
        self._password = password
        self._access: str | None = None

    def login(self) -> None:
        try:
            r = self.http.post(TOKEN_PATH, json={"username": self._user, "password": self._password})
        except httpx.HTTPError as e:
            raise ClientError(f"login: {type(e).__name__}: {e}") from None
        if r.status_code != 200:
            raise ClientError(f"login failed: HTTP {r.status_code}")
        try:
            access = r.json().get("access")
        except ValueError:
            access = None
        if not access:
            raise ClientError("login: no access token in reply")
        self._access = access

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._access}"} if self._access else {}

    def _call(self, method: str, path: str, allow_404: bool = False, **kw):
        if self._access is None:
            self.login()
        try:
            r = self.http.request(method, path, headers=self._headers(), **kw)
            if r.status_code == 401:
                self.login()
                r = self.http.request(method, path, headers=self._headers(), **kw)
        except httpx.HTTPError as e:
            raise ClientError(f"{method} {path}: {type(e).__name__}: {e}") from None
        if r.status_code == 404 and allow_404:
            return None
        if r.status_code >= 400:
            raise ClientError(f"{method} {path}: HTTP {r.status_code}")
        if not r.content:
            return {}
        try:
            return r.json()
        except ValueError:
            return {}

    def channel(self, channel_id: int) -> dict:
        data = self._call("GET", CHANNEL_PATH.format(id=channel_id)) or {}
        out = {k: data.get(k) for k in CHANNEL_KEYS}
        out["streams"] = [int(s["id"]) if isinstance(s, dict) else int(s) for s in data.get("streams") or []]
        return out

    def set_streams(self, channel_id: int, stream_ids: list[int]) -> None:
        self._call("PATCH", CHANNEL_PATH.format(id=channel_id), json={"streams": [int(s) for s in stream_ids]})

    def status(self, channel_uuid: str) -> dict | None:
        """Running-channel state, or None when the channel is not running (404). Takes the channel's UUID."""
        data = self._call("GET", STATUS_PATH.format(uuid=channel_uuid), allow_404=True)
        if data is None:
            return None
        return {k: data.get(k) for k in STATUS_KEYS if k in data}

    def change_stream(self, channel_uuid: str, stream_id: int) -> dict:
        """Switch the running channel to `stream_id` in place; the viewer's connection survives. Takes the UUID."""
        data = self._call("POST", CHANGE_STREAM_PATH.format(uuid=channel_uuid), json={"stream_id": int(stream_id)}) or {}
        return {k: data.get(k) for k in ("status", "message", "stream_id") if k in data}
