# PR15 isolation checkpoint — 2026-09-05

The draft now requires an immutable Docker runtime for automatic fix verification.
Repository tests run as a non-root child against read-only source with no external
network route or host credential mounts. A separate supervisor records process
status and returns bounded JUnit evidence. Runtime failures defer the round.
Patch validation also rejects repository metadata paths and rejects staged bytes
that differ from the tested worktree after Git normalization.

Validation on this candidate:

- Focused repair and sandbox suite: 41 passed in 7.59 seconds.
- Full default suite: 777 passed, three paid live model tests skipped, in 145.09 seconds.
- Scoped Ruff and diff whitespace checks passed.
- Real Docker passing, failing and timeout probes passed their expected outcomes;
  invocation containers were removed. See the [runtime guide](../../tools/exhaustive-runtime/README.md).

The latest integrated live run remains 769 passed and one median-recall failure
at the preceding checkpoint. A successful diagnostic retry did not resolve or
supersede that failure. The new container currently has no model-service transport.

Independent security review D036 on `36c6173` ended with an automatic cybersecurity
restriction during a Git-configuration test. No report was produced; this is an
incomplete review, not approval. The stopped experiment was not rerun. Earlier
request-change reviews remain part of the record, and the repaired code still
needs independent clearance.

PR15 remains draft. Real fix/pin/owned-PR/merge/refresh execution, three consecutive
fresh clean rounds, review clearance, merge and mirror/runner synchronization are
still open. None of the runtime smoke tests counts as exhaustive certification.
