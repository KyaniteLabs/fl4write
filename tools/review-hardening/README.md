# Review evidence and acceptance toolkit

This additive toolkit validates private review evidence and scores independently
adjudicated captures. It does not change the fleet engine's defaults, run models
on import, post reviews, fix code, or grant merge/deployment authority.

The transport preserves a durable one-attempt marker, private atomic receipts,
strict JSON, exact finish/model metadata and available counters. A per-control
ledger distinguishes backend-reported matches, mismatches and unknown settings.
Matching output limits or extraction format do not prove reasoning-budget use
or adequacy. Missing controls remain unknown; startup defaults and completion
token totals cannot fill those gaps. Requesting a run still requires the
operator's existing resource and route authorization.

The claim gate binds repository, revision, snapshot, location and exact source
quotations. Grounded claims remain unvalidated without caller-supplied independent
whole-allegation adjudication. Source-derived presentation preserves raw claim
and quote digests. Optional normalization repairs only uniquely pinned missing
ASCII-space indentation; it never repairs a behavioral allegation.

Optional detailed adjudications must check the entire `message`, `trigger`,
`expected` and `actual` fields. Each entry in `allegation_checks` binds the
field's UTF-8 SHA256 and supplies `disposition`, `artifact_sha256` and `rationale`.
A confirmed whole allegation cannot contain an unsupported, false, stale,
missing or malformed detailed check. The held-out scorer enforces the same
boundary. Legacy whole-allegation receipts retain their caller trust contract.
Checks come from the trusted evaluator, never the model envelope. Neither
hashes nor a reviewer name establish semantic truth or reviewer independence.

The proposed engineering target requires at least 50 positive and 50 clean
held-out cases, precision >=95%, recall >=90%, no false Major/Critical claim on
clean cases, and complete coherent runtime and safety provenance. Abstentions
retain missed defects. Frozen distinct-defect precision and claim precision
remain visible; changing a denominator cannot establish model improvement.
Passing this target does not authorize deployment or merge.

Run the offline checks from the repository root using existing dev dependencies:

```sh
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -B -m pytest -q -p no:cacheprovider tools/review-hardening/tests
python3 -B -m ruff check tools/review-hardening
```

The tests prohibit live HTTP/model requests and use authored synthetic custody,
runtime and adjudication records. They measure gate behavior, not model accuracy.
Real runtime receipts, infrastructure details and held-out oracle answers are
excluded. Live model quality, reasoning adequacy and fleet activation remain
separate, unaccepted stages.
