# Exhaustive loop review candidate

**Unmerged; final review and three clean product rounds remain open.** One real
automatic repair→PR→CI→merge→refresh lifecycle passed on the integration branch;
see [round26 evidence](pm-recovery/ROUND26-REPAIR-CHECKPOINT.md). The candidate
is excluded from main and the fleet runner. Historical draft findings and their
repairs are retained under `docs/pm-recovery`; those old reports do not describe
the current runtime's capabilities.

The loop reviews archived tracked text in a fresh subprocess, validates grounded
findings, tests an immutable Docker snapshot, and seals evidence. Three consecutive
zero-finding rounds with a completely green configured suite are required.
Skipped, missing or failing tests cannot count toward certification. An existing
bot-owned ledger issue is required for public certification; issue creation is
not implemented. Publication validates ownership and records a replayable transaction.

Example invocation after installing the [test runtime](../tools/exhaustive-runtime/README.md):

```bash
python3 -m fl4write.exhaustive \
  --repo /absolute/path/to/repo \
  --state-dir /durable/private/fl4write-state \
  --config /absolute/path/to/repo/.fl4write.yaml \
  --test-image sha256:IMMUTABLE_INSTALLED_IMAGE_ID \
  --test-command 'python3 -m pytest -q --junitxml {junit}' \
  --max-model-calls 64 --max-output-tokens 100000
```

A configuration loaded from inside the reviewed tree may not choose where the
operator's environment credential is sent. A route whose `key_env` reads a host
secret is admitted only when its `endpoint` is loopback, or when its host is
named by the operator out-of-band:

```bash
FL4WRITE_TRUSTED_MODEL_ENDPOINTS=api.example.com,router.example.com:8443
```

The list is comma- or space-separated `host` (any port) or `host:port` entries.
A repo-supplied route aimed anywhere else defers with an escalation naming the
endpoint, and no credential leaves the host. Set this variable in the loop's
own environment, never in the reviewed repository.

Use `--live-model-tests` when the configured suite needs the live model evaluation
transport. It requires Docker and a runtime image declaring model-proxy support.
The selected model credential remains in owned host processes. Recon, repair
generation and live tests share one per-round budget. Calls reserve their maximum
output allowance before forwarding; failed calls and retries do not refund it.
Budget records persist under the private state directory. Select sufficient limits
for complete source coverage and all verification phases before starting a run;
an exhausted budget defers, without certifying partial coverage.

`--enable-fixes` enables actual owned repair PR creation and merging only when
the repository config also enables fixes, disables shadow mode, and permits
own-PR merges. It proves the baseline, failing regression and repaired full suite
before publication, then validates ownership, base, head and checks before merging.
The local checkout refreshes to the verified merged head and runs the full suite
again. `--ledger-issue NUMBER` selects the existing owned persistent ledger.
`--base-branch BRANCH` explicitly selects an existing integration branch for repair
PRs; omitting it selects the repository default branch. Its remote head must equal
the reviewed commit, its required checks must pass, and the PR's base branch and
both repository identities must match before merge. This allows a real delivery
trial on an unmerged candidate without changing the target repository's default.

Changing the configured command, image, model, budget, base branch or live-test mode invalidates
reuse of the existing request evidence. Retain the state directory on retries;
discarding it discards recovery and spend history. Per-repository runner adoption,
real owned repair delivery, fresh whole-project quorum and three clean dogfood
rounds remain required before release.
