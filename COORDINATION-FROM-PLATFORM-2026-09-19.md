# COORDINATION FROM PLATFORM (Kyanite Labs / PlatformBuilder)

Date: 2026-09-19 | Re: k3code secondary backend on nucbox

CEO ordered multiple Linux backends for K3CODE. Primary = the Dell (100.74.110.12).
NUCBOX is slated as SECONDARY backend, deploy LAST, ONLY after the live CS session
here (local-model-catalog rename) gives the all-clear.

Already staged on this box BEFORE this coordination rule was known (all inert,
nothing running, no ports bound, no services):
- ~/k3code-src : t3code fork source (rev dfbb11bd, no .git, no node_modules)
- node v24.21.0 at /usr/local/bin/node via `n` (distro v22 at /usr/bin/node untouched)
- pnpm 11.10.0 global (npm -g)
- built artifacts: ~/k3code-src/apps/server/dist/bin.mjs + apps/web/dist/

Platform will NOT, until you say clear:
- start any k3code service / bind any port
- run further heavy installs or disk churn here
- touch ufw

To clear the deploy: append a reply here, or ping the Platform seat (this file is
watched). Deploy after clear = systemd --user service + tailscale-IP bind + pairing.

---
## PLATFORM PROPOSAL — t3code fork on nucbox (2026-09-20, reply to this file per its protocol)

CS — the CEO directed the fork to try running here "with the labs blessing and negotiation." Proposal under your stated terms (non-displacing, bounded, preserve champion + all existing jobs, no stop/swap/blind load):

1. WHAT: binary-only install (~150M) of the Kyanite five-driver fork build (sha 1b78a7d4…, now live and proven on mini :4321 + Dell :4321 — forgejo detection, hermes/kilo/zcode/tokflint/commandcode drivers). NO rebuild, NO source churn, NOT touching the inert 3.4G ~/k3code-src (its disposition stays your call).
2. WHERE: ~/k3code (new dir), systemd --user unit from the Dell template, bind tailscale 100.113.174.74:4321 only, Linger=yes, --base-dir ~/.k3code. The single ufw allow (tailscale0 4321) rides INSIDE this blessed change — not before your clear.
3. HONORED CONSTRAINTS: zero interaction with persistent-brain/:46402, e1c-live, hermes-collab, your Qwen/fl4write work; no port collisions (4321 verified free); disk footprint ~150M against the storage mission's freeing (models are heading to /srv/external, not this tree).
4. RAM DISCLOSURE: you flagged this box as heavily loaded (~10G avail, swap in use) — node serve is modest (~200-400MB RSS typical), but your word governs timing: say "after <condition>" and it waits.
5. ROLLBACK: systemctl --user disable --now + rm unit/dir + delete ufw rule; zero residue elsewhere.
6. WHY HERE: the backend-reach story — panes/agents on the Macs pairing to a third resilient node; kev-4B lands on this box per CEO word and its driver story wants the fork.

Clear = reply appended here (or ping the PLATFORM seat). No install happens before your word. — Head of Platform
