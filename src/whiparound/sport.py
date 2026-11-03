"""The sport profile: everything that differs between the NCAAF changer and the NHL changer, in one frozen record.
One image serves both; `SPORT=ncaaf|nhl` picks the profile (default ncaaf, which is what the code did before profiles).
Every number the runner and the leverage functions need about a sport is a field here or in `Sport.params`."""
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .breaks import END_PLAYS
from .espn import PeriodRules
from .leverage import Params

# ESPN NHL play-type names, sampled live 2026-11-02: "Period End", "Period Start",
# "Face Off", "Stoppage", "Goal", "Missed". "Stoppage" is any whistle, so it only arms the video detector.
NHL_END_PLAYS = frozenset({"Period End", "End of Game"})
NHL_HOP_BREAK_TYPES = ("Stoppage", "Period End", "End of Game")


@dataclass(frozen=True)
class Sport:
    key: str
    title: str                       # the slate card
    channel_word: str                # the short name on the logo
    scoreboard_path: str
    scoreboard_params: dict
    scoreboard_days_back: int        # 0: today only. n: also the n previous Eastern days, one GET each, while the clock is
    rankings_path: str | None        # an AP-style poll; None: the sport has none
    standings_url: str | None        # a different host, its own client; None: no standings call
    teamarr_league: str
    period_s: float
    regulation_periods: int
    break_status_id: str             # ESPN status type id of the free-switch stop (halftime / intermission)
    break_midgame: bool              # one stop in the middle (NCAAF) or after every period (NHL)
    period_label: str                # "Q" / "P", for Discord and log text
    end_plays: frozenset
    log_name: str
    logo_file: str
    params: Params
    scoreboard_carry_until_h: int = 4  # with days_back > 0: fetch the previous day(s) only before this Eastern hour

    @property
    def rules(self) -> PeriodRules:
        return PeriodRules(self.period_s, self.regulation_periods, self.break_status_id, self.break_midgame)

    @property
    def ranked_needs_both(self) -> bool:
        """Read from `params`, where the pure tier function can see it."""
        return self.params.ranked_needs_both

    @property
    def hop_break_types(self) -> tuple[str, ...]:
        return self.params.hop_break_types


NCAAF = Sport(
    key="ncaaf", title="NCAAF CHANGER", channel_word="NCAAF",
    scoreboard_path="/apis/site/v2/sports/football/college-football/scoreboard", scoreboard_params={"groups": "80"},
    scoreboard_days_back=0,
    rankings_path="/apis/site/v2/sports/football/college-football/rankings", standings_url=None,
    teamarr_league="college-football",
    period_s=900.0, regulation_periods=4, break_status_id="23", break_midgame=True, period_label="Q",
    end_plays=frozenset(END_PLAYS), log_name="cfb-changer.jsonl", logo_file="assets/logo.png",
    params=Params(),
)

NHL = Sport(
    key="nhl", title="NHL CHANGER", channel_word="NHL",
    scoreboard_path="/apis/site/v2/sports/hockey/nhl/scoreboard", scoreboard_params={},
    scoreboard_days_back=1,   # NHL nights cross midnight ET
    rankings_path=None, standings_url="https://site.web.api.espn.com/apis/v2/sports/hockey/nhl/standings",
    teamarr_league="nhl",
    period_s=1200.0, regulation_periods=3, break_status_id="22", break_midgame=False, period_label="P",
    end_plays=NHL_END_PLAYS, log_name="nhl-changer.jsonl", logo_file="assets/logo-nhl.png",
    params=Params(
        decided_wp=0.99, decided_min_margin=2,   # 2 up: decided ~4 min out; 3 up: ~15 min; 1 up: never (pulled goalie)
        decided_margin_any=4, decided_margin_late=3, decided_late_s=600.0, lock_in_margin=1,
        shield_period=3, shield_margin=1, upset_period=3, upset_rank_gap=6,
        top_rank=8, both_ranked_rank=4, ranked_needs_both=True,
        hop_after_by_signal={"score": 45.0, "halftime": 0.0}, hop_break_types=NHL_HOP_BREAK_TYPES,
        wp_model="poisson",
    ),
)

SPORTS = {s.key: s for s in (NCAAF, NHL)}


def get_sport(name: str | None) -> Sport:
    """`SPORT` env value -> profile; unset or empty is ncaaf. An unknown name raises, so a typo cannot run as ncaaf."""
    key = (name or "ncaaf").strip().lower() or "ncaaf"
    if key not in SPORTS:
        raise ValueError(f"unknown SPORT {name!r}: expected one of {', '.join(SPORTS)}")
    return SPORTS[key]


def scoreboard_dates(sport: Sport, now: datetime, tz: ZoneInfo) -> list[str]:
    """ESPN `dates` values to fetch this cycle, one GET each: today's Eastern day, plus the previous `days_back` days while
    it is before `scoreboard_carry_until_h` Eastern (NHL games that started at 22:00 run past midnight). ESPN rejects a
    range (`YYYYMMDD-YYYYMMDD` → HTTP 400 on the NHL scoreboard, measured 2026-11-03), so each day is its own call."""
    today = now.astimezone(tz)
    days = [today.strftime("%Y%m%d")]
    if sport.scoreboard_days_back > 0 and today.hour < sport.scoreboard_carry_until_h:
        days = [(today - timedelta(days=n)).strftime("%Y%m%d") for n in range(sport.scoreboard_days_back, 0, -1)] + days
    return days
