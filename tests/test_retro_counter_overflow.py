"""Malformed persisted counters must not prevent otherwise valid state loading."""
import json

import pytest

from fl4write import state


@pytest.mark.parametrize("literal", ["1e309", "-1e309"])
def test_overflow_counter_reconciles_without_losing_review_memory(tmp_path, literal):
    path = tmp_path / "state.json"
    path.write_text(
        '{"version":1,"prs":{"7":{"last_reviewed_sha":"abc"}},'
        '"retro_defer:1:aaaaaaaaaa":' + literal + ','
        '"retro_defer:2:bbbbbbbbbb":3,"retro_defer:3:cccccccccc":"4"}'
    )
    loaded = state.load_state(path)
    assert "retro_defer:1:aaaaaaaaaa" not in loaded
    assert loaded["prs"] == {"7": {"last_reviewed_sha": "abc"}}
    assert loaded["retro_defer:2:bbbbbbbbbb"] == 3
    assert loaded["retro_defer:3:cccccccccc"] == 4
    state.save_state(path, loaded)
    assert state.load_state(path) == loaded == json.loads(path.read_text())
