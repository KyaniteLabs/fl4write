# Docs map

Where things live. Living law and runbooks stay at the repo root, because
code, tests and desk tooling link to them by those paths.

| Where | What |
|---|---|
| `../README.md` | What the bot is, safety architecture, current status |
| `../PILOT.md` | Fleet rollout, host and cutover notes |
| `../LEARNINGS.md` | Curated lessons, numbered (cited across docs as LEARNINGS #N) |
| `../BUG-SMELL-REGISTRY.md` | Raw bug/smell feed, one line per notice |
| `../EXHAUSTIVE-BUG-RESOLUTION.md` | Exhaustive-loop spec |
| `../ISSUES-BEHAVIOR.md` | Issues-lane behavior contract |
| `../PM3-HANDOFF.md` | PM-3 handoff: runner, hosts, traps |
| `../FL4WRITE-PM-DESK.md`, `../P1-LOOP.md` | Active desk logs (appended in place) |
| `EXHAUSTIVE-USAGE.md` | How to run the exhaustive loop |
| `pm-recovery/` | Audit ledger, round checkpoints, review evidence |
| `reliability-report-baseline-2026-09-17.json` | Reliability baseline report |
| `desk/` | Dated desk memos: CEO gate menu, platform coordination |
| `brand/` | Design system and avatar prompt |

Docs use placeholders for hosts (`<runner-host>`, `<forgejo-host>`). Configs
carry no LAN addresses (loopback or `FL4WRITE_MODEL_ENDPOINT` instead); the
Forgejo configs' `api_base` still names the tailnet-direct Forgejo API.
