"""Leverage, decided, tier: the printed formula (spec Q17). Pure functions; every number is a Params field, and how a
sport counts time (period length, regulation periods, the stop) rides on each Game as `game.rules`."""
import math
from dataclasses import asdict, dataclass, field

from .docket import Channel
from .espn import Game

TIER_LISTED, TIER_RANKED, TIER_REST = 0, 1, 2
TIER_WORDS = {TIER_LISTED: "listed team", TIER_RANKED: "ranked", TIER_REST: "other"}


@dataclass(frozen=True)
class Params:
    base_weight: float = 0.4          # leverage = closeness * (base + (1-base) * progress)
    both_ranked_bonus: float = 0.15
    overtime_bonus: float = 0.25
    decided_wp: float = 0.97
    decided_margin_any: int = 25
    decided_margin_late: int = 17
    decided_min_margin: int = 0       # below this margin a game is never decided (NHL 2: a pulled goalie keeps a one-goal game live)
    decided_late_s: float = 600.0
    fallback_base: float = 8.0        # closeness without win probability: 1 - margin / (base + slope * progress)
    fallback_slope: float = 24.0
    # Win probability when ESPN gives none. "none": the margin fallback above (NCAAF). "poisson": the hockey model below
    # (NHL: ESPN publishes no hockey win probability). `goals_per_60` is each team's scoring rate; the runner overwrites
    # it from the standings every RANKINGS_REFRESH_S.
    wp_model: str = "none"
    goals_per_60: float = 3.0
    top_rank: int = 25
    both_ranked_rank: int = 25        # the both-ranked bonus needs both sides at least this good (NHL: 4)
    ranked_needs_both: bool = False   # tier RANKED needs either side ranked (NCAAF) or both sides (NHL)
    # TEAM_WEIGHTING=0: the listed teams and the ranks count for nothing. Every game is tier REST with no listed index, no
    # both-ranked bonus and no upset alert; the late-close shield then protects any close, undecided late game.
    team_weighting: bool = True
    # USE_RANKS=0 (the human, 2026-11-03): the AP poll / playoff spots count for nothing — no "ranked" tier, no both-ranked
    # bonus, no upset alert — while the listed teams still do. The standings are still read for the NHL scoring rate.
    use_ranks: bool = True
    dwell_s: float = 600.0
    hysteresis: float = 0.15
    lock_in_s: float = 180.0
    lock_in_margin: int = 8
    # Late-close shield (2026-10-17): a listed or ranked game in the 4th quarter/OT and
    # within one score outranks every other listed or ranked game, including one that just kicked off. An unranked game is
    # never shielded: it stays in its own tier. Among shielded games, tiers then leverage as usual.
    shield_period: int = 4
    shield_margin: int = 8            # "close" = within one score, as the lock-in rule counts it
    # Upset alert: a ranked favourite trailing a much lower-ranked (or unranked) team from the second half on.
    upset_period: int = 3
    upset_rank_gap: int = 10
    upset_bonus: float = 0.20
    # Break hop: when the game on screen is in a commercial break, hop to the best game in play, same tier or better.
    hop_in_play_s: float = 90.0   # 2026-10-17 21:20Z: 40 s found no destination during a 2-min ad; snaps are 30-45 s apart       # a play change within this many seconds = in play
    hop_after_s: float = 30.0     # 2026-10-17 21:20Z: 60 s hopped ~45 s into the ad         # after an ESPN break signal on the current game, no play change this long = in a break
    hop_min_interval_s: float = 180.0  # at most one hop per this
    hop_min_stay_s: float = 90.0        # 2026-10-17 21:43Z: hopped and re-ranked back 20 s later; an ad pod is >= 90 s
    # Per-signal wait before a play gap counts as a break (a score is followed by the PAT and kickoff before the ad).
    hop_after_by_signal: dict = field(default_factory=lambda: {"score": 75.0, "halftime": 0.0})
    hop_break_types: tuple[str, ...] = ("Timeout", "End Period", "End of Half", "End of Game")


@dataclass(frozen=True)
class PlayState:
    in_play: bool
    in_break: bool


@dataclass(frozen=True)
class Scored:
    game: Game
    channel: Channel
    tier: int
    leverage: float
    closeness: float
    progress: float
    decided: bool
    listed_index: int
    inputs: dict = field(default_factory=dict)
    shielded: bool = False
    upset: bool = False

    @property
    def event_id(self) -> str:
        return self.game.event_id


def late_close(g: Game, game_tier: int, p: Params) -> bool:
    """Listed or ranked, in the last period or overtime, within one score (a decided game is already tier REST): the shield.
    With team weighting off every game is tier REST, so the tier test is replaced by the one thing it also did: a decided
    game is not shielded."""
    wanted = game_tier != TIER_REST if p.team_weighting else not decided(g, p)
    return (wanted and g.state == "in" and not g.halftime and g.period >= p.shield_period
            and g.margin <= p.shield_margin)


def progress(g: Game) -> float:
    if g.state == "pre":
        return 0.0
    if g.state == "post":
        return 1.0
    r = g.rules
    if g.halftime and r.break_midgame:
        return 0.5
    if g.overtime:
        return 1.0
    if g.period <= 0:
        return 0.0
    # An every-period stop (NHL intermission) is the end of the period just played: its clock reads 0.
    clock = 0.0 if g.halftime else min(max(g.clock_s, 0.0), r.period_s)
    return min(1.0, ((g.period - 1) + (r.period_s - clock) / r.period_s) / r.regulation_periods)


def seconds_remaining(g: Game) -> float:
    """Regulation seconds left. ESPN's own figure when present; 0 in overtime or after the game."""
    if g.seconds_left is not None:
        return float(max(g.seconds_left, 0))
    r = g.rules
    regulation_s = r.regulation_periods * r.period_s
    if g.state == "post" or g.overtime:
        return 0.0
    if g.halftime and r.break_midgame:
        return regulation_s / 2
    if g.period <= 0:
        return regulation_s
    clock = 0.0 if g.halftime else min(max(g.clock_s, 0.0), r.period_s)
    return (r.regulation_periods - g.period) * r.period_s + clock


POISSON_TERMS = 20   # goals per side summed in the Skellam pmf; 20 more goals in 60 minutes is not a thing


def _poisson(n: int, mu: float) -> float:
    return math.exp(-mu) * mu ** n / math.factorial(n)


def hockey_leader_wp(margin: int, seconds_left: float, lam_per_60: float) -> float:
    """The leading side's chance to win in regulation, as a Poisson race (the NHL has no published win probability).

    Each side scores at `lam_per_60` goals per 60 minutes, so each expects mu = lam * seconds_left / 3600 more goals.
    D = trailer's goals - leader's goals over the rest of regulation is Skellam(mu, mu):
    P(D = k) = sum_j Poisson(j + k; mu) * Poisson(j; mu), j cut at POISSON_TERMS. The leader wins outright if D < margin;
    D == margin ties the game and it goes to overtime, which we call a coin flip. A tie game (margin 0) is 0.5."""
    if margin <= 0:
        return 0.5
    mu = max(lam_per_60, 0.0) * max(seconds_left, 0.0) / 3600.0

    def pmf(k: int) -> float:
        return sum(_poisson(j + k, mu) * _poisson(j, mu) for j in range(POISSON_TERMS + 1))

    p_tie = pmf(margin)
    p_trailer_ahead = sum(pmf(k) for k in range(margin + 1, margin + POISSON_TERMS + 1))
    return min(1.0, max(0.5, 1.0 - p_tie / 2.0 - p_trailer_ahead))


def home_win_prob(g: Game, p: Params | None = None) -> tuple[float | None, str | None]:
    """(home side's win probability, where it came from): ESPN's when it supplied one, else the Poisson model when this
    sport uses it, else (None, None). In overtime or a shootout the model says 0.5; the overtime bonus does the rest."""
    if g.home_wp is not None:
        return g.home_wp, "espn"
    if p is None or p.wp_model != "poisson" or g.state != "in":
        return None, None
    if g.overtime or g.home.score == g.away.score:
        return 0.5, "poisson"
    leader = hockey_leader_wp(g.margin, seconds_remaining(g), p.goals_per_60)
    return (leader if g.home.score > g.away.score else 1.0 - leader), "poisson"


def leader_wp(g: Game, p: Params | None = None) -> float | None:
    home, _ = home_win_prob(g, p)
    if home is None:
        return None
    home = min(max(home, 0.0), 1.0)
    return max(home, 1.0 - home)


def closeness(g: Game, p: Params) -> float:
    wp = leader_wp(g, p)
    if wp is not None:
        return max(0.0, 1.0 - 2.0 * (wp - 0.5))
    return max(0.0, 1.0 - g.margin / (p.fallback_base + p.fallback_slope * progress(g)))


def both_ranked(g: Game, ranks: dict[str, int], p: Params) -> bool:
    """Both sides at least `both_ranked_rank` (the bonus's bar; the tier's bar is top_rank)."""
    return _rank(g.home, ranks, p) <= p.both_ranked_rank and _rank(g.away, ranks, p) <= p.both_ranked_rank


def tier_ranked(g: Game, ranks: dict[str, int], p: Params) -> bool:
    """The RANKED tier: either side within top_rank (NCAAF: either team in the AP 25), or, when the sport says
    `ranked_needs_both`, both sides (NHL: both teams in a playoff spot)."""
    home, away = _rank(g.home, ranks, p) <= p.top_rank, _rank(g.away, ranks, p) <= p.top_rank
    return (home and away) if p.ranked_needs_both else (home or away)


def either_ranked(g: Game, ranks: dict[str, int], p: Params) -> bool:
    return _rank(g.home, ranks, p) <= p.top_rank or _rank(g.away, ranks, p) <= p.top_rank


def _rank(side, ranks: dict[str, int], p: Params) -> int:
    """The weekly AP poll (NHL: playoff seed) first, the scoreboard's own curatedRank second. 99 for all when team
    weighting is off."""
    if not p.team_weighting or not p.use_ranks:
        return 99
    return ranks.get(side.team_id) or side.rank


def upset(g: Game, ranks: dict[str, int], p: Params) -> bool:
    """Live, second half or later, not decided, and the rank favourite (ranked <= top_rank, the other side unranked or at
    least upset_rank_gap spots worse) is strictly trailing."""
    if g.state != "in" or g.halftime or g.period < p.upset_period or decided(g, p):
        return False
    home, away = _rank(g.home, ranks, p), _rank(g.away, ranks, p)
    fav, other = (g.home, g.away) if home < away else (g.away, g.home)
    fav_rank, other_rank = min(home, away), max(home, away)
    if fav_rank > p.top_rank or (other_rank != 99 and other_rank - fav_rank < p.upset_rank_gap):
        return False
    return fav.score < other.score


def leverage(g: Game, ranks: dict[str, int], p: Params) -> float:
    value = closeness(g, p) * (p.base_weight + (1.0 - p.base_weight) * progress(g))
    if both_ranked(g, ranks, p):
        value += p.both_ranked_bonus
    if g.overtime and g.state == "in":
        value += p.overtime_bonus
    if upset(g, ranks, p):
        value += p.upset_bonus
    return round(value, 4)


def decided(g: Game, p: Params) -> bool:
    if g.margin < p.decided_min_margin:
        return False
    wp = leader_wp(g, p)
    if wp is not None and wp >= p.decided_wp:
        return True
    if g.margin >= p.decided_margin_any:
        return True
    return g.margin >= p.decided_margin_late and seconds_remaining(g) < p.decided_late_s


def listed_index(ch: Channel, listed: list[str], p: Params | None = None) -> int:
    """Position of the first listed team playing in this channel; len(listed) when none (always, with weighting off)."""
    best = len(listed)
    if p is not None and not p.team_weighting:
        return best
    for i, team in enumerate(listed):
        if team in (ch.home_team, ch.away_team):
            best = min(best, i)
    return best


def tier(g: Game, ch: Channel, listed: list[str], ranks: dict[str, int], p: Params) -> int:
    if decided(g, p):
        return TIER_REST
    if listed_index(ch, listed, p) < len(listed):
        return TIER_LISTED
    if tier_ranked(g, ranks, p):
        return TIER_RANKED
    return TIER_REST


def score(g: Game, ch: Channel, listed: list[str], ranks: dict[str, int], p: Params) -> Scored:
    t = tier(g, ch, listed, ranks, p)
    home_wp, wp_source = home_win_prob(g, p)
    return Scored(
        game=g, channel=ch,
        tier=t,
        leverage=leverage(g, ranks, p),
        closeness=round(closeness(g, p), 4),
        progress=round(progress(g), 4),
        decided=decided(g, p),
        listed_index=listed_index(ch, listed, p),
        shielded=late_close(g, t, p),
        upset=upset(g, ranks, p),
        inputs={
            "home_wp": home_wp, "wp_source": wp_source, "wp_carried": g.wp_carried, "margin": g.margin, "period": g.period, "clock_s": g.clock_s,
            "seconds_left": seconds_remaining(g), "home_rank": _rank(g.home, ranks, p), "away_rank": _rank(g.away, ranks, p),
            "home_score": g.home.score, "away_score": g.away.score, "state": g.state, "halftime": g.halftime,
        },
    )


def rank_candidates(games: list[Game], docket: dict[str, Channel], listed: list[str], ranks: dict[str, int],
                    p: Params) -> list[Scored]:
    """Live games with an event channel, best first: shielded (late and close) first, then tier, leverage, listed order, id."""
    out = []
    for g in games:
        ch = docket.get(g.event_id)
        if ch is None or not g.live:
            continue
        out.append(score(g, ch, listed, ranks, p))
    out.sort(key=lambda s: (0 if s.shielded else 1, s.tier, -s.leverage, s.listed_index, s.event_id))
    return out


def params_dict(p: Params) -> dict:
    return asdict(p)
