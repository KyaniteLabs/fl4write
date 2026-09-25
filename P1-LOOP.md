# P1-LOOP — FL4WRITE implementation of Persistent-1 (first implementation target)

CEO order 2026-09-21: fl4write is the first implementation target of the finalized
P-1 pattern. This file mirrors the lab's schema of record
(`~/workspaces/persistent-1/LAB-NOTEBOOK.md`, seat FLYWHEELRD) — consumed, not forked.
One entry per fix cycle. The blind verifier is the planted-diffs reliability harness
(`fl4write.reliability`): its numbers are the verification field, and a cycle whose
numbers did not move the product is a gathering, not a conclusion.

## Loop law (mirrored from the lab, adapted to this seat)

1. Sections per entry, fixed order: status header / HYPOTHESIS / WHAT RAN /
   LOOK (raw) / LOOK FINDINGS / DELTAS / SURPRISE / DEAD-ENDS / NEXT-HYPOTHESIS.
2. A cycle is not done when it runs. The harness gather is not the conclusion;
   authored LOOK FINDINGS close the entry.
3. Every number real. Harness receipts under `~/.fl4write/reliability/` (runner
   state dir, never the checkout).
4. Fixes route propose-only through PRs (the product's own law — nothing lands
   outside a reviewed, CI-green PR).
5. The loop never weakens the safety rails (fork=comment-only, scrub gate,
   grounding gates) to make a number move.

---

## ENTRY 001 — 2026-09-21T05:39-06:3xZ — reliability re-measure + corpus validity — status: closed (look authored 001; blind verifier confirmed the hypothesis)

- harness receipts: `~/.fl4write/reliability/` — pre-fix corpus report measured
  05:39Z (FP 60%, precision 78.6%); law-clean corpus re-measure 05:58Z CLOSED the
  entry: FP 0.0, severity precision 12/12 = 100%, determinism 100%, recall 10/10
  + 10/10 actionable. Trend rows land in `~/.fl4write/reliability/trend.jsonl`.

### HYPOTHESIS
The actionable-FP regression (40% baseline -> 60% re-measure) is not model noise
but corpus invalidity: the clean cases violate the org's own testing law, so the
law-mandated missing-tests notes are counted as false positives. Making the clean
cases law-clean (each ships tests) isolates true noise and restores the FP metric
to what it claims to measure, without touching the analyzer.

### WHAT RAN
Champion route Qwen3.8-27B via gpu-host:8908, runs=5, 30 real model calls,
determinism 100% (15/15, 0 severity flips), recall 10/10 caught_any AND
10/10 caught_actionable. Pre-fix corpus: actionable-FP 60% (3/5 clean cases
flagged), severity precision 11/14 = 78.6%.

### LOOK (raw)
All three flagged clean cases carried the SAME rule class — `testing-quality`
Major, "new core logic function ... does not include any tests"
(clean-median stats_util.py:1, clean-pagination paging.py:1,
clean-file-close count_config.py:1). The two unflagged clean cases
(clean-sql-params, clean-threshold) drew no findings at all.

### LOOK FINDINGS
The "false positives" were true findings under the org's review law ("changes to
logic ship with tests") — the corpus, not the reviewer, was wrong. A clean case
must be clean against the LAW, not merely defect-free: correct-but-test-less
snippets legitimately draw the note. Baseline comparability is preserved by
keeping the defect corpus frozen and recording that both historic FP readings
(the 40% and the 60%) contained this class.

### DELTAS
Corpus: 5/5 clean cases now ship real tests (test files added; corpus date-free
integrity law enforced — a dated docstring draft was refused by the corpus's own
guard, which is the guard working). Product code: none (correctly — the analyzer
was not at fault). Metric honesty: FP now measures noise; law-note rate stays
derivable from harness records for anyone who wants it.

### SURPRISE
The corpus's date-free guard rejected my own documentation edit (an ISO date in
a comment) — the repo's hardening laws police their maintainers too.

### DEAD-ENDS
Softening the testing-quality rule or its severity to lower FP — rejected: it
would tune the product away from a CEO-chartered law to please a meter.
Reclassifying law-notes as non-FP inside the harness accounting — rejected as
unilateral goalpost-moving; the corpus fix addresses the cause, not the symptom.

### NEXT-HYPOTHESIS
On the law-clean corpus the champion's true actionable-FP is < 40% and severity
precision >= 84.6% (rubric thresholds); if any clean case STILL draws a finding,
that is genuine model noise and the next cycle tunes the analyzer prompt (not the
law, not the corpus).

---

## ENTRY 002 — 2026-09-21T07:00-07:30Z — fix-lane fallback gap — status: closed (look authored 002; pin-verified, live re-observation queued)

- harness receipts: 07:00Z production cycle (20 ok / 0 errors) — the cycle line
  itself is the instrument here; trend row 3 (merged-main verification) covers
  the review lane.

### HYPOTHESIS
After #23/#26 every config has a distinct fallback, so a primary-provider
outage should cost zero reviews. If any model_down appears anyway, it marks a
call site that bypasses the route loop — not a config gap.

### WHAT RAN
07:00Z cycle on merged main: deepinfra primary returned HTTP 402 Payment
Required org-wide (out of credit). Reviews: 8 reviewed, 0 errors — carried by
the local Qwen3.8-27B fallback. Fix lane: 3 failures, all "model unavailable:
HTTP 402" (Achiote #248/#250).

### LOOK (raw)
`ALERT: fix failures: 3 — #250 error: model unavailable: HTTP Error 402;
#250 ...; #248 ...` while the same cycle's reviews succeeded through
fallback. executor.py:424 called `_call_model(config.model, ...)` — primary
route only; the route loop lives in analyzer.analyze.

### LOOK FINDINGS
Resilience was built per-config but not per-call-site: the review path loops
routes, the fix path did not. A config-level fix cannot heal a call site that
never consults the fallback. The outage was also a free live drill: the
fallback lanes carried real production traffic through a genuine primary
failure with zero lost reviews.

### DELTAS
Fix path now loops distinct routes exactly like the review path (executor.py,
PR #32). Two pin tests: primary-402 -> fallback answers; all-routes-down ->
honest error label. Suite 1257 passing + 4 skipped.

### SURPRISE
The strongest DIM6 evidence was unplanned: a real 402 payment-wall is a better
outage drill than any synthetic probe.

### DEAD-ENDS
None this cycle; the gap was found, fixed, and pinned in one pass.

### NEXT-HYPOTHESIS
The 08:00Z cycle (with #32 live) shows fix_attempts>0 with no 402-class
"model unavailable" reasons while deepinfra stays down — proving the fix lane
rides the fallback too. If deepinfra credits refresh first, the hypothesis is
untestable this window and waits for the next outage (the drill will come).
