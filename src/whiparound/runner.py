"""Entry point: configuration from the environment, the cycle loop, the delayed-snapshot ring, the switch."""
import os
import signal
import sys
import threading
import time
from contextlib import nullcontext
from collections import deque
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .api import ChangerApi
from .breaks import ArmedWindow, BreakParams, VideoBreakDetector, break_state, is_break_signal
from .decider import Decider
from .discord import post as discord_post
from .slate import SlateSource, next_kickoff_lines, render_slate
from .dispatcharr import DispatcharrClient
from .docket import Channel, TeamarrClient, build_docket, load_listed_teams
from .espn import EspnClient, Game, WpMemory, carry_wp
from .http import ClientError, make_http_client
from .leverage import TIER_WORDS, Params, PlayState, Scored, params_dict, rank_candidates
from .log import JsonlLog
from .sport import NCAAF, Sport, get_sport, scoreboard_dates
from .standings import StandingsClient, split_url
from .streams import StreamMemory


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


def env_credential(suffix: str) -> str:
    """`CHANGER_<suffix>`, else the older `NCAAF_CHANGER_<suffix>` (so the live NCAAF service keeps working unchanged)."""
    return os.environ.get(f"CHANGER_{suffix}") or os.environ.get(f"NCAAF_CHANGER_{suffix}", "")


class SnapshotRing:
    """Scoreboard fetches by fetch time; `delayed(now)` is the newest at least `delay_s` old (else the oldest)."""

    def __init__(self, delay_s: float, keep_s: float | None = None):
        self.delay = timedelta(seconds=delay_s)
        self.keep = timedelta(seconds=keep_s if keep_s is not None else max(delay_s * 4, 300))
        self.items: deque[tuple[datetime, list[Game]]] = deque()

    def push(self, at: datetime, games: list[Game]) -> None:
        self.items.append((at, games))
        while self.items and at - self.items[0][0] > self.keep and len(self.items) > 1:
            self.items.popleft()

    def delayed(self, now: datetime) -> tuple[datetime, list[Game]] | None:
        chosen = None
        for at, games in self.items:
            if now - at >= self.delay:
                chosen = (at, games)
        if chosen is None and self.items:
            chosen = self.items[0]
        return chosen


def _alt(s: Scored) -> dict:
    return {"event_id": s.event_id, "name": s.game.short_name or s.game.name, "tier": s.tier, "leverage": s.leverage, "shielded": s.shielded, "upset": s.upset}


def _pick_fields(s: Scored | None) -> dict:
    if s is None:
        return {"pick": None}
    return {"pick": {
        "event_id": s.event_id, "name": s.game.short_name or s.game.name, "channel_name": s.channel.channel_name,
        "dispatcharr_channel_id": s.channel.dispatcharr_channel_id, "tier": s.tier, "tier_word": TIER_WORDS[s.tier],
        "leverage": s.leverage, "closeness": s.closeness, "progress": s.progress, "decided": s.decided, "shielded": s.shielded, "upset": s.upset,
        "inputs": s.inputs,
    }}


class Runner:
    def __init__(self, *, espn: EspnClient, teamarr: TeamarrClient, dispatcharr: DispatcharrClient | None,
                 discord_http, webhook: str, log: JsonlLog, params: Params, listed_path: Path,
                 channel_id: int | None, dry_run: bool, cycle_s: float, delay_s: float,
                 docket_refresh_s: float, rankings_refresh_s: float, tz: ZoneInfo,
                 dispatcharr_url: str = "", slate_path: Path | None = None,
                 detector_factory: "callable | None" = None, idle_cycle_s: float = 120.0,
                 stream_memory_s: float = 21600.0, sport: Sport = NCAAF,
                 standings: StandingsClient | None = None):
        self.sport, self.standings = sport, standings
        self.espn, self.teamarr, self.dispatcharr = espn, teamarr, dispatcharr
        self.dispatcharr_url, self.slate_path = dispatcharr_url.rstrip("/"), slate_path
        # Break detection (issue 04): ESPN signals arm; only the video detector declares a break; nothing acts on it yet.
        self.armed = ArmedWindow()
        self.break_params = BreakParams()
        self.prev_games: dict[str, Game] = {}
        self.wp_memory: dict[str, WpMemory] = {}
        self.detector_factory = detector_factory
        self.detector: VideoBreakDetector | None = None
        # Stream memory (2026-10-24): the list last applied to the changer channel, and the game it was applied for.
        self.streams = StreamMemory(stream_memory_s)
        self.applied_event_id: str | None = None
        self.applied_streams: list[int] = []
        self.changer_uuid: str | None = None
        self.latest_games: list[Game] = []
        self.last_cycle: dict = {}
        self.last_video_event_wall: datetime | None = None
        self.only_us_cycles = 0
        self.discord_http, self.webhook = discord_http, webhook
        self.log, self.params, self.listed_path = log, params, listed_path
        self.channel_id, self.dry_run = channel_id, dry_run
        self.cycle_s, self.tz = cycle_s, tz
        self.idle_cycle_s = idle_cycle_s
        self.last_candidates: int | None = None   # candidates in the last cycle; picks the sleep (cycle_s or idle_cycle_s)
        self.cadence_s = cycle_s
        # Play freshness (break hop): event_id -> (last play id, snapshot time it last changed)
        self.last_play_seen: dict[str, tuple[str | None, datetime]] = {}
        self.docket_refresh = timedelta(seconds=docket_refresh_s)
        self.rankings_refresh = timedelta(seconds=rankings_refresh_s)
        self.ring = SnapshotRing(delay_s)
        self.decider = Decider(params)
        self.docket: dict[str, Channel] = {}
        self.listed: list[str] = []
        self.ranks: dict[str, int] = {}
        self.docket_at: datetime | None = None
        self.ranks_at: datetime | None = None
        self.upset_alerted: set[str] = set()
        self.stop = threading.Event()

    # -- refreshes -------------------------------------------------------------------------------------------
    def refresh_docket(self, now: datetime) -> None:
        try:
            self.listed = load_listed_teams(self.listed_path.read_text(encoding="utf-8"))
        except OSError as e:
            self.log.write("config_error", error=f"listed teams: {type(e).__name__}")
        try:
            rows = self.teamarr.managed()
        except ClientError as e:
            self.log.write("docket_error", error=str(e))
            return
        self.docket = build_docket(rows, self.sport.teamarr_league)
        self.docket_at = now
        self.log.write("docket", channels=len(self.docket), listed=self.listed,
                       sample=[c.channel_name for c in list(self.docket.values())[:5]])

    def refresh_rankings(self, now: datetime) -> None:
        """The sport's standing of teams, every RANKINGS_REFRESH_S: the AP poll (NCAAF) or the standings (NHL: playoff
        seed per team, and the league scoring rate that becomes the Poisson model's lambda)."""
        source = "standings" if self.standings is not None else "ap"
        try:
            if self.standings is not None:
                result = self.standings.standings()
                self.ranks = result.ranks
                if result.goals_per_60 is not None:   # nothing played yet: keep the last rate (default 3.0)
                    self.set_goals_per_60(result.goals_per_60)
            else:
                self.ranks = self.espn.rankings()
            self.ranks_at = now
            fields = {"goals_per_60": round(self.params.goals_per_60, 4)} if source == "standings" else {}
            self.log.write("rankings", source=source, ranked=sum(1 for r in self.ranks.values() if r <= self.params.top_rank),
                           **fields)
        except ClientError as e:
            self.log.write("espn_error", what="standings" if source == "standings" else "rankings", error=str(e))

    def set_goals_per_60(self, rate: float) -> None:
        self.params = replace(self.params, goals_per_60=rate)
        self.decider.p = self.params

    # -- one cycle --------------------------------------------------------------------------------------------
    def cycle(self, now: datetime) -> None:
        if self.docket_at is None or now - self.docket_at >= self.docket_refresh:
            self.refresh_docket(now)
        if self.ranks_at is None or now - self.ranks_at >= self.rankings_refresh:
            self.refresh_rankings(now)
        dates = scoreboard_dates(self.sport, now, self.tz)
        try:
            games = self.espn.scoreboard(dates)
        except ClientError as e:
            self.log.write("espn_error", what="scoreboard", error=str(e), held=self.decider.current)
            return
        games = carry_wp(games, self.wp_memory, now)
        self.ring.push(now, games)
        self.latest_games = games
        snap = self.ring.delayed(now)
        if snap is None:
            return
        snap_at, snap_games = snap
        ranked = rank_candidates(snap_games, self.docket, self.listed, self.ranks, self.params)
        self.last_candidates = len(ranked)
        self.update_play_seen(snap_games, snap_at)
        # Break state of the game on screen is read before the decision, so the hop rule sees the latest video state.
        breaks = self.check_breaks(now, self.decider.current, ranked, snap_games)
        play_state = self.build_play_state(now, snap_at, ranked, breaks["video_in_break"])
        on_screen = self.decider.current
        decision = self.decider.step(now, ranked, play_state)
        self.last_cycle = self.log.write(
            "cycle", **_pick_fields(decision.pick), reason=decision.reason, switched=decision.switched,
            team_weighting=self.params.team_weighting, use_ranks=self.params.use_ranks,
            previous=decision.previous, snapshot_age_s=round((now - snap_at).total_seconds(), 1),
            live=sum(1 for g in snap_games if g.live), candidates=len(ranked),
            alternatives=[_alt(s) for s in ranked[:4] if decision.pick is None or s.event_id != decision.pick.event_id][:3],
            breaks=breaks,
            hop={"current_in_break": play_state.get(on_screen, PlayState(False, False)).in_break,
                 "in_play": [s.event_id for s in ranked[:5] if play_state[s.event_id].in_play],
                 "hopped_from": self.decider.hopped_from},
        )
        self.check_upsets(ranked, snap_games)
        if decision.switched:
            self.switch(decision.pick, decision.previous, decision.reason, ranked, now)

    # -- play freshness (break hop) ---------------------------------------------------------------------------
    def update_play_seen(self, snap_games: list[Game], snap_at: datetime) -> None:
        """Remember, per game, the last play id and the snapshot time it first showed; forget games not in the snapshot."""
        ids = {g.event_id for g in snap_games}
        self.last_play_seen = {k: v for k, v in self.last_play_seen.items() if k in ids}
        for g in snap_games:
            if g.state != "in":
                continue
            seen = self.last_play_seen.get(g.event_id)
            if seen is None or seen[0] != g.last_play_id:
                self.last_play_seen[g.event_id] = (g.last_play_id, snap_at)

    def play_age_s(self, event_id: str, snap_at: datetime) -> float | None:
        """Seconds, in snapshot time, since this game's last play id changed; None if the game was never seen live."""
        seen = self.last_play_seen.get(event_id)
        return None if seen is None else (snap_at - seen[1]).total_seconds()

    def build_play_state(self, now: datetime, snap_at: datetime, ranked: list[Scored], video_in_break: bool) -> dict[str, PlayState]:
        """In play = a fresh play that is not itself a break play. In a break: only for the game on screen (we cannot see
        the others' video, and a play gap alone is not a break): the video detector says so, or an ESPN break signal is
        armed and the plays have stopped for hop_after_s."""
        p, current = self.params, self.decider.current
        out = {}
        for s in ranked:
            age = self.play_age_s(s.event_id, snap_at)
            lp = (s.game.last_play_type or "").lower()
            in_play = (age is not None and age <= p.hop_in_play_s
                       and not any(t.lower() in lp for t in p.hop_break_types))
            after = p.hop_after_by_signal.get(self.armed.reason, p.hop_after_s) if self.armed.armed(now) else None
            in_break = (s.event_id == current
                        and (video_in_break or (after is not None and age is not None and age >= after)))
            out[s.event_id] = PlayState(in_play, in_break)
        return out

    # -- upset alert ------------------------------------------------------------------------------------------
    def check_upsets(self, ranked: list[Scored], snap_games: list[Game]) -> None:
        """One alert per game per process, when its upset flag first goes true; forgotten only once the game is not live."""
        self.upset_alerted &= {g.event_id for g in snap_games if g.live}
        for s in ranked:
            if not s.upset or s.event_id in self.upset_alerted:
                continue
            self.upset_alerted.add(s.event_id)
            g = s.game
            fav, dog = (g.home, g.away) if g.home.score < g.away.score else (g.away, g.home)  # the favourite trails
            self.log.write("upset_alert", event_id=s.event_id, name=g.short_name or g.name,
                           home_score=g.home.score, away_score=g.away.score, period=g.period, clock_s=g.clock_s,
                           home_rank=s.inputs["home_rank"], away_rank=s.inputs["away_rank"], leverage=s.leverage)
            clock = f"{int(g.clock_s) // 60}:{int(g.clock_s) % 60:02d}"
            fav_rank = s.inputs["home_rank"] if fav is g.home else s.inputs["away_rank"]
            text = (f"⚠ Upset alert: {dog.abbrev} {dog.score} – #{fav_rank} {fav.abbrev} {fav.score}, "
                    f"{self.sport.period_label}{g.period} {clock}" + (" [dry run]" if self.dry_run else ""))
            if self.webhook:
                try:
                    discord_post(self.discord_http, self.webhook, text)
                except ClientError as e:
                    self.log.write("discord_error", error=str(e))

    # -- break detection (logging only) -----------------------------------------------------------------------
    def check_breaks(self, now: datetime, current: str | None, ranked: list[Scored], snap_games: list[Game]) -> dict:
        """ESPN signals on the game on screen arm a window; the video detector (attached only while the changer channel
        is actually running, so it never opens a provider connection of its own) says whether a break is on."""
        signal_name = None
        pick = next((s for s in ranked if s.event_id == current), None)
        if pick is not None:
            signal_name = is_break_signal(self.prev_games.get(pick.event_id), pick.game, self.sport.end_plays)
            if signal_name and not (self.armed.armed(now) and self.armed.reason == signal_name):
                self.armed.arm(now, signal_name)
                self.log.write("break_signal", event_id=pick.event_id, name=pick.game.short_name, signal=signal_name)
        self.prev_games = {g.event_id: g for g in snap_games}
        self.tend_detector(now)
        events = self.detector.events(now - timedelta(minutes=15)) if self.detector is not None else []
        for e in events:  # log each black/silence event once, so the thresholds can be tuned from the log (issue 04)
            if self.last_video_event_wall is None or e.end_wall > self.last_video_event_wall:
                self.log.write("video_event", kind=e.kind, start=e.start_wall.isoformat(timespec="milliseconds"),
                               duration_s=round(e.duration_s, 3), armed=self.armed.armed(now))
        if events:
            self.last_video_event_wall = max(e.end_wall for e in events)
        video = break_state(events, now, self.break_params)
        armed = self.armed.armed(now)
        return {"armed": armed, "armed_reason": self.armed.reason if armed else None,
                "video_attached": self.detector is not None, "video_in_break": video.in_break,
                "video_reason": video.reason or None, "video_events": video.event_count, "events_15m": len(events)}

    def tend_detector(self, now: datetime) -> None:
        """Attach the detector to the changer channel's own output while a *viewer* is watching; detach when the only
        client left is the detector itself (2026-10-17 22:10Z: it had kept the channel, a provider connection and ~1 CPU
        alive after the viewer left). Two quiet cycles before detaching, so a viewer's reconnect does not flap it."""
        if self.detector_factory is None or self.dispatcharr is None or self.channel_id is None or self.dry_run:
            return
        try:
            status = self.dispatcharr.status(self.changer_uuid_or_fetch())
            running = status is not None
            n = 0
            if running:
                n = status.get("client_count") if isinstance(status.get("client_count"), int) else len(status.get("clients") or [])
            self.learn_streams(now, status)
            ours = 1 if self.detector is not None else 0
            viewers = max(0, n - ours)
            if running and self.detector is None and viewers >= 1 and self.changer_uuid:
                self.detector = self.detector_factory()
                self.detector.start(f"{self.dispatcharr_url}/proxy/ts/stream/{self.changer_uuid}")
                self.only_us_cycles = 0
                self.log.write("detector_attach", channel_id=self.channel_id, viewers=viewers)
            elif self.detector is not None and (not running or viewers == 0):
                self.only_us_cycles += 1
                if not running or self.only_us_cycles >= 2:
                    self.detector.stop()
                    self.detector = None
                    self.only_us_cycles = 0
                    self.log.write("detector_detach", channel_id=self.channel_id,
                                   reason="channel stopped" if not running else "no viewer left")
            else:
                self.only_us_cycles = 0
        except ClientError as e:
            self.log.write("dispatcharr_error", what="status", error=str(e))

    def learn_streams(self, now: datetime, status: dict | None) -> None:
        """From the status reply `tend_detector` already fetched (no call of its own): when the changer channel is playing
        the game we last applied a list for and Dispatcharr has settled on a stream other than the list's first, the
        streams ahead of it failed over (2026-10-24: three dead feeds, a 46 s tune-in). Remember that, and PATCH the
        changer's list so a fresh tune-in starts on the working stream. Never `change_stream`: the viewer is already
        on it. Stream ids only in the log."""
        if not status or status.get("state") != "active" or not self.applied_streams:
            return
        event_id = self.applied_event_id
        playing = status.get("stream_id")
        if event_id is None or event_id != self.decider.current or isinstance(playing, bool) or not isinstance(playing, int):
            return
        learned, newly_dead = self.streams.learn(event_id, self.applied_streams, playing, now)
        if not learned:
            return
        new_order = self.streams.order(event_id, self.applied_streams, now)
        reordered = False
        if new_order != self.applied_streams:
            try:
                self.dispatcharr.set_streams(self.channel_id, new_order)
                self.applied_streams = new_order
                reordered = True
            except ClientError as e:
                self.log.write("dispatcharr_error", what="set_streams", error=str(e))
        self.log.write("stream_learned", event_id=event_id, good=playing, dead=newly_dead, reordered=reordered)

    def changer_uuid_or_fetch(self) -> str:
        """The changer channel's UUID (the live proxy's key), read once from Dispatcharr and cached."""
        if self.changer_uuid is None and self.dispatcharr is not None and self.channel_id is not None:
            self.changer_uuid = self.dispatcharr.channel(self.channel_id).get("uuid") or ""
        return self.changer_uuid or ""

    # -- the control API's callbacks --------------------------------------------------------------------------
    def state(self) -> dict:
        iso = lambda d: d.isoformat(timespec="seconds") if d is not None else None  # noqa: E731
        return {"current": self.decider.current, "since": iso(self.decider.since), "pinned": self.decider.pinned,
                "pin_until": iso(self.decider.pin_until), "sport": self.sport.key, "dry_run": self.dry_run, "channel_id": self.channel_id,
                "docket_channels": len(self.docket), "listed": self.listed, "last_cycle": self.last_cycle}

    def pin(self, event_id: str, minutes: int) -> None:
        if event_id not in self.docket:
            raise ValueError("unknown event_id: not on today's docket")
        self.decider.pin(event_id, datetime.now(timezone.utc) + timedelta(minutes=minutes))
        self.log.write("pin", event_id=event_id, minutes=minutes)

    def unpin(self) -> None:
        self.decider.unpin()
        self.log.write("unpin")

    def slate(self) -> SlateSource:
        """One fresh still per request: the next kickoff from the latest scoreboard."""
        path = self.slate_path or Path("/tmp/whiparound-slate.png")
        render_slate(next_kickoff_lines(self.latest_games, datetime.now(timezone.utc), self.tz), path,
                     title=self.sport.title)
        return SlateSource(path)

    # -- the switch -------------------------------------------------------------------------------------------
    def switch(self, pick: Scored | None, previous: str | None, reason: str, ranked: list[Scored],
               now: datetime | None = None) -> None:
        prev_name = next((s.game.short_name for s in ranked if s.event_id == previous), previous)
        if pick is None:
            self.log.write("switch", to=None, previous=previous, reason=reason, applied=False)
            return
        applied, detail = False, None
        if not self.dry_run and self.channel_id is not None and self.dispatcharr is not None:
            try:
                source = self.dispatcharr.channel(pick.channel.dispatcharr_channel_id)
                source_streams = source["streams"]
                if source_streams:
                    # Remembered-good stream first, streams a failover skipped last (2026-10-24).
                    streams = self.streams.order(pick.event_id, source_streams, now or datetime.now(timezone.utc))
                    self.dispatcharr.set_streams(self.channel_id, streams)
                    self.applied_event_id, self.applied_streams = pick.event_id, list(streams)
                    status = self.dispatcharr.status(self.changer_uuid_or_fetch())
                    if status is not None:
                        detail = self.dispatcharr.change_stream(self.changer_uuid_or_fetch(), streams[0])
                    applied = True
                    detail = {"streams": len(streams), "running": status is not None, "reply": detail,
                              "order_changed": streams != source_streams}
                else:
                    detail = {"streams": 0}
            except ClientError as e:
                detail = {"error": str(e)}
        self.log.write("switch", to=pick.event_id, name=pick.game.short_name, previous=previous, reason=reason,
                       dry_run=self.dry_run, applied=applied, detail=detail)
        text = (f"→ {pick.game.short_name or pick.game.name} ({TIER_WORDS[pick.tier]}, leverage {pick.leverage:.2f}, "
                f"{reason}); was {prev_name or 'nothing'}" + (" [dry run]" if self.dry_run else ""))
        if self.webhook:
            try:
                discord_post(self.discord_http, self.webhook, text)
            except ClientError as e:
                self.log.write("discord_error", error=str(e))

    # -- loop -------------------------------------------------------------------------------------------------
    def next_interval_s(self) -> float:
        """cycle_s while there are candidates, idle_cycle_s when the last cycle had none; logs once when it flips."""
        interval = self.idle_cycle_s if self.last_candidates == 0 else self.cycle_s
        if interval != self.cadence_s:
            self.cadence_s = interval
            self.log.write("cadence", cycle_s=interval, candidates=self.last_candidates)
        return interval

    def run(self) -> None:
        self.log.write("start", sport=self.sport.key, dry_run=self.dry_run, channel_id=self.channel_id, params=params_dict(self.params),
                       cycle_s=self.cycle_s, idle_cycle_s=self.idle_cycle_s, delay_s=self.ring.delay.total_seconds())
        while not self.stop.is_set():
            now = datetime.now(timezone.utc)
            try:
                self.cycle(now)
            except Exception as e:  # noqa: BLE001 - keep the loop alive; the line names the type only
                self.log.write("cycle_error", error=f"{type(e).__name__}: {e}")
            elapsed = (datetime.now(timezone.utc) - now).total_seconds()
            self.stop.wait(max(1.0, self.next_interval_s() - elapsed))
        if self.detector is not None:
            self.detector.stop()
            self.detector = None
        self.log.write("stop", current=self.decider.current)


def main() -> int:
    try:
        sport = get_sport(os.environ.get("SPORT"))
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 2
    espn_base = os.environ.get("ESPN_BASE", "https://site.api.espn.com")
    teamarr_url = os.environ.get("TEAMARR_URL", "http://teamarr:9195")
    dispatcharr_url = os.environ.get("DISPATCHARR_URL", "http://dispatcharr:9191")
    user = env_credential("DISPATCHARR_USER")
    password = env_credential("DISPATCHARR_PASSWORD")
    webhook = env_credential("DISCORD_WEBHOOK")
    channel_raw = os.environ.get("CHANGER_CHANNEL_ID", "")
    channel_id = int(channel_raw) if channel_raw.strip() else None
    dry_run = os.environ.get("DRY_RUN", "1") != "0"
    log_raw = os.environ.get("LOG_PATH", f"/data/{sport.log_name}")
    log = JsonlLog(Path(log_raw) if log_raw else None)
    if not dry_run and (not user or not password):
        print("CHANGER_DISPATCHARR_USER and _PASSWORD (or the older NCAAF_CHANGER_DISPATCHARR_USER and _PASSWORD) "
              "must be set when DRY_RUN=0", file=sys.stderr)
        return 2

    standings_base, standings_path = split_url(sport.standings_url) if sport.standings_url else ("", "")
    with make_http_client(espn_base, 10.0) as espn_http, \
            make_http_client(teamarr_url, 15.0) as teamarr_http, \
            make_http_client(dispatcharr_url, 10.0) as dispatcharr_http, \
            make_http_client("https://discord.com", 10.0) as discord_http, \
            (make_http_client(standings_base, 10.0) if standings_base else nullcontext()) as standings_http:
        runner = Runner(
            espn=EspnClient(espn_http, sport), teamarr=TeamarrClient(teamarr_http),
            dispatcharr=DispatcharrClient(dispatcharr_http, user, password) if user else None,
            discord_http=discord_http, webhook=webhook, log=log,
            params=replace(sport.params, team_weighting=os.environ.get("TEAM_WEIGHTING", "1").strip() != "0",
                           use_ranks=os.environ.get("USE_RANKS", "1").strip() != "0"),
            listed_path=Path(os.environ.get("LISTED_TEAMS_FILE", "/config/listed-teams.txt")),
            channel_id=channel_id, dry_run=dry_run,
            cycle_s=_env_float("CYCLE_S", 60.0), idle_cycle_s=_env_float("IDLE_CYCLE_S", 120.0), delay_s=_env_float("DELAY_S", 45.0),
            docket_refresh_s=_env_float("DOCKET_REFRESH_S", 600.0),
            rankings_refresh_s=_env_float("RANKINGS_REFRESH_S", 21600.0),
            tz=ZoneInfo(os.environ.get("TZ_ET", "America/New_York")),
            stream_memory_s=_env_float("STREAM_MEMORY_S", 21600.0),
            dispatcharr_url=dispatcharr_url,
            slate_path=Path(os.environ.get("SLATE_PATH", "/data/slate.png")),
            detector_factory=lambda: VideoBreakDetector(log=log.write), sport=sport,
            standings=StandingsClient(standings_http, standings_path) if standings_http is not None else None,
        )
        api = ChangerApi(runner.state, runner.pin, runner.unpin,
                         logo_path=Path(os.environ.get("LOGO_PATH", f"/app/{sport.logo_file}")),
                         slate_fn=runner.slate, bind=os.environ.get("API_BIND", "0.0.0.0"),
                         port=int(os.environ.get("API_PORT", "8787")), log=log.write)

        def _stop(signum, frame):
            runner.stop.set()

        signal.signal(signal.SIGTERM, _stop)
        signal.signal(signal.SIGINT, _stop)
        api.start()
        log.write("api_start", port=api.port)
        try:
            runner.run()
        finally:
            api.stop()
    return 0
