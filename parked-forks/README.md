# parked-forks — personal-account fork configs, out of the scheduler

These 44 `*.fork.fl4write.yaml` configs target forks under the personal
account `simongonzalezdc`, not `KyaniteLabs`. Card `t_c7791d3f` (Product
decision, 2026-11-11) makes FL4WRITE write only to KyaniteLabs: every mutating
request to a `simongonzalezdc/*` GitHub repo is refused by
`fl4write/writeguard.py` before a token is minted or a socket is opened.

They live in this subdirectory because `run-cycle.sh` plans with
`*.fl4write.yaml` — a root-only shell glob, so nothing here can be scheduled.
Do not move them back to the repo root, and do not point them at the org: a
fork's PRs are upstream contributions that Simon makes with his own account.

To review one again, the policy has to change first (the allowlist in
`fl4write/writeguard.py` is a protected path — card E2 forbids auto-merge of
changes to it), then the file can return to the root.
