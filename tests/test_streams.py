from datetime import timedelta

import pytest

from whiparound.streams import StreamMemory


def test_no_memory_returns_the_input_unchanged(t0):
    assert StreamMemory().order("1", [4, 3, 9, 1], t0) == [4, 3, 9, 1]


def test_observe_marks_streams_ahead_dead_and_sets_good(t0):
    m = StreamMemory()
    assert m.observe("1", [1, 2, 3, 4, 5], 4, t0) is True
    assert m.good == {"1": 4} and set(m.dead) == {("1", 1), ("1", 2), ("1", 3)}
    assert m.order("1", [1, 2, 3, 4, 5], t0) == [4, 5, 1, 2, 3]   # good first, others, dead last in original order


def test_playing_the_first_sets_good_without_dead_marks(t0):
    m = StreamMemory()
    assert m.observe("1", [7, 8, 9], 7, t0) is False
    assert m.good == {"1": 7} and m.dead == {}


def test_unknown_playing_id_is_ignored(t0):
    m = StreamMemory()
    assert m.observe("1", [7, 8, 9], 42, t0) is False
    assert m.good == {} and m.dead == {}


def test_order_is_always_a_permutation(t0):
    m = StreamMemory()
    m.observe("1", [1, 2, 3, 4], 3, t0)
    m.observe("2", [1, 2, 3, 4], 4, t0)
    for event, streams in [("1", [4, 3, 2, 1]), ("1", [9, 8]), ("2", [1, 2]), ("1", []), ("3", [5, 1, 6]), ("2", [4, 4, 1])]:
        assert sorted(m.order(event, streams, t0)) == sorted(streams)


def test_good_absent_from_the_list_is_skipped_and_all_dead_keeps_order(t0):
    m = StreamMemory()
    m.observe("2", [1, 2, 3, 4], 4, t0)                      # good 4; dead 1, 2, 3
    assert m.order("2", [3, 1, 2], t0) == [3, 1, 2]          # 4 absent, all dead: original order
    assert m.order("2", [3, 5, 1], t0) == [5, 3, 1]          # 5 is unknown, so it outranks the dead


def test_dead_entries_expire_and_are_forgotten(t0):
    m = StreamMemory(ttl_s=100)
    m.observe("1", [1, 2, 3], 3, t0)
    assert m.order("1", [1, 2, 3, 4], t0 + timedelta(seconds=99)) == [3, 4, 1, 2]
    assert m.order("1", [1, 2, 3, 4], t0 + timedelta(seconds=100)) == [3, 1, 2, 4]   # good stays, dead forgotten
    assert m.dead == {}


def test_a_stream_seen_playing_is_no_longer_dead(t0):
    m = StreamMemory()
    m.observe("1", [1, 2, 3], 3, t0)                          # 1, 2 dead for game 1
    m.observe("1", [3, 1, 2], 1, t0)                          # later 1 plays for game 1: no longer dead there
    assert ("1", 1) not in m.dead and m.good["1"] == 1


def test_dead_is_per_game(t0):
    # A provider's event slot is reused for later games (review 2026-10-24): dead for one game says nothing of another.
    m = StreamMemory()
    m.observe("1", [1, 2, 3], 3, t0)
    assert m.order("2", [1, 2, 3], t0) == [1, 2, 3]
    assert m.order("1", [1, 2, 3], t0) == [3, 1, 2]


def test_learn_reports_only_newly_dead(t0):
    m = StreamMemory()
    assert m.learn("1", [1, 2, 3], 3, t0) == (True, [1, 2])
    assert m.learn("1", [3, 1, 2, 4], 4, t0) == (True, [3])   # 1 and 2 already dead; 3 had been playing, so it is new
    assert m.learn("1", [4, 3, 1, 2], 4, t0) == (False, [])
    assert m.learn("2", [1, 2, 4], 4, t0) == (True, [1, 2])   # another game: its own marks
