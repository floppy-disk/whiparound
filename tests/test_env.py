from whiparound.runner import env_credential


def test_new_names_win_and_old_names_still_work(monkeypatch):
    for name in ("CHANGER_DISPATCHARR_USER", "NCAAF_CHANGER_DISPATCHARR_USER"):
        monkeypatch.delenv(name, raising=False)
    assert env_credential("DISPATCHARR_USER") == ""
    monkeypatch.setenv("NCAAF_CHANGER_DISPATCHARR_USER", "old")
    assert env_credential("DISPATCHARR_USER") == "old"
    monkeypatch.setenv("CHANGER_DISPATCHARR_USER", "new")
    assert env_credential("DISPATCHARR_USER") == "new"
    monkeypatch.setenv("CHANGER_DISPATCHARR_USER", "")          # set but empty: the old name is still read
    assert env_credential("DISPATCHARR_USER") == "old"
