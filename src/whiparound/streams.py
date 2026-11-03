"""Per-game stream memory (2026-10-24): which stream Dispatcharr settled on for a game on the changer channel, and which
it skipped past. Dispatcharr tries a channel's streams in list order and fails over on errors; the Pitt @ Miami tune-in
took 46 s because the first three streams were dead at the provider and the changer re-copied the event channel's order
(dead streams on top) on every switch. Pure: no I/O, no clock of its own, stream ids only. Memory is in-process; a
restart forgets it (by design)."""
from datetime import datetime, timedelta


class StreamMemory:
    def __init__(self, ttl_s: float = 21600.0):
        self.ttl = timedelta(seconds=ttl_s)
        self.good: dict[str, int] = {}      # event_id -> the stream last seen playing for that game
        # (event_id, stream_id) -> when a failover last skipped it. Per game, not per stream: a provider's event slot
        # ("ESPN UNLTD 048") is reused for later games and may work then (review, 2026-10-24).
        self.dead: dict[tuple[str, int], datetime] = {}

    def _expire(self, now: datetime) -> None:
        self.dead = {s: at for s, at in self.dead.items() if now - at < self.ttl}

    def order(self, event_id: str, streams: list[int], now: datetime) -> list[int]:
        """`streams` reordered: the remembered good stream, then the rest in their order minus dead, then the dead in
        their order. A permutation of the input; unchanged when nothing is remembered."""
        self._expire(now)
        good = self.good.get(event_id)
        rest = list(streams)
        first = []
        if good in rest:
            rest.remove(good)                # one occurrence, so a duplicate id in the input is never dropped
            first = [good]
        dead = {s for (e, s) in self.dead if e == event_id}
        return first + [s for s in rest if s not in dead] + [s for s in rest if s in dead]

    def learn(self, event_id: str, applied: list[int], playing: int, now: datetime) -> tuple[bool, list[int]]:
        """What the status reply taught us: (learned, streams newly marked dead). `applied` is the list last applied to
        the changer channel for this game, `playing` the stream_id Dispatcharr reports. A stream that is not in the
        list (a stale reply mid-switch) teaches nothing."""
        if playing not in applied:
            return False, []
        self._expire(now)
        ahead = applied[:applied.index(playing)]
        newly = [s for s in ahead if (event_id, s) not in self.dead]
        for s in ahead:
            self.dead[(event_id, s)] = now
        self.dead.pop((event_id, playing), None)  # it is playing, so it is not dead
        self.good[event_id] = playing
        return bool(ahead), newly

    def observe(self, event_id: str, applied: list[int], playing: int, now: datetime) -> bool:
        """`learn` without the detail: True when a failover was seen (streams ahead of `playing` marked dead)."""
        return self.learn(event_id, applied, playing, now)[0]
