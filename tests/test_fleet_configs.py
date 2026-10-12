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
    assert len(CONFIGS) > 100, "fleet checkout expected (central configs live in the repo root)"


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


def test_no_fleet_config_hardcodes_a_lan_host_address() -> None:
    # The repo is public: LAN (RFC1918) host addresses ride in as ${VAR}
    # placeholders or loopback, never literals (commit 1963133 fixed one
    # config; this pins the whole fleet). Tailnet (CGNAT) Forgejo api_base
    # literals are out of scope until the runner supplies them by env.
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

