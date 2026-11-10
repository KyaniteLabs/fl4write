"""Merged-PR sweep state made explicit (fl4write/sweep_lifecycle.py).

Retro's per-PR lifecycle was spread over retro_seen, retro_parked and
retro_defer:<n>:<sha> counters written inline; the post-merge shadow belt
encoded two kinds in one string. These tests pin the named transitions.
"""

from __future__ import annotations

import pytest

from fl4write import sweep_lifecycle as sw
from fl4write.sweep_lifecycle import RetroPhase

SHA = "a" * 40


def test_retro_phase():
    assert sw.retro_phase({}) is RetroPhase.ACTIVE
    assert sw.retro_phase({"retro_complete": "true"}) is RetroPhase.ACTIVE   # only real True
    st: dict = {}
    sw.retro_finish(st)
    assert st == {"retro_complete": True} and sw.retro_phase(st) is RetroPhase.COMPLETE


def test_deferrals_park_on_the_limit_and_reset_the_counter():
    st: dict = {}
    parked: dict = {}
    assert sw.retro_deferred(st, parked, 9, SHA, now=1000) == (1, False)
    assert st == {"retro_defer:9:aaaaaaaaaa": 1}
    assert sw.retro_deferred(st, parked, 9, SHA, now=1000) == (2, False)
    assert sw.retro_deferred(st, parked, 9, SHA, now=1000) == (3, True)
    assert st == {"retro_parked": {"9": 1000 + sw.RETRO_PARK_SECONDS}} and parked == {"9": 87400}


def test_deferral_counters_are_per_head():
    st: dict = {}
    sw.retro_deferred(st, {}, 9, SHA)
    sw.retro_deferred(st, {}, 9, "b" * 40)
    assert st == {"retro_defer:9:aaaaaaaaaa": 1, "retro_defer:9:bbbbbbbbbb": 1}


def test_done_clears_counter_and_park():
    st = {"retro_defer:9:aaaaaaaaaa": 2, "retro_defer:8:aaaaaaaaaa": 1}
    parked = {"9": 5, "8": 7}
    sw.retro_done(st, parked, 9, SHA)
    assert st == {"retro_defer:8:aaaaaaaaaa": 1, "retro_parked": {"8": 7}} and parked == {"8": 7}


def test_park_split_and_load():
    parked = sw.load_parked({"retro_parked": {"1": 100, 2: "200", "x": 5, "3": True, "4": -1}})
    assert parked == {"1": 100, "2": 200}
    assert sw.park_split(parked, now=150) == ({2}, {1})
    assert sw.park_split(parked, now=200) == (set(), {1, 2})        # expiry is inclusive
    assert sw.load_parked({"retro_parked": [1]}) == {} and sw.load_parked({}) == {}


def test_seen_round_trip_normalizes_json_string_keys():
    assert sw.load_seen({"retro_seen": {"3": True, 4: True, "x": True, "-1": True}}) == {3, 4}
    assert sw.load_seen({"retro_seen": None}) == set()
    st: dict = {}
    sw.store_seen(st, {5, 6})
    assert st == {"retro_seen": {5: True, 6: True}}


@pytest.mark.parametrize("entry,hit", [
    (SHA, True), (SHA + ":dep", True), ("b" * 40, False), ("b" * 40 + ":dep", False),
    (None, False), (5, False), (SHA + ":depx", False)])
def test_post_merge_belt(entry, hit):
    assert sw.pm_belt_hit(entry, SHA) is hit


def test_post_merge_belt_entries():
    assert sw.pm_belt_entry(SHA) == SHA
    assert sw.pm_belt_entry(SHA, dependency=True) == SHA + ":dep"
