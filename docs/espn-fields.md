# ESPN fields whiparound reads

ESPN's site API is public, needs no key, and is **undocumented**. Nothing here is a contract: ESPN can rename a field,
drop one, or change what a status id means without notice. This file lists exactly what the parser takes
(`src/whiparound/espn.py`, `src/whiparound/standings.py`) and what each value becomes, so a change on ESPN's side can be
traced to one line. Anything not listed is ignored. Sample responses the tests parse are in `tests/fixtures/`
(`scoreboard.json`, `standings.json`).

Each call is a plain `GET`, no authentication, one connection at a time, connect timeout 5 s, read timeout 10 s.
Errors (network, non-200, not JSON) are logged as `espn_error` and the cycle keeps the previous decision.

## Endpoints

| Sport | Endpoint (host `site.api.espn.com`) | Query | Called |
|---|---|---|---|
| NCAAF scoreboard | `/apis/site/v2/sports/football/college-football/scoreboard` | `groups=80` (FBS), `dates=YYYYMMDD` | every cycle (`CYCLE_S`, 60 s) |
| NCAAF rankings | `/apis/site/v2/sports/football/college-football/rankings` | none | every `RANKINGS_REFRESH_S` (6 h) |
| NHL scoreboard | `/apis/site/v2/sports/hockey/nhl/scoreboard` | `dates=YYYYMMDD` | every cycle |
| NHL standings | `https://site.web.api.espn.com/apis/v2/sports/hockey/nhl/standings` (**a different host**) | none | every `RANKINGS_REFRESH_S` |

`dates` is an Eastern-time day (`TZ_ET`, default `America/New_York`). **One day per GET**: a range
(`YYYYMMDD-YYYYMMDD`) answered HTTP 400 on the NHL scoreboard (measured 2026-11-03). For the NHL, where games that start
late run past midnight, the previous Eastern day is fetched as well, but only before 04:00 ET (`sport.py`,
`scoreboard_dates`); when two days are fetched, a game seen on both keeps the later day's copy.

## Scoreboard: `events[]`

Events missing either a `competitors[].homeAway == "home"` or `"away"` entry are skipped. The first element of
`competitions[]` is the game.

| ESPN field | Becomes (`Game`) | Used for |
|---|---|---|
| `events[].id` | `event_id` | the join key to Teamarr's `event_id` (the docket); everything is keyed by it |
| `events[].name`, `.shortName` | `name`, `short_name` | log lines, Discord text, slate card |
| `events[].date` (ISO 8601) | `date` (UTC; epoch 0 if unparseable) | next-kickoff text on the slate |
| `events[].status` (else `competitions[0].status`) `.type.state` | `state` (`pre` / `in` / `post`; default `pre`) | only `in` games are candidates; `post` is progress 1.0 |
| `status.type.id` | `status_type_id` | compared with the sport's break id: `23` = halftime (NCAAF), `22` = end of period (NHL) |
| `status.period` | `period` | progress, overtime (`period` above the regulation count), lock-in, late-close shield, upset window |
| `status.clock` (seconds, float) | `clock_s` | progress and seconds remaining when ESPN gives none |
| `competitors[].homeAway` | picks the `home` and `away` `Side` | |
| `competitors[].score` | `Side.score` (int, 0 if missing) | margin, "decided", score-change break signal |
| `competitors[].team.id` | `Side.team_id` | the key into the AP rankings / NHL standings |
| `competitors[].team.abbreviation` | `Side.abbrev` | Discord text, slate card |
| `competitors[].curatedRank.current` | `Side.rank` (99 = unranked; `0` also counts as unranked) | ranking fallback when the AP/standings lookup has no entry |
| `competitions[0].situation.lastPlay.probability.homeWinPercentage` | `home_wp` | NCAAF win probability (closeness); absent for the NHL, which uses the tool's own Poisson model |
| `competitions[0].situation.lastPlay.probability.secondsLeft` | `seconds_left` | regulation seconds left, preferred over computing it from period and clock |
| `competitions[0].situation.lastPlay.id` | `last_play_id` | play freshness for the break hop: a change means a new play |
| `competitions[0].situation.lastPlay.type.text` | `last_play_type` | break signals (`End Period`, `End of Half`, `End of Game`, any text containing `Timeout`; NHL: `Period End`, `End of Game`) and the hop's break plays |
| `competitions[0].situation.possession` | `possession` | parsed and kept, not used by any rule |

**Missing win probability.** ESPN sometimes omits `lastPlay.probability` for minutes at a time. For an in-play game with
no value, the last one seen is carried forward (`wp_carried: true` in the log) for up to 15 minutes, and only while the
score and period are unchanged; any change drops it (`carry_wp`).

**Status ids.** Only two ids are interpreted, both as "a scheduled stop where the viewer loses nothing by switching":
`23` for the NCAAF profile and `22` for the NHL profile (`Sport.break_status_id`). Other ids are carried but not read.

## Rankings: `rankings[]` (NCAAF)

| ESPN field | Becomes |
|---|---|
| `rankings[]` entry with `type == "ap"` | the poll used; if absent, the rank map is empty |
| `ranks[].team.id` | key |
| `ranks[].current` (1 to 25 only) | value: the team's AP rank |

A team not in the map falls back to the scoreboard's `curatedRank.current`, then to 99 (unranked).

## Standings: `children[].standings.entries[]` (NHL)

| ESPN field | Becomes |
|---|---|
| `entries[].team.id` | key |
| `entries[].stats[]` where `name == "playoffSeed"` (`value`) | the team's rank, standing in for the AP rank in the tier logic; 0 or missing = 99 |
| `stats[]` `name == "pointsFor"`, summed over all teams | goals for |
| `stats[]` `name == "gamesPlayed"`, summed over all teams | games played |

`goals_per_60` = total `pointsFor` / total `gamesPlayed`: the league's goals per team per 60-minute game, the lambda of the
NHL Poisson model. Nothing played yet: the previous value is kept (initially 3.0). The stats are matched by `name`, not
by position.
