"""The switching rules (spec Q5, Q17): a pure state machine over already-ranked, already-delayed candidates."""
from dataclasses import dataclass
from datetime import datetime

from .leverage import Params, PlayState, Scored, seconds_remaining


@dataclass(frozen=True)
class Decision:
    pick: Scored | None
    switched: bool
    reason: str
    previous: str | None
    hopped: bool = False


class Decider:
    def __init__(self, params: Params):
        self.p = params
        self.current: str | None = None
        self.since: datetime | None = None
        self.pinned: str | None = None
        self.pin_until: datetime | None = None
        self.hopped_from: str | None = None   # set while on a break-hop destination (dwell and hysteresis do not apply)
        self.last_hop_at: datetime | None = None

    def pin(self, event_id: str, until: datetime) -> None:
        self.pinned, self.pin_until = event_id, until

    def unpin(self) -> None:
        self.pinned, self.pin_until = None, None

    def _take(self, pick: Scored | None, now: datetime, reason: str) -> Decision:
        prev = self.current
        if pick is None:
            self.current, self.since, self.hopped_from = None, None, None
            return Decision(None, prev is not None, reason, prev)
        switched = pick.event_id != prev
        if switched:
            self.current, self.since = pick.event_id, now
            if not reason.startswith("hop"):
                self.hopped_from = None
        return Decision(pick, switched, reason, prev, reason in ("hop", "hop_rerank", "hop_stay"))

    def _hop_target(self, cur: Scored, ranked: list[Scored], play_state: dict[str, PlayState],
                    above_only: bool = False) -> Scored | None:
        """The best-ranked other game in play that is the same tier as the current one or better, or an upset alert
        (which may come from any tier). With `above_only`, only games ranked above the current one count."""
        for s in ranked:
            if s.event_id == cur.event_id:
                if above_only:
                    return None
                continue
            ps = play_state.get(s.event_id)
            if (s.tier <= cur.tier or s.upset) and ps is not None and ps.in_play:
                return s
        return None

    def step(self, now: datetime, ranked: list[Scored], play_state: dict[str, PlayState] | None = None) -> Decision:
        by_id = {s.event_id: s for s in ranked}
        top = ranked[0] if ranked else None

        if self.pinned is not None:
            if self.pin_until is not None and now >= self.pin_until:
                self.unpin()
            elif self.pinned in by_id:
                return self._take(by_id[self.pinned], now, "pinned")

        if self.current is None:
            return self._take(top, now, "initial" if top else "nothing_live")
        cur = by_id.get(self.current)
        if cur is None:
            return self._take(top, now, "current_gone" if top else "nothing_live")
        on_top = top is None or top.event_id == cur.event_id

        if not on_top:
            if cur.game.state == "post":
                return self._take(top, now, "final")
            if cur.game.halftime:
                return self._take(top, now, "halftime")

        if (cur.game.period >= cur.game.rules.regulation_periods and seconds_remaining(cur.game) < self.p.lock_in_s
                and cur.game.margin <= self.p.lock_in_margin):
            return self._take(cur, now, "still_best" if on_top else "lock_in")

        if play_state is not None:
            ps_cur = play_state.get(cur.event_id)
            interval_ok = (self.last_hop_at is None
                           or (now - self.last_hop_at).total_seconds() >= self.p.hop_min_interval_s)
            if ps_cur is not None and ps_cur.in_break and interval_ok:
                dest = self._hop_target(cur, ranked, play_state)
                if dest is not None:
                    self.hopped_from = None if dest.event_id == top.event_id else cur.event_id
                    self.last_hop_at = now
                    return self._take(dest, now, "hop")
            if self.hopped_from is not None:
                stayed = (now - self.last_hop_at).total_seconds() if self.last_hop_at is not None else 1e9
                if on_top:
                    self.hopped_from = None  # the destination is now the top pick: back on the normal path
                elif stayed < self.p.hop_min_stay_s and cur.game.state == "in" and not cur.game.halftime:
                    return self._take(cur, now, "hop_stay")  # never bounce back within an ad pod's length
                else:
                    # While on a hop destination: move up to the best in-play game ranked above the current one
                    # (the top pick when it is in play, else e.g. an upset alert that woke up). 2026-10-17 21:40Z.
                    better = self._hop_target(cur, ranked, play_state, above_only=True)
                    if better is not None:
                        if better.event_id == top.event_id:
                            self.hopped_from = None  # taking the top pick ends the hop
                        return self._take(better, now, "hop_rerank")
                    return self._take(cur, now, "hop_hold")

        if on_top:
            return self._take(cur, now, "still_best")

        if self.since is not None and (now - self.since).total_seconds() < self.p.dwell_s:
            return self._take(cur, now, "dwell")

        if top.tier < cur.tier:
            return self._take(top, now, "better_tier")
        if top.tier == cur.tier and top.leverage - cur.leverage >= self.p.hysteresis:
            return self._take(top, now, "leverage")
        return self._take(cur, now, "hysteresis")
