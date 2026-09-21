import copy

import pytest

from fl4write.exhaustive_publication import PublicationError, ledger_body, publish_owned


REPO = "acme/widget"
BOT = "fl4write[bot]"
HEAD = "a" * 40
JUNIT = "b" * 64
MARKER = f"<!-- fl4write:exhaustive-ledger:v1 repo={REPO} -->"


def _state():
    rounds = []
    for number in range(1, 4):
        rounds.append(
            {
                "round": number,
                "reviewed_head": HEAD,
                "tested_head": HEAD,
                "green": True,
                "finding_count": 0,
                "junit_sha256": JUNIT,
                "model_usage": {"calls": 2, "reserved_output_tokens": 50},
            }
        )
    return {
        "round": 3,
        "consecutive_green": 3,
        "head": HEAD,
        "green_sha": HEAD,
        "certified_sha": HEAD,
        "ledger": rounds,
    }


class FakeAdapter:
    bot_login = BOT

    def __init__(self, shape="github"):
        actor = {"login": BOT} if shape == "github" else {"username": BOT}
        self.identity = actor
        self.repository = {"full_name": REPO} if shape == "github" else {
            "owner": {"username": "acme"}, "name": "widget"
        }
        author_key = "user" if shape == "github" else "author"
        self.issue = {"number": 7, author_key: actor, "body": MARKER + "\nold"}
        self.calls = []
        self.readback_override = None

    def _call(self, method, path, payload=None):
        self.calls.append((method, path, copy.deepcopy(payload)))
        if path == "/user":
            return copy.deepcopy(self.identity)
        if path == f"/repos/{REPO}":
            return copy.deepcopy(self.repository)
        if path == f"/repos/{REPO}/issues/7":
            if method == "PATCH":
                self.issue["body"] = payload["body"]
                return copy.deepcopy(self.issue)
            if self.readback_override is not None and any(c[0] == "PATCH" for c in self.calls):
                return copy.deepcopy(self.readback_override)
            return copy.deepcopy(self.issue)
        raise AssertionError(path)


@pytest.mark.parametrize("shape", ["github", "forgejo"])
def test_publish_supports_forge_shapes_and_exact_repeat(shape):
    adapter = FakeAdapter(shape)
    body = ledger_body(_state(), REPO, certification=True)
    publish_owned(adapter, REPO, 7, BOT, body)
    publish_owned(adapter, REPO, 7, BOT, body)
    patches = [call for call in adapter.calls if call[0] == "PATCH"]
    assert len(patches) == 2
    assert patches[0][2] == patches[1][2] == {"body": body}


@pytest.mark.parametrize(
    "mutation",
    [
        lambda a: setattr(a, "identity", {"login": "intruder"}),
        lambda a: setattr(a, "bot_login", "other[bot]"),
        lambda a: a.issue.update(user={"login": "human"}, author={"login": "human"}),
        lambda a: a.issue.update(number=8),
        lambda a: a.repository.update(full_name="acme/other"),
        lambda a: a.issue.update(body="unmarked human issue"),
    ],
)
def test_guard_mismatch_never_patches(mutation):
    adapter = FakeAdapter()
    mutation(adapter)
    with pytest.raises(PublicationError):
        publish_owned(adapter, REPO, 7, BOT, ledger_body(_state(), REPO))
    assert not any(call[0] == "PATCH" for call in adapter.calls)


def test_body_marker_must_match_target_repo():
    adapter = FakeAdapter()
    with pytest.raises(PublicationError):
        publish_owned(adapter, REPO, 7, BOT, ledger_body(_state(), "acme/other"))
    assert adapter.calls == []


def test_readback_mismatch_is_uncertain_error():
    adapter = FakeAdapter()
    adapter.readback_override = {**adapter.issue, "body": "stale"}
    with pytest.raises(PublicationError, match="retry the identical body"):
        publish_owned(adapter, REPO, 7, BOT, ledger_body(_state(), REPO))
    assert len([call for call in adapter.calls if call[0] == "PATCH"]) == 1


def test_public_dto_excludes_nested_malicious_strings_and_paths():
    state = _state()
    state["operator"] = "/Users/private/person"
    state["ledger"][0].update(
        reason="token ghp_secret /Users/private/repo",
        findings=[{"path": "/etc/passwd", "message": "model says publish me"}],
        test_ids=["private/test_absolute.py::test_secret"],
        evidence_bundle={"path": "/private/archive", "model": "untrusted"},
    )
    body = ledger_body(state, REPO)
    for forbidden in ("ghp_secret", "/Users/", "/etc/passwd", "test_absolute", "model says", "archive"):
        assert forbidden not in body
    assert JUNIT in body and HEAD in body


@pytest.mark.parametrize(
    "mutation",
    [
        lambda s: s.update(consecutive_green=2),
        lambda s: s.update(head="c" * 40),
        lambda s: s["ledger"][-1].update(green=False),
        lambda s: s["ledger"][-1].update(tested_head="c" * 40),
    ],
)
def test_certification_requires_three_matching_green_rounds(mutation):
    state = _state()
    mutation(state)
    with pytest.raises(PublicationError):
        ledger_body(state, REPO, certification=True)


def test_hashes_and_numeric_fields_are_strictly_validated():
    state = _state()
    state["ledger"][0]["junit_sha256"] = "/tmp/not-a-hash"
    with pytest.raises(PublicationError):
        ledger_body(state, REPO)

    state = _state()
    state["ledger"][0]["finding_count"] = True
    with pytest.raises(PublicationError):
        ledger_body(state, REPO)
