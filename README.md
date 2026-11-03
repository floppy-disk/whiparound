# whiparound

**whiparound: an auto-switching live sports channel for Dispatcharr (college football, NHL).**

> **Disclaimer.** This is a third-party tool for Dispatcharr, used at your own risk. The Dispatcharr team does not review or
> audit third-party projects and is not liable for them; verify what you run. This tool is not affiliated with or endorsed by
> Dispatcharr. It reads undocumented public ESPN endpoints that may change or go away without notice.

whiparound keeps one Live TV channel in [Dispatcharr](https://github.com/Dispatcharr/Dispatcharr) on the game that matters
most right now. Every minute it asks ESPN which games are live, picks the highest-leverage one that already has a Teamarr
event channel (your listed teams first, then ranked or playoff-position teams, then anyone), and points the changer channel
at that game's streams. If someone is watching, it cuts the running stream in place, so the viewer's connection survives.
There is no LLM anywhere: every decision is a printed formula, and every input is in the log.

It runs as one small container per sport (NCAAF and NHL profiles, same image). **It does nothing unless the stack below is
already in place** (in particular, Teamarr event channels for the games), so read the next section first.

## Before you start: what your stack must already have

| Layer | Dependency | Exactly what is used | Verified how |
|---|---|---|---|
| Runtime | Python 3.12 or newer (`pyproject.toml`); the image is `python:3.12-slim` | `zoneinfo`, `dataclasses`, `http.server` | `pyproject.toml`, `Dockerfile` |
| Runtime | `httpx>=0.27` | one bounded `Client` per service (`http.py`) | code |
| Runtime | `pillow>=10.1` | slate rendering (`slate.py`), logo script (`assets/make_logo.py`) | code |
| Runtime | `tzdata` | `America/New_York` for ESPN's Eastern dates | code |
| Runtime | `ffmpeg` in the image (`apt-get install ffmpeg`) | break detector (`breaks.py`: `blackdetect`, `silencedetect`); slate stream (`slate.py`) | `Dockerfile` |
| Service | **Dispatcharr**, built and measured against **0.32.0**; other versions are unverified | `POST /api/accounts/token/` (SimpleJWT login); `GET` and `PATCH /api/channels/channels/{id}/` (read the pick's event-channel streams; set the changer channel's `streams` list in order); `GET /proxy/ts/status/{uuid}` (is the channel running, who is watching); `POST /proxy/ts/change_stream/{uuid}` with body `{"stream_id": N}` (the in-place cut); `GET /proxy/ts/stream/{uuid}` (ffmpeg reads the changer's own output for break detection). The live proxy is **keyed by channel UUID, not id** | `dispatcharr.py`; measured on the author's instance, 2026-10-17 |
| Service | **Teamarr**, tested against **v2.22.0** (the author's instance, October to November 2026); other versions unverified | `GET /api/v1/channels/managed` returning `{"channels": [...]}` or a bare list; rows need `event_id`, `league` (`college-football` or `nhl`), `dispatcharr_channel_id`, `tvg_id`, `channel_name` or `event_name`, `home_team`, `away_team`, `deleted_at`. **Assumption: Teamarr's `event_id` is ESPN's event id.** The whole docket join rests on it | `docket.py` |
| Service | **ESPN public JSON**: undocumented, no key, no terms of service reviewed | `site.api.espn.com/apis/site/v2/sports/football/college-football/scoreboard?groups=80&dates=YYYYMMDD`; `.../college-football/rankings` (AP poll); `.../hockey/nhl/scoreboard?dates=YYYYMMDD` (one day per request; a date range answers HTTP 400); and a **second host**, `site.web.api.espn.com/apis/v2/sports/hockey/nhl/standings` (NHL playoff seeds and scoring rate). Win probability comes from `situation.lastPlay.probability` (college football only). Field by field: [docs/espn-fields.md](docs/espn-fields.md) | `espn.py`, `standings.py`, `sport.py` |
| Service | Discord webhook (optional) | one `POST` per switch and per upset alert; an empty variable means silent | `discord.py` |
| Your setup | Event channels per game already exist in Dispatcharr, created by Teamarr, with `league` set | the docket filter drops everything else | `docket.py` |
| Your setup | A Dispatcharr user (admin) for the tool; `DRY_RUN=0` refuses to start without one | | `runner.py` `main()` |
| Your setup | The changer channel itself, created by you by hand ([recipe below](#create-the-changer-channel-in-dispatcharr)); its id goes in `CHANGER_CHANNEL_ID` | | |
| Your setup | Docker network reachability: the tool reaches Dispatcharr and Teamarr by hostname; Dispatcharr reaches the tool's port 8787 for `/logo.png` and `/slate.ts`. The tool's API has **no authentication** | | `api.py` |
| Your setup | A listed-teams file: exact Teamarr team names, one per line, order = priority | | `docket.load_listed_teams` |

Checklist before the first start:

- [ ] Dispatcharr (built against 0.32.0; `change_stream` by UUID was measured there, other versions are untested) is reachable
      from the tool's container, and you have an admin user for the tool.
- [ ] Teamarr creates one event channel per game, with `league` `college-football` or `nhl`. **Teamarr's `event_id` must be
      ESPN's event id**; if it is not, no game will match a channel and the log will show `candidates: 0`.
- [ ] The container can reach the internet at `site.api.espn.com` and, for the NHL, `site.web.api.espn.com`. These endpoints
      are undocumented, need no key and may change without notice.
- [ ] The tool, Dispatcharr and Teamarr share a Docker network. Dispatcharr can fetch `http://<tool>:8787/logo.png` and
      `http://<tool>:8787/slate.ts`.
- [ ] The control API has **no authentication**. Never publish port 8787 outside that network.
- [ ] Optional: a Discord webhook for switch and upset posts.

## Quick start

1. Clone this repository, copy the examples, and make the data directory writable by the container's user (uid 1000):

   ```sh
   cp compose.example.yaml compose.yaml
   cp .env.example .env
   cp config/listed-teams.example.txt config/listed-teams.txt
   mkdir -p data/ncaaf && chown 1000:1000 data/ncaaf    # may need sudo
   ```

2. Edit `config/listed-teams.txt` (exact Teamarr names, one per line, order = priority; your own lists are gitignored), put the
   Dispatcharr user in `.env`, and set the network name at the bottom of `compose.yaml` to the one Dispatcharr and Teamarr share.

3. `compose.yaml` (this is `compose.example.yaml`):

   ```yaml
   # Copy to compose.yaml, edit, then:  docker compose config  &&  docker compose up -d --build
   # Start with DRY_RUN "1" (decide and log, never touch Dispatcharr); flip it to "0" once the log looks right.
   services:
     changer-ncaaf:
       build: .                     # or: image: ghcr.io/<owner>/whiparound:0.1.0 once a release image is published
       container_name: changer-ncaaf
       restart: unless-stopped
       user: "1000:1000"            # ./data/ncaaf must be writable by this uid
       environment:
         SPORT: ncaaf
         DRY_RUN: "1"
         CHANGER_CHANNEL_ID: ""     # the Dispatcharr channel you created (README, "Create the changer channel")
         CHANGER_DISPATCHARR_USER: ${CHANGER_DISPATCHARR_USER}
         CHANGER_DISPATCHARR_PASSWORD: ${CHANGER_DISPATCHARR_PASSWORD}
         CHANGER_DISCORD_WEBHOOK: ${CHANGER_DISCORD_WEBHOOK:-}
         DISPATCHARR_URL: http://dispatcharr:9191
         TEAMARR_URL: http://teamarr:9195
       volumes:
         - ./data/ncaaf:/data
         - ./config/listed-teams.txt:/config/listed-teams.txt:ro
       networks: [iptv]             # the network Dispatcharr and Teamarr are on
       # No "ports:" on purpose: the control API (8787) has no authentication. Dispatcharr reaches it over the network.

     # The NHL changer: the same image, its own container, channel, data directory and team list.
     # changer-nhl:
     #   build: .
     #   container_name: changer-nhl
     #   restart: unless-stopped
     #   user: "1000:1000"
     #   environment:
     #     SPORT: nhl
     #     DRY_RUN: "1"
     #     CHANGER_CHANNEL_ID: ""     # a second Dispatcharr channel, not the NCAAF one
     #     CHANGER_DISPATCHARR_USER: ${CHANGER_DISPATCHARR_USER}
     #     CHANGER_DISPATCHARR_PASSWORD: ${CHANGER_DISPATCHARR_PASSWORD}
     #     CHANGER_DISCORD_WEBHOOK: ${CHANGER_DISCORD_WEBHOOK:-}
     #     DISPATCHARR_URL: http://dispatcharr:9191
     #     TEAMARR_URL: http://teamarr:9195
     #   volumes:
     #     - ./data/nhl:/data
     #     - ./config/listed-teams-nhl.txt:/config/listed-teams.txt:ro
     #   networks: [iptv]

   networks:
     iptv:
       external: true               # rename to the existing Docker network Dispatcharr and Teamarr share
   ```

4. Start in **dry-run** (`DRY_RUN: "1"`, as in the example), which decides and logs but never calls Dispatcharr:

   ```sh
   docker compose config          # check it renders
   docker compose up -d --build
   docker compose logs -f changer-ncaaf
   ```

   Read the `cycle` lines in the log (also written to `./data/ncaaf/cfb-changer.jsonl`): `live` is how many games ESPN says are
   on, `candidates` how many of them have a Teamarr channel, and `pick` and `alternatives` show what it would choose and why. A
   `docket` line with `channels: 0` means Teamarr's rows did not match (league name, or no `dispatcharr_channel_id`).
5. [Create the changer channel](#create-the-changer-channel-in-dispatcharr), put its id in `CHANGER_CHANNEL_ID`, set
   `DRY_RUN: "0"`, and `docker compose up -d` again. The first real switch is logged as a `switch` line with `applied: true`.

## Create the changer channel in Dispatcharr

The tool does not create the channel; you do, once. It only ever replaces that channel's list of streams.

**In the UI:** add a channel in its own group, with a number and a `tvg_id`, enabled in the profile your player uses. Set its
logo URL to `http://<tool>:8787/logo.png` (`<tool>` = the container name, e.g. `changer-ncaaf`). **No streams are needed**: the
tool sets them. Note the channel's id for `CHANGER_CHANNEL_ID`. The tool fetches the channel's `uuid` itself.

**Through the API** (what the author did on 0.32.0):

```sh
# 1. log in once and keep the "access" token (Dispatcharr answered HTTP 429 after about 6 logins in 10 minutes)
curl -s -X POST http://dispatcharr:9191/api/accounts/token/ -H 'Content-Type: application/json' \
     -d '{"username": "...", "password": "..."}'
# 2. the logo: the reply has an "id"
curl -s -X POST http://dispatcharr:9191/api/channels/logos/ -H "Authorization: Bearer $TOKEN" \
     -H 'Content-Type: application/json' -d '{"name": "NCAAF Changer", "url": "http://changer-ncaaf:8787/logo.png"}'
# 3. the channel: the "id" in the reply is CHANGER_CHANNEL_ID
curl -s -X POST http://dispatcharr:9191/api/channels/channels/ -H "Authorization: Bearer $TOKEN" \
     -H 'Content-Type: application/json' \
     -d '{"name": "NCAAF Changer", "channel_number": 900, "channel_group_id": 1, "tvg_id": "ncaaf-changer",
          "logo_id": 1, "streams": [], "user_level": 0}'
```

`channel_number`, `channel_group_id` and `logo_id` above are placeholders; use your own. New channels landed in every profile on
0.32.0 (observed).

Two settings worth knowing:

- Set Dispatcharr's `channel_shutdown_delay` small, so that a fresh tune-in starts on stream #1 of the list the tool set
  (observed 2026-10-17).
- Optional slate: add a stream with URL `http://<tool>:8787/slate.ts` to the channel as a fallback for when no game is on.
  **Untested**: the author never wired it into a live channel. See **Slate** below.

## Environment

| Var | Default | Meaning |
|---|---|---|
| `USE_RANKS` | `1` | `0`: the AP Top 25 (NCAAF) / playoff spots (NHL) count for nothing: no "ranked" tier, no both-ranked bonus, no upset alert. Listed teams still apply. `TEAM_WEIGHTING=0` implies it |
| `DRY_RUN` | `1` | `1`: decide and log only, never call Dispatcharr. `0`: switch for real |
| `SPORT` | `ncaaf` | `ncaaf` or `nhl`: the sport profile (`sport.py`). Anything else exits with status 2 |
| `TEAM_WEIGHTING` | `1` | `0` turns team weighting off, see **Two sports** |
| `CHANGER_CHANNEL_ID` | unset | Dispatcharr id of the changer channel. Unset: nothing is switched |
| `CHANGER_DISPATCHARR_USER` / `CHANGER_DISPATCHARR_PASSWORD` | | Dispatcharr admin user for the tool (required when `DRY_RUN=0`). **Deprecated fallback:** `NCAAF_CHANGER_DISPATCHARR_USER` / `_PASSWORD` are read when the `CHANGER_` names are unset |
| `CHANGER_DISCORD_WEBHOOK` | empty | One post per switch and per upset alert. Empty: no posts. **Deprecated fallback:** `NCAAF_CHANGER_DISCORD_WEBHOOK` |
| `DISPATCHARR_URL` | `http://dispatcharr:9191` | Where the tool reaches Dispatcharr |
| `TEAMARR_URL` | `http://teamarr:9195` | Your Teamarr's host and port. `9195` is the port the author's Teamarr listens on; Teamarr's own default is unverified, so set this |
| `ESPN_BASE` | `https://site.api.espn.com` | |
| `LISTED_TEAMS_FILE` | `/config/listed-teams.txt` | Exact Teamarr names, one per line, order = priority |
| `LOG_PATH` | `/data/<sport log_name>` (`cfb-changer.jsonl` for `ncaaf`, `nhl-changer.jsonl` for `nhl`) | JSON lines, also echoed to stdout; empty string = stdout only |
| `LOGO_PATH` | `/app/<sport logo_file>` (`assets/logo.png`, `assets/logo-nhl.png`) | The file `GET /logo.png` serves |
| `SLATE_PATH` | `/data/slate.png` | Where the slate still is rendered |
| `CYCLE_S` / `DELAY_S` | `60` / `45` | Cycle length; how old the scoreboard snapshot is that decisions run on, to match the video's lag behind the data |
| `IDLE_CYCLE_S` | `120` | Sleep between cycles when the last cycle had no candidates (no live game with a channel); `CYCLE_S` otherwise. A `cadence` log line is written when it flips |
| `STREAM_MEMORY_S` | `21600` | How long a stream that a failover skipped stays at the bottom of the changer channel's list. The stream Dispatcharr last settled on for a game stays first. In-process: a restart forgets |
| `DOCKET_REFRESH_S` / `RANKINGS_REFRESH_S` | `600` / `21600` | Teamarr refresh; `RANKINGS_REFRESH_S` is the AP poll (NCAAF) or the standings (NHL) refresh |
| `FFMPEG_BIN` | `ffmpeg` | ffmpeg binary, used by the break detector (`breaks.py`) and the slate stream (`slate.py`) |
| `TZ_ET` | `America/New_York` | ESPN's `dates` are Eastern days |
| `API_BIND` / `API_PORT` | `0.0.0.0` / `8787` | The control API (`api.py`). No authentication: reach it only over the Docker network |

Log events (`start` names the `sport`; `rankings` carries `source: "ap" | "standings"`; every `cycle` carries `team_weighting`):
`start`, `docket`, `rankings`, `cycle`, `switch`, `upset_alert`, `stream_learned`, `cadence`, `pin`, `unpin`, `api_start`,
`api_request`, the break-detection events (`break_signal`, `video_event`, `detector_*`), and the errors `espn_error`,
`docket_error`, `dispatcharr_error`, `discord_error`, `config_error`, `cycle_error`, `api_error`, plus `stop`. No URLs and no
secrets by design (`redact()` on every line; clients never return URL fields).

## How it decides

Each cycle (60 s):

1. ESPN scoreboard (today in New York time) gives each game's score, clock, period, win probability and rank.
2. Teamarr's managed channels (refreshed every 10 min) give the **docket**: ESPN event id to Dispatcharr event channel. A game
   with no channel is never a candidate.
3. Every live game that has a channel is scored: **tier** (listed, ranked, other; a *decided* game drops to other), then
   **leverage** inside the tier.
4. The decision runs on a snapshot `DELAY_S` old. Rules in order: a pin overrides everything; when the current game has ended
   or is at its scheduled stop (halftime; the NHL's intermissions) and is not the top pick, switch freely; lock-in; break hop
   (below); dwell; then hysteresis.
5. One `cycle` line is logged. On a switch, unless `DRY_RUN=1`: set the changer channel's streams to the pick's event-channel
   streams (a stream that failed over recently goes last), and if the channel is running, `POST /proxy/ts/change_stream` so the
   viewer's connection survives. Post to Discord.

**The formula.** All numbers are fields of `Params` in `leverage.py`; the NHL overrides some (see **Two sports**).

- **Tier:** a listed team (your file) beats ranked (either side in the AP top 25; NHL: both sides in a playoff spot), which
  beats everything else. A decided game is tier "other".
- **Leverage** (within a tier): `closeness * (0.4 + 0.6 * progress)`, plus 0.15 when both sides are ranked, 0.25 in overtime,
  and 0.20 for an upset in progress. `progress` is the fraction of regulation played. `closeness = 1 - 2 * |wp - 0.5|`, where
  `wp` is the leader's win probability: ESPN's figure (NCAAF), else the tool's Poisson model (NHL), else a margin-based
  fallback `1 - margin / (8 + 24 * progress)`.
- **Decided:** win probability at least 0.97 (NHL 0.99), or a lead of 25 points or more, or 17 or more with under 10 minutes
  left (NHL: 4 goals or more, or 3 or more with under 10 minutes; a one-goal NHL game is never decided, because of the pulled
  goalie).
- **Late-close shield:** a listed or ranked game in the last period or overtime, within one score, outranks every other listed or
  ranked game, even one that just started. An unranked game is never shielded.
- **Dwell** 10 minutes on a game before leaving it for a better one; **hysteresis**: a challenger in the same tier must beat the
  current game's leverage by 0.15 (a better tier needs no margin but still waits out the dwell); **lock-in** for the last 3 minutes of regulation when within one
  score (NCAAF 8 points, NHL 1 goal).
- **Upset alert.** A live, undecided game in the 3rd quarter or later (NHL: 3rd period) where the favourite by rank (ranked 25
  or better, the other side unranked or at least 10 spots worse, AP poll first, scoreboard rank second; NHL: seed 8 or better,
  gap 6) is strictly trailing. `leverage` gains the upset bonus, and the first cycle it is true writes one `upset_alert` log
  line and posts to Discord; one alert per game per process.

Every `cycle` log line prints the inputs (`pick.inputs`: margin, period, clock, win probability and its source, ranks).

## Control API

`ChangerApi` (stdlib `ThreadingHTTPServer`, **no authentication**, Docker network only). One `api_request` log line per request
(method, path, status, milliseconds; never the body). The image has no `curl`; run these from any container on the same network.

| Endpoint | Does |
|---|---|
| `GET /healthz` | `{"ok": true}` |
| `GET /state` | the runner's state as JSON (current game, pin, last cycle) |
| `POST /pin` | body `{"event_id": "4018...", "minutes": 30}` (1 to 1440); 204, or 400 on a bad body or an event not on the docket |
| `DELETE /pin` | clear the pin; 204 |
| `GET /logo.png` | the channel logo, `Cache-Control: public, max-age=86400` |
| `GET /slate.ts` | the slate as a live MPEG-TS stream until the client disconnects; 404 if no slate is configured |

```sh
curl -s http://changer-ncaaf:8787/state
curl -s -X POST http://changer-ncaaf:8787/pin -H 'Content-Type: application/json' \
     -d '{"event_id": "401856724", "minutes": 30}'      # event ids are in the cycle log's "pick" and "alternatives"
curl -s -X DELETE http://changer-ncaaf:8787/pin
```

## Two sports

One image, two containers. `sport.py` holds a frozen `Sport` per profile; `SPORT` picks it (`ncaaf` is the default). Every
sport-specific number is a `Sport` field or a field of `Sport.params`; the pure leverage functions read the period arithmetic from
`game.rules`, which the scoreboard parser fills from the profile.

| Field | `ncaaf` | `nhl` |
|---|---|---|
| `title` (slate), `channel_word` (logo) | `NCAAF CHANGER`, `NCAAF` | `NHL CHANGER`, `NHL` |
| `scoreboard_path`, `scoreboard_params` | `/apis/site/v2/sports/football/college-football/scoreboard`, `{"groups": "80"}` | `/apis/site/v2/sports/hockey/nhl/scoreboard`, `{}` |
| `scoreboard_days_back` | 0 (`dates=YYYYMMDD`, today ET) | 1: the previous Eastern day is fetched too, in its own GET, but only before 04:00 ET (games that start late run past midnight). ESPN rejects a date range (HTTP 400) |
| `rankings_path` | `/apis/site/v2/sports/football/college-football/rankings` (AP) | none |
| `standings_url` | none | `https://site.web.api.espn.com/apis/v2/sports/hockey/nhl/standings` (own client, 1 connection, connect 5 s, read 10 s) |
| `teamarr_league` | `college-football` | `nhl` |
| `period_s`, `regulation_periods` | 900, 4 | 1200, 3 |
| `break_status_id`, `break_midgame` | `23` (halftime, once) | `22` (end of period, intermission, after every period) |
| `period_label` | `Q` | `P` |
| `end_plays` | `End Period`, `End of Half`, `End of Game` | `Period End`, `End of Game` (play names sampled live 2026-11-02) |
| `hop_break_types` | `Timeout`, `End Period`, `End of Half`, `End of Game` | `Stoppage`, `Period End`, `End of Game` |
| `log_name`, `logo_file` | `cfb-changer.jsonl`, `assets/logo.png` | `nhl-changer.jsonl`, `assets/logo-nhl.png` |
| `Params` overrides | none | `decided_wp` 0.99, `decided_min_margin` 2, `decided_margin_any` 4, `decided_margin_late` 3, `decided_late_s` 600, `lock_in_margin` 1, `shield_period` 3, `shield_margin` 1, `upset_period` 3, `upset_rank_gap` 6, `top_rank` 8, `both_ranked_rank` 4, `ranked_needs_both`, `hop_after_by_signal` `{score: 45, halftime: 0}`, `wp_model` `poisson` |

**NHL win probability (Poisson).** ESPN publishes none for hockey (`home_wp` is always absent), so with `wp_model="poisson"` and no
ESPN figure `closeness` comes from `hockey_leader_wp(margin, seconds_left, lam_per_60)` in `leverage.py`. Each side expects
`mu = lam * seconds_left / 3600` more goals in regulation. `D` = trailer's goals minus leader's goals is Skellam(mu, mu):
`P(D = k) = sum_j Poisson(j + k; mu) * Poisson(j; mu)` (j cut at 20). The leader's win probability is
`P(D < margin) + 0.5 * P(D == margin)`: a tie goes to overtime, a coin flip. A tied game is 0.5; overtime and shootout are 0.5
too (closeness 1, the overtime bonus does the rest). `seconds_left` is `(3 - period) * 1200 + clock`; at an intermission the clock
reads 0 and the period is the one just played. One goal up with 20 minutes left is 0.762. This model is the tool's own and has not
been checked against outcomes. `Scored.inputs` carries `home_wp` (the home side's) and `wp_source` (`poisson`, `espn`, or null).

**Standings.** Every `RANKINGS_REFRESH_S` the NHL changer GETs the standings: `ranks` = each team's `playoffSeed` (missing teams
count as 99) and `Params.goals_per_60` = sum of `pointsFor` / sum of `gamesPlayed` (a team plays one 60-minute game per game; the
previous value stays when nothing has been played, default 3.0). Tier RANKED needs both sides at seed `top_rank` (8) or better
(`ranked_needs_both`; NCAAF: either side). The both-ranked bonus needs both at `both_ranked_rank` (4). The `rankings` log line
carries `source`, `ranked` (teams with seed at most `top_rank`) and `goals_per_60`.

**`TEAM_WEIGHTING`** (both sports, `1` default, `0` off). Off: `listed_index` is `len(listed)` for every channel and every team's
rank reads 99, so every game is tier "other" with no both-ranked bonus and no upset alert, and the late-close shield protects any
close, undecided late game (not only listed or ranked ones). The standings are still fetched, for lambda. Every `cycle` line
carries `team_weighting`.

## Break detection

`breaks.py` finds commercial breaks on the live stream without ML. `VideoBreakDetector` runs ffmpeg
(`blackdetect=d=0.08:pix_th=0.10`, `silencedetect=n=-45dB:d=0.25`) on the stream in a daemon thread, converts stream time to wall
clock, and keeps 15 minutes of black/silence events; it restarts with 5 to 60 s backoff if ffmpeg exits. It attaches to the
changer channel's own output only while a viewer is watching, so it never keeps the channel running by itself.
`break_state(events, now, BreakParams)` is the pure rule: in a break when a black of at least 1.0 s ended within the last 45 s, or
when at least 2 events in the last 75 s have consecutive start gaps of 20 to 40 s (ad boundaries are about 30 s apart); the break
ends after 60 s with no event. ESPN side: `is_break_signal(prev, cur)` names a score change, period change, halftime, timeout or
end-of-period play, and `ArmedWindow` remembers it for 120 s; a signal arms the video detector and never declares a break itself.
The detector logs `detector_start`, `detector_exit`, `detector_restart`; its `in_break` feeds the break hop below. The stream URL
and ffmpeg's stderr are never logged, only counts. These thresholds were tuned on one provider's feeds in October 2026.

## Break hop

When the game on screen goes to commercial, the tool hops to the best game that is in play, same tier or better (or one with an
upset alert); when the break ends it re-ranks normally (no forced return). Deterministic, no ML.

- **Play freshness.** `Runner.last_play_seen` keeps, per live game, ESPN's `situation.lastPlay.id` and the snapshot time it last
  changed; `play_age_s()` is the seconds since. A game is **in play** when its last play changed within `hop_in_play_s` (90) and that
  play is not a break play (`hop_break_types`).
- **In a break** is judged for the game on screen only: the video detector says `in_break`, or an ESPN break signal is armed and
  that game has had no play change for `hop_after_s` (30; per signal: after a score 75, or 45 for the NHL; after halftime 0). Other
  games are never "in a break": a play gap alone is not one.
- **The rule** (`decider.py`, after halftime/final/lock-in, before dwell): current game in a break and at least
  `hop_min_interval_s` (180) since the last hop, then take the first ranked game of the same tier or better that is in play
  (reason `hop`). For `hop_min_stay_s` (90) after a hop the tool stays (`hop_stay`). After that, while on a hop destination,
  dwell and hysteresis do not apply: the best in-play game ranked above the current one is taken (`hop_rerank`; taking the top
  pick ends the hop); otherwise it holds (`hop_hold`). A destination that goes to a break hops again, subject to the interval.
  Any ordinary switch ends the hop.
- The `cycle` log line carries `hop: {current_in_break, in_play (top 5), hopped_from}`. Params: `hop_in_play_s`, `hop_after_s`,
  `hop_after_by_signal`, `hop_min_interval_s`, `hop_min_stay_s`, `hop_break_types` (`PlayState` is the per-game input to
  `Decider.step`).

## Slate

Off-hours still card for the changer channel when no game is on. `render_slate(lines, path)` draws the sport's title ("NCAAF CHANGER")
and up to three grey lines on navy (#0b1a33) with Pillow's built-in font; the same lines give the same bytes.
`next_kickoff_lines(games, now, tz)` makes the text ("Next kickoff Sat 12:00 ET" plus the game's short name, or "none scheduled
today"). `SlateSource` runs one `ffmpeg` per `/slate.ts` viewer (`-re`, libx264 stillimage 30 fps, silent AAC, MPEG-TS on stdout); on
a write error or disconnect it terminates the process (kill after 3 s) and reaps it. The Docker image has `ffmpeg`. The stream is
tested against a local ffmpeg; it has **not been tested as a fallback stream in a live Dispatcharr channel**.

## Limitations

- One changer channel per container: run two containers for two sports.
- No persistence across restarts: the current pick, dwell timers, pins and stream memory are forgotten.
- ESPN only, and only its undocumented endpoints; there is no fallback if they change. Whether ESPN tolerates one more client
  polling every 20 to 60 seconds is unknown (no terms of service were reviewed); treat it as a risk.
- The NHL win-probability model is the tool's own Poisson model and has not been checked against outcomes.
- Break detection is a heuristic tuned on one provider's feeds in October 2026. Expect false hops, and missed breaks on feeds
  without black frames or silence between ads.
- The in-place cut is a raw splice. A client that plays the stream directly (a TV app) shows about 5 seconds of frozen
  video, resyncs audio first, then recovers (measured once). **A browser does not recover**: Emby's web player remuxes the
  stream into HLS with the video copied as-is, and at the splice its segmenter stops emitting segments until the player gives
  up and reloads, about 80 seconds of black (measured 2026-11-03 in Firefox via Emby Web). Watch the changer channel on a
  direct-play client, or expect to reload after each switch in a browser. The fix is for the tool to produce its own
  continuous output with regenerated timestamps; not built.
- While the changer channel is watched it uses one provider connection. There is no awareness of a provider's connection cap.
- Day boundaries are US Eastern.
- The control API has no authentication.
- Tested against Dispatcharr 0.32.0 only. Teamarr's version and the shape of `/api/v1/channels/managed` are verified only
  against the author's instance.

## Develop and test

```sh
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest -q -W error  # warnings are errors; leaked sockets/files fail the suite
.venv/bin/python assets/make_logo.py    # regenerate assets/logo.png; add --sport nhl for assets/logo-nhl.png
DRY_RUN=1 LOG_PATH= LISTED_TEAMS_FILE=config/listed-teams.example.txt TEAMARR_URL=http://localhost:9195 \
    .venv/bin/python -m whiparound
```

Tests use recorded ESPN responses in `tests/fixtures/` and sockets bound to `127.0.0.1` on ephemeral ports. One slate test needs
`ffmpeg` and is skipped when it is absent. `dispatcharr.TOKEN_PATH` (`/api/accounts/token/`) is Django SimpleJWT's default; confirm it
against your Dispatcharr's `urls.py` before the first real switch.

## Contributing

Issues and pull requests are welcome on GitHub. A change must keep `pytest -q -W error` green. Bug reports are most useful with the
`cycle` log lines around the problem: they carry every input and, by design, no URLs or secrets.

## Releasing

1. Set `version` in `pyproject.toml` (SemVer; the single source of the version).
2. In `CHANGELOG.md`, move the `Unreleased` entries under a new heading `X.Y.Z - YYYY-MM-DD`.
3. Commit, then tag: `git tag vX.Y.Z`.
4. Push the commit and the tag: `git push origin main vX.Y.Z`.
5. The `release` workflow builds the multi-arch image and pushes it to `ghcr.io/<owner>/whiparound` as `X.Y.Z`, `X.Y` and `latest`.

## Credits

Inspired by the Game Changer feature of mlbserver. Built for Dispatcharr and Teamarr, which are separate projects with their own
authors; this tool is not affiliated with either.

## Licence

[MIT](LICENSE). Copyright (c) 2026 Joe Drugan.
