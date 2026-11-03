from whiparound.docket import build_docket, load_listed_teams


def row(**kw):
    base = {"event_id": "401856724", "tvg_id": "teamarr-event-401856724", "league": "college-football",
            "sport": "football", "home_team": "Florida Gators", "away_team": "Texas Longhorns",
            "channel_name": "Texas Longhorns vs Florida Gators", "dispatcharr_channel_id": 1001,
            "deleted_at": None, "logo_url": "http://x/logo.png", "event_provider": "espn"}
    base.update(kw)
    return base


def test_build_docket_filters_and_prefers_english():
    rows = [
        row(tvg_id="teamarr-event-401856724-spanish", dispatcharr_channel_id=1002),
        row(),
        row(event_id="1", tvg_id="teamarr-event-1", league="nhl"),
        row(event_id="2", tvg_id="teamarr-event-2", deleted_at="2026-10-17T10:00:00Z"),
        row(event_id="3", tvg_id="teamarr-event-3", dispatcharr_channel_id=None),
        row(event_id="4", tvg_id="teamarr-event-4-spanish", dispatcharr_channel_id=99),
    ]
    docket = build_docket(rows)
    assert set(docket) == {"401856724", "4"}
    ch = docket["401856724"]
    assert ch.dispatcharr_channel_id == 1001 and ch.tvg_id == "teamarr-event-401856724"
    assert ch.home_team == "Florida Gators" and ch.away_team == "Texas Longhorns"
    assert docket["4"].dispatcharr_channel_id == 99  # Spanish-only is still a channel
    # the order of arrival does not matter
    docket2 = build_docket(rows[1::-1])
    assert docket2["401856724"].dispatcharr_channel_id == 1001
    assert not hasattr(ch, "logo_url")


def test_load_listed_teams():
    text = "# comment\nNotre Dame Fighting Irish\n\n  Pittsburgh Panthers \n"
    assert load_listed_teams(text) == ["Notre Dame Fighting Irish", "Pittsburgh Panthers"]
