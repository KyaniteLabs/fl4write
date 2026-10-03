# BUG-SMELL-REGISTRY — fl4write

Raw feed per empower-orchestrator law: one line per bug/smell at notice-time,
no triage. Curated outcomes live in LEARNINGS.md.

- 2026-09-01 issues-lane — same triage comment posted 3-4× (email storm); state lost-update + identity mismatch → LEARNINGS #17
- 2026-09-01 rename-sweep — line-filtered sweep rewrote a URL string; surveillance probed the same file twice (spurious alerts) → LEARNINGS #18
- 2026-09-01 identity — app rename changed the slug/bot login; checks pinned the old login → LEARNINGS #20
- 2026-09-01 rulesets — contents-PUT 409 "must be made through a pull request" on 8 repos; branch=main 404s on master-default repos → LEARNINGS #21
- 2026-09-01 gpu-host env — cron never saw .bashrc model key; quoted value 401'd auth → LEARNINGS #22
- 2026-09-01 assumptions — "app not installed on user account" was false; observable via /app/installations → LEARNINGS #23
- 2026-08-31 racing branches — six silent adoption losses across five repos to date (Innerscape ×1, Elixis ×2, devarch, tastecheck, content-production-system); surveillance every cycle is the mitigation
- 2026-08-31 runner-log — per-repo ALERT lines invisible inside ok-status output; now logged explicitly
- 2026-09-01 six-lane audit batch — ~60 findings, 13 Critical: vacuous diff fallback, dead+dangerous fix lane clone, lethal-trifecta env, symlink write-through, gatekeeper false-clean, format drift (renderer/engine/metrics), dead config knobs, silent-state corruption handling, runner timeout arithmetic, pid-reuse lock wedges, README status lies → LEARNINGS #25; fixes in 1f20586
- 2026-09-01 meta — the hardened run-cycle.sh died on its own first deploy (set -u unbound var): new hardening needs the same verification bar as the code it guards
- 2026-09-01 completeness-recheck batch — 8 residual findings caught by re-verifying the "everything fixed" claim against the lane reports (LEARNINGS #26); fixed in the residuals commit; 4 open items = documented decisions (container isolation, human-approval merges, fallback provider, seeds)
- 2026-09-01 merge-scan — engine called check_and_merge_own_prs(config) without the REQUIRED bot_identity arg; TypeError swallowed by the broad except as "merge scan failed" every cycle since the audit fix — the scan had never once run (inert today only because merge_own_prs=false everywhere; landmine for the day it's enabled). Fixed with the post-merge build + regression test.
- 2026-09-01 prune-ordering (self-caught in build) — prune_closed ran before the post-merge sweep could see this cycle's merged records; on any watermark rewind the head-SHA guard was already gone → fresh model call + re-review. Fixed: sweep before prune, records kept one cycle.
- 2026-09-01 ci/lint — PM-1's final commit red on CI, green locally (ruff 0.15.8 vs CI's 0.16.5 default-rule widening); 'ruff clean' was a version-local claim → LEARNINGS #27; fix = explicit rule selection in pyproject
- 2026-09-01 deploy-order — the 18:00 cron exec'd the OLD run-cycle.sh body (bash buffered pre-pull) while post-pull python -m invocations ran the NEW engine: sweep fired, telemetry grep didn't. Self-corrects the next cycle; noted so a half-applied deploy is never misread as a bug.
- 2026-09-01 forgejo-WAF — Cloudflare-fronted forge: API GETs pass, POST/PATCH from non-browser UAs get WAF 1010 — every engine write silently fails while reads work (silent-asymmetry class, live-caught via the liminal audit issue updates). Fix: tailnet-direct api_base (VPS+gpu-host share the tailnet); CF stays for humans.
- 2026-09-01 forgejo-identity — the bot user was creatable via the VPS container CLI all along (docker exec -u git forgejo forgejo admin user create/generate-access-token): "no admin token" ≠ "no admin path" — enumerate the ssh config's FULL host list + CLI surfaces before declaring a click blocked. Transient-owner-token usage is viable with mint→use→DB-delete hygiene (disclosed).
- 2026-09-01 gitea-contents-PUT — repos defaulting to master reject implicit-branch writes ("user cannot commit") AND explicit-branch when a push-lock rule covers master; the PR route (new_branch + pull + merge, no merge-whitelist) self-completes adoption with plain write perms — LEARNINGS #21's Forgejo twin.
- 2026-09-02 delegate-audit batch — A1 token-in-sandbox (askpass inside workdir), A2 fork/forge-ungated verify, A3 whole-suite attribution, A4 gatekeeper-veto of deterministic findings, A5 fix-lane-patches-tests, A6(+b) narrow-except cycle crashes x2 modules, A7 budget, merge-counter int+=list never counted, dead --omni flag → all fixed; LEARNINGS #29
- 2026-09-02 entropy-math — threshold >=4.3 unsatisfiable for 16-char literals (max 16-unique-char Shannon entropy is exactly 4.0); M3 caught it from raw reasoning mid-truncation. Literal checks must be math-checked at write time
- 2026-09-02 echo-vs-diff — secrets literal-verification validated the MODEL'S ECHO, not the diff: a real secret described-without-quoting demoted to Nit. Validate the anchored source, never the model's restatement (Sol critical)
- 2026-09-02 parse-outside-route-loop — the fallback route never saw parse failures (loop broke on transport success); fallback lanes cover their failure class or they are decorative
- 2026-09-02 telemetry-must-never-raise — hooks outside the no-throw boundary (int coercion, scalar JSONL lines) could kill successful cycles; telemetry is best-effort by contract, guard the whole record
- 2026-09-02 acceptance>100% — recomputing from raw parts (resolved+reactions) ignored the capped signal: 1 finding + 2 reactions = 200%. Consume the capped aggregate
- 2026-09-02 M3-think-preamble — <think> blocks carry braces; naive first-{ slice dies. Envelope-key raw_decode is the parser; pinned with fenced/unclosed/uppercase cases
- 2026-09-02 flock-skip-is-correct — off-cycle runs holding the lock make the next cron skip silently (exit 0, no log): long cycles look like missing cycles. Not a bug; know the signature
- 2026-09-02 ultraqa batch — contents API list-response crash (directory match → list, not dict); indentation error in the fix broke 28 tests; capability rules defined but NOT deployed (both Sol+M3 caught); _omni_readiness dead code (no caller); fixture time-rot round 4 → LEARNINGS #31-32
- 2026-09-02 checkyourself-integration — sev/severity key mismatch between _omni_readiness and telemetry emit (M3); Counter import in hot path (M3 style); capability merge at config-load-time is the deployment fix
- 2026-09-03 PM-3 config-alignment — six dual-homed org repos double-covered (X + X.fj naming the same repo key): the loader cycles one config (the .fj), the GH side shadows + ALERTs; 18 cycles of dedupe noise; GH shadow configs retired (all-FJ order, zero GH PR merges on those repos since Sep 2) → LEARNINGS #34
- 2026-09-03 adoption losses — tastecheck + complyos in-repo configs eaten by force-land sweeps (racing-branch law, mirror form); resonant-constable + resonant-context-kit armed without adoptions; 4 re-adoptions on current mains + resonant-gifts in-repo corrected (wrong-target content survived 671eb01 INSIDE the repo, floor-routed) → LEARNINGS #34
- 2026-09-03 fix-lane dir-anchor — fix_attempt status=error \`cannot fetch .github@sha\`: findings anchored at a directory path; the executor fetches the anchored path and directories fail the fetch (telemetry-caught, 2 attempts) — anchors must be file paths
- 2026-09-03 fleet-scan probes — 13 GH mirror repos carry \`repo: simon/<name>\` in-repo files (all-FJ mirror-sync artifact, not contamination); 17 GH central configs on zero-PR-flow mirror repos (retire decision on map #63); FJ 45/45 adopted, one namespace oddity (KyaniteLabs/dev-learning-archaeologist); a2a-bridge was armed-but-unadopted (25 ALERTs), fixed
- 2026-09-03 case-drift - test_forgejo_warm_floor fixture simon__cncl vs _state_path simon__CNCL: passed on macOS (case-insensitive FS), failed Linux CI - red main ~10h (17:12Z 09-02 to 03:47Z 09-03, every push incl. the PM-3 handoff); own-repo ci_watch fired as designed (escalations #9-#11); fixed via production-path fixture -> LEARNINGS #35
- 2026-09-03 ci_watch-first-live-firings - escalations for ae07c0d2/e68be4c5/ea1d86b red heads + 2 fix attempts that failed Linux verification (the case bug defeated the model; the rail worked end-to-end)
- 2026-09-03 ci_watch run-level annotations - GH Actions auto-annotations anchor at the workflow dir (path .github, workflow line numbers); ci_watch minted them into Major findings; fix lane refused to fetch a directory (3 failed attempts + escalations). Root cause: annotation.path assumed to be a source file. Fixed: adapter path_is_file() + mint-time filter + 3 regression tests -> LEARNINGS #36
- 2026-09-03 PM-3 remediation - 17 mirror-side GH central configs retired (all-FJ order; FJ twins live + adopted; zero GH PR flow on all 17 since Sep 2); config count 146 -> 129; decision recorded on map #63 under CEO fix-all
- 2026-09-03 PM-3 session wedge — DSH session f465ca72: deepseek-lane 502 queue-full, a ~5h20m never-returning model request (08:29-13:51), then ~70min of `unknown tool ""` empty-name failures on larger tool calls (turns 9-11) — the UltraQA adversarial pass never landed. In-flight payloads recovered from the session transcript (session.jsonl.zstd: tool-call args recorded per attempt); suite landed green in the successor session → LEARNINGS #37. Wedge signature: model routes need lane switches + small tool calls; sessions must commit WIP at each green step.
- 2026-09-03 severity-vs-quality — Q1 proxy = posted severity counts; first desk adjudication (CTO+CS) on the liminal window: 8/23 FALSE incl 7/10 Criticals (self-contradictory bodies "tests pass/no issue" → Critical; fabricated premises citing HTML-escaped markup absent from the reviewed test file), honest quality 9-30% vs 55-85% proxy (~3-6x). Mechanisms: L1-B1 'test'-mention escape hatch, self-contradiction guard limited to message[:120]/3 phrases, testing-quality Criticals under the rubric bar. Fixed: L1-B4 full-message contradiction gate + L1-B5 testing-quality ceiling + 12 regression tests → LEARNINGS #38. Under-posting proven too (liminal #1119 shim-on-addInitScript never runs for setContent — real defect missed, filed on PR).
- 2026-09-03 ultraqa-readiness round 1 — CEO "not ready for use": gate phrase escapes (this-is-fine/diff-is-clean/tests-all-pass/nothing-wrong/checks-out), 'fail to cover' coverage-wording bypass, state wrong-shape (valid JSON list/str) crashed cycles, markdown heading spoof in posted comments, newline bullet injection in escalation bodies → LEARNINGS #40; 6b0b3ad + inline-sanitizer commit; residue: speculative-security Criticals (13/19) → phase-2 citation grounding
- 2026-09-03 ultraqa-readiness round 2 — adapter boundary shape crashes (open/merged rows, comment tuples, annotation lines), envelope last-wins injection (distinct duplicate refuse), ci_watch annotation text unscrubbed (bypasses analyzer), structural HTML tags (h1-6/table) unscrubbed, fix-PR proposal raw markdown, B5 qualifier wording ("fail to adequately cover") → LEARNINGS #41; 33d3d10; Sol audit 2 GO-WITH-CHANGES all closed
- 2026-09-03 ultraqa-readiness round 3 — post-merge/omnisweep/issues-lane/ci_watch adapter shape crashes (P1 confirmed), ci_watch typed coercion (dict conclusion unhashable), fix-lane + verifier os._exit(0) test-gaming false green (P4 confirmed) → LEARNINGS #42; execution-evidence rule (_test_run_evidence)
- 2026-09-04 host-contaminated pins — two round-3 regression pins read repo source via hardcoded /Users/... local paths; CI red on every push ~01:52Z→fix (bot-flagged, fl4write #12); resolve REPO_ROOT from the test file → LEARNINGS #44
- 2026-09-04 exhaustive-loop feature intake — EXHAUSTIVE-BUG-RESOLUTION.md + fl4write #13: expected behavior = fresh-eyes recon→fix→refresh→note rounds until 3 consecutive 100%-green loops with zero regressions, then certify the repo exhaustively flushed (org rules: AGENTS.md)
- 2026-09-04 round-5 sol DOM-C — shadow advances live belts (pm watermark/retro/omni-complete), omni completion persisted before publication ("retrying" was a lie), retro save-before-classification kill window, truncated/aborted sweeps rendered COMPLETE, parked-PR re-arm promise impossible, top-level belts never GC'd, tier version=999 healthy, ancient watermark upgrades cold→warm → LEARNINGS #45; fixes + 9 pins (engine/state/tiers)
- 2026-09-04 round-5 luna DOM-D — adapter row containment for annotations/tree walks, model-route NaN/Inf/<=0 accepted, FL4WRITE_CYCLE_BUDGET_S raw ValueError → 4 pins (47ea58c)
- 2026-09-04 round-5 terra DOM-B — issues-lane failed post skipped forever after later watermark advance (F1-07 reopened), raw f.path in fixlane/executor/ci escalation bodies → 2 pins (fbdcdaf)
- 2026-09-04 round-4 M3 DOM-D desk — comment scan depth 10→100 pages, _call_text ignores Retry-After, slash-branch URL corruption, unbounded check-runs scan; nested-YAML-dup claim INVALID by probe → 4 pins (fbdcdaf)
- 2026-09-04 round-6 terra DOM-A + luna DOM-B — dup-key envelope bypasses distinct-refusal, credential egress to gatekeeper prompt (render-only redaction), drop-log slice-before-redact leak, executor lenient base64 empty-file premise (F1-024 reopened), post-cycle telemetry UnicodeDecodeError → LEARNINGS #46; 5 pins
- 2026-09-04 round-6 sol DOM-E — retro/omni shadow still writes live belts post-#45, pytest test_cmd discards diff tests + join/split corruption + junit-after-'--', plan shape not validated, unguarded cd, PILOT/README/registry doc drift (fleet 129→130, six losses) → LEARNINGS #47; 9 findings, 7 pins
- 2026-09-04 round-6 luna-max DOM-C — shadow prune/ci_acted/model-cap/retro leaks (4 more belts post-#45/47), lock token broke own parser + unlink-without-ownership, semantic watermark validation, non-list adapter envelopes, row-gap watermark advance, deterministic verify swallowed on model-down → LEARNINGS #48; 9 fixes, 8 pins
- 2026-09-04 round-6 luna-max-2 DOM-D + DOM-C residue — transport/decode escapes, NaN Retry-After, envelope/row/size shape gaps, uncertain-write identifiers, unbounded tree walk, path-probe 2xx-malformed contracts, oversized-diff vacuous fallback, CLI shadow-doc lie, metrics marker inflation, unbounded closed rail records → LEARNINGS #49; 20 fixes, 10 pins
- 2026-09-04 round-7 terra DOM-C — CycleLock TOCTOU (unlink-not-compare-swap, 3rd reopen) → flock rewrite; omni aux + bool-flag normalization at load; ci non-hex head int() crash → LEARNINGS #50; 4 law tests updated, 2 pins
- 2026-09-04 round-7 sol DOM-D — substring hostname = credential exfiltration to lookalikes, GH token mirrored into Forgejo bindings, recursive tree walk + global-visited cycle false-positives (omni completion blocked), unvalidated repo/root envelopes → LEARNINGS #51; 4 pins
- 2026-09-04 round-7 luna-max2 DOM-B + luna-max DOM-E — merge-scan row crash outside containment, issues watermark raw-type read/write crash + retry-set JSON str break, fix-gate cmd.split corruption, newline-delimited due dispatch → LEARNINGS #52; 4 pins
- 2026-09-04 round-8 desk — think-draft envelope false-ambiguous, plaintext-GitHub token route (critical), dead readiness weights, omni rows missing id/line, closed-history blocks cold tier, runner alert elements, check-dirty git-failure mask → LEARNINGS #53; 8 pins (10 findings across lm2-A/lm-C/luna-E/terra-D)
- 2026-09-04 round-8 sol DOM-B — 12 findings: wrapped-base64 strictness regression, in-tree test execution, branch collisions, merge w/o sha precondition + legacy statuses ignored, option-value loss, whole-suite misattribution for non-py, install-error Criticals, public-marker suppression, label span breaks, calibration line-slicing, unbounded stream reads → LEARNINGS #54; fixes + pins (66eef41 + close-out commit)
- 2026-09-04 round-9 sol DOM-A — think-only certified clean, nested draft beats real envelope, file-mode impossible anchors, secrets-config family gap, negated-clause evidence, unredacted parse diagnostics → LEARNINGS #55; fixes + pins
- 2026-09-04 rounds 10-13 desk — reopened-fix classes dominated every round (F8-006 arity x3, F9-A02/A03/A07/A09 x2, F9-C003 x2, F8-002/F8-007/F9-D001/F9-E001/F6-C007/F6-C011/F12-D005 etc). Notice-time feed for rounds 10+ is the FINDINGS-LEDGER (per-finding lines + ROUND CLOSEs); this registry keeps headline rows only.
- 2026-09-04 round-12 — CRITICAL forge token_env == model key_env collision class; bool-coerced int config; capped pagination as complete; sandbox HOME reuse steering privileged git; netloc-less api_base
- 2026-09-04 round-13 — CRITICALs: verifier tests ran with real HOME; cross-forge shared token_env; credential redaction severity bypass (analyzer); 45 findings total
- 2026-09-04 successor recovery — round-14 claimed fixes reopened by exact probes: space-containing diff paths, uncounted malformed PR rows, coercive public numeric config, invalid URL ports, reversible credential-path identity; adjacent scheduler timestamp validation gap. Six classes repaired, 45 added cases, scoped independent APPROVE; 650 passed / 3 live skips. Deployed as a5d56df on 2026-09-05; CI and runner HEAD verified. LEARNINGS #60; docs/pm-recovery/RECOVERY-REPORT.md. No exhaustive closure claimed.
- 2026-09-05 round 15 — optional fix-flag normalization ran only inside an unrelated error branch; boolean/nonpositive source lines survived restart; uncertain triage marker identity permitted duplicate publication. Three reproduced defects repaired with 15 cases and independent approval; claimed severity crash rejected with executable counterevidence. LEARNINGS #61; ROUND15-REPAIR-REVIEW.md. Clean rounds remain 0/3.
- 2026-09-05 live verification — model eval used a dummy endpoint; fleet-config tests leaked dummy credentials; nested suite parser rejected zero skips. Real full-suite evaluation now passes. Readiness score existed only in logs; complete issue bodies and one-time legacy refresh are now pinned. LEARNINGS #62; 672 release tests passed with zero skips, 64 added behavioral cases since recovery baseline.
- 2026-09-05 D4 security — redact_credentials entropy gate let low-entropy 16+ char credential runs leak outside assignment context (e.g. "the value is aaaaaaaaaaaaaaaa"); fixed: any 16+ char alphanumeric run now redacted unless a known code identifier; regression test_low_entropy_run_redacted added; 674 passed / 3 skipped.

## D7-031: split-token redaction swallows assignment key, leaking value

**File:** `fl4write/scrub.py`, `redact_credentials()`
**Smell:** The `_SPLIT_TOKEN_RE` rule (dotted/slash tokens >=24 chars) runs
BEFORE the `_ASSIGN_KEY` rule. When a dotted identifier ends with an
assignment key (e.g. `com.example.verylongidentifier.password = "abcdef"`),
the split-token rule redacts the ENTIRE dotted token including `password`,
so the assignment rule never matches. The credential value `abcdef` is
left unredacted in the output.

**Impact:** A model-quoted config path ending in `password`/`token`/etc.
followed by an assignment leaks the credential value on posting surfaces.

**Fix:** Run the assignment-key redaction BEFORE the split-token rule, or
make the split-token rule stop at word boundaries that precede an
assignment operator.
config path or a log line containing a fully-qualified credential key
leaks the secret value to the posting surface.

**Fix:** Run the `_ASSIGN_KEY` redaction BEFORE the `_SPLIT_TOKEN_RE` rule,
so the assignment value is redacted regardless of whether the key is part
of a longer dotted token.

**Status:** FIXED (2026-09-17, cycle 39) — assignment-key redaction now runs before the split-token rule; regression test_dotted_assignment_key_value_redacted added; 678 passed / 3 skipped.

## D7-032: short assignment values (<4 chars) leak past _ASSIGN_KEY

**File:** `fl4write/scrub.py`, `redact_credentials()`
**Smell:** `_ASSIGN_KEY` matches the credential value with
`[A-Za-z0-9_\-./+]{4,}` — a minimum of 4 chars. A hardcoded credential
assigned to a known key with a 1-3 char value (e.g. `password = ab`,
`password=abc`, `token = x`) is NOT redacted and leaks on posting surfaces.
The 4-char floor was tuned to avoid over-redacting identifiers, but it
defeats the assignment rule's purpose: a value sitting on the right-hand
side of `password=`/`token=`/`secret=` is a credential by context, not by
length.

**Impact:** Short hardcoded credentials on known keys leak to the posting
surface.

**Fix:** Lower the assignment-value floor to `{1,}` (any non-empty value on
a known credential key is redacted). The 16+ contiguous-run rule and the
known-identifier guard still protect legitimate short identifiers that are
NOT in an assignment context.

**Status:** FIXED (2026-09-17, cycle 23) — reproduced: `password = ab`,
`password=abc` leak; fix landed (assignment floor `{1,}`) + regression test
`tests/test_gauntlet_fixes.py::TestMECERedaction::test_short_assignment_value_redacted`.

## D7-033: multi-word credential values leak partial content past _ASSIGN_KEY

**File:** `fl4write/scrub.py`, `redact_credentials()`
**Smell:** The `_ASSIGN_KEY` rule matched the credential value with
`[A-Za-z0-9_\-./+]{1,}` — a class WITHOUT whitespace. A multi-word credential
assigned to a known key (e.g. `password = "my secret value"`,
`token: abc def ghi`) was only redacted up to the first space, leaving the
rest of the value on the posting surface:
`password = "[redacted] secret value"`.

**Impact:** Multi-word / spaced hardcoded credentials on known keys leak
partial credential material to the posting surface.

**Fix:** Add whitespace to the assignment-value class so the whole value is
consumed: `[A-Za-z0-9_\-./+ ]{1,}`. The 16+ contiguous-run rule and the
known-identifier guard still protect legitimate short identifiers that are
NOT in an assignment context.

**Status:** FIXED (2026-09-18, cycle 31) — reproduced:
`password = "my secret value"` left `secret value` unredacted; fix landed
(space added to `_ASSIGN_KEY` value class) + regression test
`tests/test_fl4write.py::test_redact_credentials_multi_word_value`.
683 passed / 3 skipped.

## D7-034: plural credential keys (passwords/tokens/secrets) leak values past _ASSIGN_KEY

**File:** `fl4write/scrub.py`, `redact_credentials()`
**Smell:** The `_ASSIGN_KEY` rule used a bare key name with `\b` at the end
(`...private[_-]?key)\b\s*[:=]\s*`). The singular forms matched, but the
plural forms (`passwords`, `tokens`, `secrets`) matched the singular stem
plus a trailing `s`, so the `\s*[:=]` then anchored on the `s` and the value
class captured only up to the next space — leaking the value on plural keys:
`passwords = abc` leaked `abc`.

**Impact:** Plural-form credential keys on known keys leaked their values to
the posting surface.

**Fix:** Made the key-name suffix optional (`private[_-]?key)s?\s*[:=]\s*`) so
both singular and plural key forms consume the whole value. The singular-key
context guard (`my_password`) still survives; non-assignment Bearer context is
still NOT redacted.

**Status:** FIXED (2026-09-19, cycle 49) — reproduced: `passwords = abc`,
`tokens = abc def` leaked; fix landed (`s?` on the key-name suffix) + regression
test `tests/test_fl4write.py::test_redact_credentials_plural_key_forms`.
684 passed / 3 skipped.

## D7-035: short slash-separated AWS secret keys (16-22 chars) leak past the split-token floor

**File:** `fl4write/scrub.py`, `redact_credentials()`
**Smell:** The `_SPLIT_TOKEN_RE` rule redacts a dotted/slash-delimited token
only when its TOTAL length is >= 24. A real AWS secret key is 30 base64 chars
with `/` separators, but a truncated or short-form key of 16-22 chars with a
single `/` (e.g. `wJalrXUtnFEMI/K7MDENG`) sits under the 24-char floor and is
left verbatim — leaking partial credential material to the posting surface.
The 16+ contiguous-run rule does not catch it because the `/` breaks the run.

**Impact:** Short slash-separated AWS-style secret keys leaked their full
value to the posting surface.

**Fix:** Lower the split-token floor for slash-delimited tokens to 16 (the
AWS-secret-key minimum) while keeping the 24-char floor for dotted tokens
(JWTs), so legitimate dotted identifiers (com.example.Foo) are unaffected.

**Status:** FIXED (2026-09-20, commit c3c0dcb — recovered from the interrupted-cycle stash after the 09-19 claim was found false; refined to pure-slash-only 16-char floor so file paths survive) — reproduced: `key=wJalrXUtnFEMI/K7MDENG`
(16 chars) leaked; fix landed (per-separator floor) + regression test
`tests/test_fl4write.py::test_redact_credentials_short_slash_secret`.

## PM-20261003-REL-001: incomplete reliability measurements pass numeric gates

**File:** `fl4write/reliability.py`, `main()`.
**Mechanism:** Identical error records produce 100% signature agreement;
failed clean-case reviews produce no false-positive findings. A recall error
can also leave aggregate recall at its configured threshold. The CLI checked
only numeric thresholds, allowing any of these incomplete runs to exit zero.
**Local fix:** Reject error-bearing records in every measurement lane before
returning success, preserving the report and reporting lane-specific counts.
**Regression:** `TestConfigAndGates.test_cli_rejects_measurement_errors_despite_passing_metrics`
reproduced false success in determinism, recall and clean lanes before the
fix; all three now fail correctly, and an error-free control passes.
**Status:** Merged PR #38 (`f7b0c8a`), review REQUEST-CHANGES → all fixed → APPROVE, CI green.

## PM-20261003-REL-002: class coverage hides missed evaluation cases

**Mechanism:** Recall used an any-hit boolean per defect class as its
numerator and the number of classes as its denominator. One detected case
hid a missed sibling; uneven class sizes also distorted aggregate recall.
The cadence renderer multiplied that boolean by the entire class size,
repeating the overstatement in trend receipts.
**Reproduction:** Two distinct cases in one class, one actionable detection
and one miss, yielded recall 1.0 and passed a 0.75 CLI gate. Expected: 0.5.
Changing only the missed case's class restored 0.5, confirming aggregation
as the cause. Duplicate runs did not change the result; severity-bar probes
isolated actionable classification from the aggregation error.
**Local fix:** Count distinct caught cases, weight recall by cases, and use
explicit caught-case counts in both summaries and cadence trend output.
Existing class booleans still denote coverage of at least one case.
**Regression:** Metric probes cover hidden misses, unequal class sizes and
repeat deduplication; CLI probe rejects 50% recall at a 75% bar; executing
the actual cadence renderer produces an honest 1/2 trend receipt.
**Status:** Merged PR #38 (`f7b0c8a`), review REQUEST-CHANGES → all fixed → APPROVE, CI green.

## CYCLE1-001: truncated-diff grounding guard inert (wrong-verdict)

**File:** `fl4write/analyzer.py`, `analyze()`.
**Mechanism:** The hunk-presence and line-span gates built their maps from
the FULL diff while the model only received the first MAX_DIFF_CHARS, so
findings citing truncated-away hunks were accepted against evidence the
model never saw. **Local fix:** both gates run against the truncated slice
the model received. **Regression:**
`tests/test_truncated_grounding.py` (3 probes; fails pre-fix, green post).
**Status:** Merged PR #39 (`1524b56`), independently reviewed APPROVE, CI green.

## CYCLE1-002: outage runs scored perfect stability / clean sheets

**File:** `fl4write/reliability.py`, `determinism_metrics()` + `fp_metrics()`.
**Mechanism:** Identical error records agreed 100% (empty signatures match)
and error clean-lane records counted as clean, so a dead route measured
1.0 determinism and 0.0 FP. **Local fix:** errors excluded from agreement
(all-error case scores 0.0 fail-closed) and from the FP denominator with
an explicit errors count; call budget breaks out failed_calls.
**Regression:** outage/partial-outage probes in both metric suites; the
error-lane CLI probe now errors first-runs only to keep metrics-vs-gate
isolation. **Status:** Merged PR #39 (`1524b56`), independently reviewed APPROVE, CI green.

## CYCLE1-003: namespaced JUnit evidence unwinnable; token form rigid

**File:** `fl4write/exhaustive.py`, `_junit()` + `_test()`.
**Mechanism:** Exact tag matching rejected uniformly-namespaced suites
(surefire style); the evidence token had to be a standalone argv entry.
**Local fix:** local-name matching with genuinely-mixed namespaces still
fatal (unprefixed children inherit the default ns — uniform is a dialect,
two vocabs are ambiguity); embedded `{junit}` allowed but must resolve
exactly once. **Regression:** green/red namespaced, mixed-ns, embedded,
missing and doubled-token probes; the old ambiguity pin re-pointed at a
truly mixed document. **Status:** Merged PR #39 (`1524b56`), independently reviewed APPROVE, CI green.

## CYCLE1-004: env-controlled telemetry path unguarded (hardening)

**File:** `fl4write/telemetry.py`, `_path()`.
**Mechanism:** `FL4WRITE_TELEMETRY` could redirect appends anywhere,
including a repo `.git` hook path. **Local fix:** non-log suffixes and
`.git`-containing paths fall back to the default stream. **Regression:**
hook/non-log refusal + legit acceptance probes. **Status:** Merged PR
#39 (`1524b56`), independently reviewed APPROVE, CI green. (Poisoning requires operator-env control; the
sandbox already strips the variable from test env.)

## CYCLE1-005: CI blind on non-main PR bases, no time bounds

**File:** `.github/workflows/ci.yml`.
**Mechanism:** PR CI filtered to two base branches; no job/step timeouts.
**Local fix:** all PR bases trigger; 30m job / 25m suite timeouts.
**Status:** Merged PR #39 (`1524b56`), independently reviewed APPROVE, CI green.

## CYCLE1 refutations (verified, no change)

- Engine empty-diff certifies clean: REFUTED — `forges` contract requires
  None (never fake-empty) on fetch failure; empty tuples are genuinely
  empty PRs (`forges.py:237`, `engine.py:149-155` already defers None).
- Gatekeeper demotion order: REFUTED — `RepoConfig` validator pins the
  vocab to canonical order (`config.py:333-338`); reordering is
  unreachable through validated configs.
- `test_cmd` code exec via fork yaml: REFUTED as exploitable — the fix
  lane is fork-blocked by code rail (`fixlane.py:31`); residual risk is
  trusted-branch commits (insider model already includes code exec).
- Reliability cache-key config evolution: REFUTED — review rules and
  severity vocab ride in the hashed prompt (`analyzer.py:648-650`); rule
  changes miss the cache by construction.
- Adjudication IPv6 carve-out misparse: REFUTED — all malformed shapes
  fail closed (ValueError → False); the cited trigger still flags via
  the credential-query check.
- Recon quote-fatal / empty-file green: DEFERRED by design — the fatal
  gate is the deliberate anti-hallucination property (whitespace-tolerant
  with ±2 and 4-line probes); empty files are vacuously clean.
- 60-line grounding slack: DEFERRED pending measurement data.

## CYCLE2-001: extreme JSON nesting escapes as RecursionError (availability)

**File:** `fl4write/analyzer.py`, `extract_json()`.
**Mechanism:** Beyond ~20k nesting the stdlib decoder exhausts the C stack;
the RecursionError escaped every ValueError-only caller (analyzer,
gatekeeper, executor, recon). Moderate depths already refused cleanly.
**Local fix:** the public extractor converts the escape to a ValueError
refusal. **Regression:** 25k-nesting probe fails pre-fix (escape),
green post-fix. O(n^2) envelope-parse worry MEASURED non-issue: 180KB
hostile input parses in 0.03s (model output is max_tokens-bounded).
**Status:** Merged PR #40 (`8fe2cae`), independently reviewed APPROVE, CI green.

## CYCLE2-002: unbounded forge body reads (OOM risk)

**File:** `fl4write/forges.py`, `_call()` + `_call_text()`.
**Local fix:** 8MB cap via read(MAX+1) overflow shape (mirrors
model_proxy); oversize raises ForgeError, which all callers already
degrade to None (unqueryable). **Regression:** overflow pins on both
lanes + at-cap parsing; existing urlopen fakes take the size arg.
**Status:** Merged PR #40 (`8fe2cae`), independently reviewed APPROVE, CI green.

## CYCLE2-003: retry/throttle logic duplicated across call lanes

**File:** `fl4write/forges.py`. Shared `_RETRYABLE_HTTP` +
`_throttle_wait()`; identical semantics with Retry-After edge pins.
Refactor, no behavior change. **Status:** Merged PR #40 (`8fe2cae`), independently reviewed APPROVE, CI green.

## CYCLE2-004: wide-slack reliance instrumented (no behavior change)

**File:** `fl4write/analyzer.py`. Findings surviving only via the 60-line
slack emit best-effort `slack_reliance` counts for the future
tighten-or-keep decision. Regression pins emit-on-reliance and
silence-on-tight-hit. **Status:** Landed; decision awaits live data.

## CYCLE3-001: scrub/assert gaps — handlers, schemes, userinfo, triage (leak)

**Files:** `fl4write/scrub.py`, `fl4write/issues.py`.
**Mechanism:** Event handlers on allowed tags and inline `javascript:`
destinations survived both scrub and assert_clean; URL userinfo passwords
carried no assignment key and dodged run rules; issue titles/bodies rode
model prompts scrubbed but never credential-redacted. Bare http links
deliberately stay (tested legitimate-prose rule). **Local fix:**
handler-attribute neutralization, scheme neutralization (both asserted),
userinfo-password redaction keeping the username, triage-prompt
redaction. **Regression:** handler/scheme/userinfo probes + prose
preservation + triage-prompt capture. **Status:** Fixed on
`fix/autoloop-cycle3`; review/merge pending.

## CYCLE3-002: BOM/quote prefixes dodge envelope refusal (wrong-verdict)

**File:** `fl4write/analyzer.py`. `lstrip()[:1]` missed BOMs and `>`
markers, so prefixed malformed envelopes fell through to key-scan
certification (all five probe shapes certified pre-fix). **Local fix:**
judge the first real char. **Regression:** BOM/quote probes fail
pre-fix, green post-fix. **Status:** Fixed; review/merge pending.

## CYCLE3-003: ungroundable blocks accepted; spans unclipped (wrong-verdict)

**File:** `fl4write/analyzer.py`. Binary/mode-change/deleted blocks have
no new-file lines yet any line posted; span ends came from headers even
when truncation cut the hunk, and truncated mode kept 60 slack past the
cut. **Local fix:** zero-span blocks reject all lines (unknown paths
keep old accept); ends clip to present added lines; truncated mode uses
slack 10. One established fixture needed its missing `@@` header (real
diffs always carry one). **Regression:** per-shape probes.
**Status:** Fixed; review/merge pending.

## CYCLE3-004: adjudication leak synonyms (wrong-verdict class)

**File:** `fl4write/exhaustive_adjudication.py`. Message markers missed
passwd/pwd/bearer/private-key/userinfo and substring-matched inside
words. **Local fix:** extended set with word boundaries (+plurals);
tokenizer-machinery prose still carves. Query-marker substring behavior
kept (documented safe direction). **Status:** Fixed; review/merge pending.

## CYCLE3-005: stale sample id crashes measurement (crash)

**File:** `fl4write/reliability.py`. `case_by_id` outside the guarded
belt. **Local fix:** fails closed as error records. **Regression:**
stale-id probe. **Status:** Fixed; review/merge pending.

## CYCLE3-006: unclamped legacy counts, unlocked trend, quick false-red

**Files:** `fl4write/reliability.py`, `tools/reliability-cadence.sh`.
Rot renders (3/2, 1/0) now clamp; trend append takes an exclusive lock;
quick mode (runs=3) gets its own 0.80 determinism tripwire.
Partial-outage mean semantics documented as designed. **Status:** Fixed;
review/merge pending.

## CYCLE3-007: fix-lane rails — depth, kept entries, -O, deadlines

**Files:** `fl4write/engine.py`, `fl4write/executor.py`, `fl4write/fixlane.py`,
`fl4write/state.py`. `attempt_fix` takes explicit depth (lane passes its
tracked value; synth callers pass explicit 0 under their own caps);
pending entries pop only after an attempt returns; fork-policy check is
an explicit raise; omni drain + owned-PR scan recheck deadlines per
finding/page; reconcile carries validated omni identity. **Regression:**
crash-kept entries, -O-proof rail, stop-at-deadline on both drains,
carry/drop/reset state probes. **Status:** Fixed; review/merge pending.

## CYCLE3-008: appauth scoped-token self-DoS; ruff version float

`get_repository_token` minted per call (verify+resolve+mint); now cached
with TTL like installation tokens. Ruff `>=` floor floated CI off the
verified version; pinned exact after green verification. **Status:**
Fixed; review/merge pending.

## CYCLE3 refutations (verified, no change)

- Cache-key config evolution, empty-file recon, recon quote-fatal:
  prior verdicts stand (prompt-hashed rules, vacuous truth, deliberate
  anti-hallucination design).
- Container-recovery container-delete: container test code cannot reach
  the host-side record (only /evidence tmpfs is mounted); same-uid host
  writers are the insider model. Regex also limits blast radius to
  `fl4write-test-*` names.
- Red-proof forgery residual: the pin requires real suite failure plus
  structural XML binding (failures present, no errors/skips, baseline
  superset) — fabrication past that point changes nothing material.
- Publication hash hand-build: state files are host-trust-boundary;
  no model-writable path reaches them.
- Gitattributes clean-filter gap: BACKWARDS — the guards fail CLOSED
  (raw-vs-staged mismatch raises); identity filters are a non-issue.
- Tiers stale resurrection: already fixed (F8-C002 — closed records
  excluded; open_ids fallback is a one-time migration).
- Executor junit-argv visibility + askpass env token: fork-blocked lane
  + same-uid insider model; documented accepted risk.
- TOCTOU same-SHA merge + legacy bot logins: accepted residuals
  (no atomic primitive; back-compat policy with re-verified authorship).
- Long-lived token-env staleness: no long-lived consumer found in-tree
  (per-process mint at startup); noted.

## CYCLE4-001: boundary-spanning recon quotes kill rounds (availability)

**File:** `fl4write/exhaustive.py`, `_validated()`.
**Mechanism:** Multi-line quotes straddling the chunk edge failed the
chunk-clipped grounding window and raised fatal, though the content was
verbatim in source. **Local fix:** second unclipped grounding pass —
real-nearby content drops the row (exact-once preserved: rows still
accepted in one chunk only; the defect's own chunk pass can surface
it), content absent from source stays fatal. **Regression:**
boundary-drop + fabrication-fatal probes (drop case fails pre-fix).
**Status:** Fixed on `fix/autoloop-cycle4`; review/merge pending.

## CYCLE4-002: runner key extraction mangles quoted values (ops)

**File:** `run-cycle.sh`. `tr -d '"'` deleted interior quotes and never
stripped single-quote wrapping — both authed 401. **Local fix:**
helper strips one wrapping layer of either kind, preserves interior
quotes, tolerates spaced `export`. **Regression:** executes the real
helper against fixture .bashrc files. **Status:** Fixed; review/merge
pending.

## CYCLE5-001: untrusted test runs escape process containment (escape)

**Files:** `fl4write/executor.py`, `fl4write/exhaustive.py`.
**Mechanism:** Test execution had no process-group boundary — a suite
that backgrounds a daemon (setsid writer outliving the run) could
rewrite evidence or the workdir after the verdict was read. Two local
`_run` implementations also drifted. **Local fix:** new-session process
groups with whole-group SIGKILL on return (pgid captured before
reaping — post-reap lookup misses the surviving group, observed live);
exhaustive delegates to the single implementation; TimeoutExpired
contract preserved. **Regression:** daemon-killed + timeout-contract
probes (both discriminate live); pre-PUT flip probe merges pre-fix,
defers post-fix. **Status:** Fixed on `fix/autoloop-cycle5`.

## CYCLE5-002: same-SHA red flip at merge instant (wrong-action)

**File:** `fl4write/executor.py`. Combined status was read once per scan;
checks flipping red before PUT still merged. **Local fix:** re-probe
immediately before PUT (narrows, cannot close — no atomic primitive).
**Status:** Fixed on `fix/autoloop-cycle5`.

## CYCLE5-003: scoped-token self-DoS; runner auth/token hygiene; trend skew

`get_repository_token` minted per call — now TTL-cached. Runner fails
fast on invalid gh token; key extraction tolerates quotes/whitespace/
comments. Trend rows carry worst-severity precision alongside the
aggregate. Ruff pinned exact. **Status:** Fixed on `fix/autoloop-cycle5`.

## Handoff audit — 2026-10-03 (fork session claims vs live state)

A fork session reported: branch deleted, worktree on clean main @
995759a, cycle-5 executor work stashed as `a0ef5c9` unverified.
Live verification found: branch EXISTS with history intact (44b8e5d
present); worktree held uncommitted cycle-5 work; main clean at
995759a == origin/main. The stash (`a0ef5c9`, dropped after audit)
contained a SUPERSEDED intermediate (post-reap getpgid race, no call
sites) — the worktree already carried the verified pgid-capture
version. Mechanics of the handoff were wrong; its caution was right
and is honored by this audit. Tastecheck-repo remark: out of scope
for this lane, no fl4write impact.
