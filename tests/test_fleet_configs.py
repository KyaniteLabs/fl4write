"""Fleet config invariants (PM-3, 2026-09-03 — LEARNINGS #33).

Every *.fl4write.yaml in the repo root is repo law as data (fl4write/config.py).
The loader dedupes configs by repo key and can cycle ONE config per repo — a
second central config naming the same repo is shadowed (it ALERTs every cycle
and never runs). The fleet therefore keeps repo keys unique across ALL central
configs; this test is the CI-visible tripwire for the class that cost ~18
cycles of dedupe ALERTs on six dual-homed org repos (kinocut, Epoch,
Innerscape, checkyourself, devarch-framework, Elixis).
"""

from __future__ import annotations

import ipaddress
import pathlib
import re

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
CONFIGS = sorted(
    p for p in ROOT.glob("*.fl4write.yaml") if not p.name.startswith(".")
)  # hidden .fl4write.yaml = a repo's in-repo law file, not a fleet config


def _repo_key(path: pathlib.Path) -> str:
    data = yaml.safe_load(path.read_text())
    assert isinstance(data, dict), f"{path.name} is not a mapping"
    repo = data.get("repo")
    assert isinstance(repo, str) and "/" in repo, f"{path.name}: repo key missing/malformed"
    return repo


def test_fleet_configs_present() -> None:
    # 44 personal-account fork configs moved to parked-forks/ by the write-
    # containment card (t_c7791d3f) — root glob = 86 at that change. A partial
    # checkout still trips this floor.
    assert len(CONFIGS) >= 80, "fleet checkout expected (central configs live in the repo root)"


def test_every_fleet_config_parses_with_repo_key() -> None:
    for path in CONFIGS:
        _repo_key(path)


def test_repo_keys_are_unique_across_the_fleet() -> None:
    seen: dict[str, pathlib.Path] = {}
    duplicates: list[str] = []
    for path in CONFIGS:
        key = _repo_key(path)
        if key in seen:
            duplicates.append(f"{seen[key].name} and {path.name} both name {key}")
        seen[key] = path
    assert not duplicates, (
        "duplicate repo keys — the loader cycles one config only, the rest ALERT every "
        "cycle and never run: " + "; ".join(duplicates)
    )


_IPV4 = re.compile(r"(?<![\d.])(\d{1,3}(?:\.\d{1,3}){3})(?![\d.])")


def _api_base_values(node: object, where: str = ""):
    """Yield (location, value) for every api_base key in a parsed YAML doc.

    Walks nested mappings and lists so flow style, nesting, and comments are
    all covered — the value the loader would see is the parsed scalar, never
    the raw line (a trailing `# comment` or quoting made the round-1 raw-line
    regex skip the scalar entirely; security review, PR #26).
    """
    if isinstance(node, dict):
        for key, value in node.items():
            loc = f"{where}.{key}" if where else str(key)
            if key == "api_base":
                yield loc, value
            else:
                yield from _api_base_values(value, loc)
    elif isinstance(node, list):
        for i, item in enumerate(node):
            yield from _api_base_values(item, f"{where}[{i}]")


def _api_base_ipv4_leaks(text: str, name: str) -> list[str]:
    """Parse config text and list its api_base values that contain an IPv4 literal."""
    data = yaml.safe_load(text)
    assert isinstance(data, dict), f"{name} does not parse as a YAML mapping"
    return [
        f"{name}:{loc}: {value}"
        for loc, value in _api_base_values(data)
        if isinstance(value, str) and _IPV4.search(value)
    ]


def test_no_fleet_config_hardcodes_a_lan_host_address() -> None:
    # The repo is public: LAN (RFC1918) host addresses ride in as ${VAR}
    # placeholders or loopback, never literals (commit 1963133 fixed one
    # config; this pins the whole fleet). Since the FL4WRITE_FORGEJO_API_BASE
    # migration (2026-11-01) the Forgejo api_base is runner-env-supplied too.
    leaks: list[str] = []
    for path in CONFIGS + [ROOT / ".fl4write.yaml"]:
        for lineno, line in enumerate(path.read_text().splitlines(), 1):
            for literal in _IPV4.findall(line):
                try:
                    ip = ipaddress.ip_address(literal)
                except ValueError:
                    continue
                if ip.is_private and not ip.is_loopback:  # RFC1918 & co.; not CGNAT
                    leaks.append(f"{path.name}:{lineno}: {literal}")
    assert not leaks, "LAN host addresses in tracked configs: " + "; ".join(leaks)
    # api_base admits NO IPv4 literal at all — hostnames and ${VAR}
    # placeholders only. The check inspects PARSED scalar values, never raw
    # lines: an inline `# comment` or quoting made the round-1 line regex
    # skip the scalar entirely (security review, PR #26). Structural regex,
    # never ipaddress.is_private: the CGNAT answer is Python-version-sensitive,
    # and any host literal in api_base is topology exposure regardless of its
    # address class.
    api_base_leaks: list[str] = []
    for path in CONFIGS + [ROOT / ".fl4write.yaml"]:
        api_base_leaks += _api_base_ipv4_leaks(path.read_text(), path.name)
    assert not api_base_leaks, (
        "api_base values must be hostnames or ${VAR} placeholders, never IPv4 "
        "literals: " + "; ".join(api_base_leaks)
    )
    # Controls (same parse+walk path as the fleet check): commented, quoted,
    # nested, and flow-style literals must be caught; hostnames and
    # placeholders — commented or quoted — must pass.
    for snippet in (
        "api_base: http://8.8.8.8:3000/api/v1",
        "api_base: http://8.8.8.8:3000/api/v1 # inline comment",
        'api_base: "https://8.8.8.8/api/v1"  # quoted + comment',
        "api_base: 'http://8.8.8.8:3000/api/v1'",
        'forgejo: {"api_base": "https://8.8.8.8/api/v1"}',
        "forges:\n  forgejo:\n    api_base: http://192.0.2.10:3000/api/v1\n",
    ):
        assert _api_base_ipv4_leaks(snippet, "control"), (
            f"api_base IPv4 literal escaped the pin: {snippet!r}"
        )
    for snippet in (
        "api_base: https://api.github.com",
        "api_base: https://git.example.com/api/v1 # hostname + comment",
        'api_base: "https://git.example.com/api/v1"',
        "api_base: ${FL4WRITE_FORGEJO_API_BASE}",
        'api_base: "${FL4WRITE_FORGEJO_API_BASE}"',
    ):
        assert not _api_base_ipv4_leaks(snippet, "control"), (
            f"hostname/placeholder wrongly flagged by the pin: {snippet!r}"
        )

