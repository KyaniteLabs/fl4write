# Exhaustive evidence prototype — quarantined

**Unmerged review draft; not approved for deployment or certification.**
Independent review reproduced five unresolved safety defects. Forge publication
is disabled at both the CLI run boundary and the publication function.
`--ledger-issue` always defers without making a forge call or advancing state.
The candidate is excluded from main and the fleet runner.

The prototype explores fresh subprocess recon over archived tracked text,
model budgets, evidence-grounded findings, JUnit parsing and persisted round
state. Its test fixtures do not establish real finding quality or exhaustion.
Do not rely on its success return as certification: counter/ledger consistency
is one of the unresolved defects.

Isolated experimentation only:

```bash
python3 -m fl4write.exhaustive \\
  --repo /absolute/path/to/repo \\
  --state-dir /durable/private/fl4write-state \\
  --config /absolute/path/to/repo/.fl4write.yaml \\
  --test-command 'python3 -m pytest -q --junitxml {junit}'
```

The independent report is [FEATURE13-DRAFT-REVIEW.md](pm-recovery/FEATURE13-DRAFT-REVIEW.md).
Required repairs: bind counters to verified trailing ledger evidence; make
publication transactional; use a strict public-data schema; verify issue
ownership before updates; finalize and re-hash immutable evidence bundles.

The product also needs an atomic fix-plus-regression-pin executor API,
authenticated ownership/fork/base/HEAD rails, post-fix full-suite verification
and refreshed contexts, per-repository runner integration, quorum approval,
and three real clean dogfood rounds. Existing one-file fixes cannot satisfy
that contract. GitHub fixes stop at the missing API; Forgejo fixes explicitly
defer. These gaps remain unfinished engineering work.
