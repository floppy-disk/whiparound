# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html). The version in `pyproject.toml` is the single source.

## Unreleased

## 0.1.0 - first public release

First public release. It is 0.x on purpose: the ESPN endpoints it reads are undocumented, and the NHL model is new.

### Added

- NCAAF and NHL profiles from one image, chosen with `SPORT`.
- Win probability from ESPN (college football) or a Poisson/Skellam model fed by the league scoring rate (NHL); a
  margin-based fallback when neither is available.
- Tiers: your listed teams, then ranked teams (AP top 25; NHL playoff seeds), then everything else; a decided game drops to
  the last tier. `TEAM_WEIGHTING=0` turns team weighting off; `USE_RANKS=0` keeps listed teams but ignores ranks and playoff seeds.
- Switching rules: dwell, hysteresis, free switch at halftime, intermission and final, lock-in at the end of a close game,
  late-close shield, and an upset alert (log line and optional Discord post).
- Break detection (ffmpeg black and silence detection armed by ESPN signals) and the break hop to a game that is in play.
- In-place switching through Dispatcharr's `change_stream`, so a viewer's connection survives a switch.
- Stream memory: a stream that failed over is moved to the bottom of the changer channel's list for a while.
- Control API: health, state, pin and unpin, logo, and an off-hours slate stream. No authentication; for the Docker network only.
- One JSON log line per cycle with every input, no URLs and no secrets; optional Discord webhook posts.
- `compose.example.yaml`, example team lists, GitHub Actions for tests and for a multi-arch image on GHCR, MIT licence.
