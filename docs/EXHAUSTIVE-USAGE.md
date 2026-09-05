# Exhaustive evidence loop

**Unmerged implementation draft; not approved for deployment.** The fix executor,
runner integration, independent quorum and real dogfood gates below remain open.

This default-off workflow produces SHA-bound local evidence. Passing `--ledger-issue NUMBER`
publishes each completed round by editing that existing forge issue in place; after three clean
rounds at one SHA, the same idempotent PATCH publishes certification. Without that option the run
stops at explicitly local, uncertified evidence. It never claims independent review authority.

```bash
python3 -m fl4write.exhaustive \
  --repo /absolute/path/to/repo \
  --state-dir /durable/private/fl4write-state \
  --config /absolute/path/to/repo/.fl4write.yaml \
  --ledger-issue 13 \
  --test-command 'python3 -m pytest -q --junitxml {junit}'
```

Recon has no caller-command surface. Every round launches the fixed trusted worker in a fresh
subprocess and temporary home. The worker loads the configured primary model, receives only that
route's credential, and sees the prior ledger plus a read-only `git archive` of HEAD.

The worker enumerates every UTF-8, NUL-free tracked file. It chunks large files on line boundaries
while preserving absolute line numbers. An unreviewable file or line, malformed or truncated
response, provider outage, call-limit exhaustion, or output-token reservation shortfall defers the
round. Before each call, the worker increments the call count and reserves the route's complete
`max_tokens`; model-reported usage cannot enlarge the envelope. There are no automatic retries.

Every finding must name the current file, an absolute line, and exact single-line archived
evidence. The coverage manifest binds reviewed ranges to file hashes. The archive manifest binds
those bytes to the reviewed commit.

Findings reset the green counter and persist as pending evidence before fix action. Fixes remain
off unless `--enable-fixes` is explicit. Forgejo fixes defer with a named unsupported-adapter
escalation. GitHub establishes the repository-specific App installation identity, but currently
defers before opening a PR: the existing executor can replace only one finding file and cannot
atomically include or attest a regression pin. The required integration API is
`attempt_fix_with_regression_pin(pr, findings, config)`, returning PR number, author, fork flag,
base SHA, changed paths, test IDs, and JUnit hash. The exhaustive lane will not simulate those
proofs or open a one-file PR and call it compliant. Recovery turns an interrupted pending round
into durable non-green evidence, so a crash cannot erase defects.

The test command runs on the same archive and must contain standalone `{junit}`. Malformed numeric
attributes, aggregate or child failures/errors/skips, zero or duplicate test IDs, nonzero exit, and
lost baseline IDs reset the counter. Ledger rows bind the JUnit hash and test IDs. Provider outage
alone leaves the counter unchanged.

HEAD is checked after recon and tests. Drift is non-green. Three consecutive zero-finding,
full-green rounds for one SHA can publish `forge_published`, scoped only to archived-text recon and
the configured full test command. Publication uses one caller-selected existing issue, so retries
repeat the same PATCH body and cannot create duplicate issues. All model, process, and ledger text
is recursively scrubbed before publication. Moving HEAD invalidates certification. Corrupt state
fails closed.

Remaining acceptance gates: extend the executor with the atomic regression-pin API above; prove
the GitHub PR author/fork/base/HEAD rails against the live App identity; integrate a per-repo
default-off runner trigger; obtain fresh-context quorum review; and dogfood three clean live rounds
at the shipped SHA. Local mocked tests do not prove those gates.
