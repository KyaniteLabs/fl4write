# Round25 repair checkpoint

Fresh independent functional review of frozen `f63addb` found three P2
defects. Default validation passed 987 tests with three paid-model skips;
Ruff, Bash syntax and 130 configuration loads passed. This is not a clean
round; the counter remains 0/3.

Persisted numeric keys passed isdigit checks but failed integer conversion
for superscript digits and oversized ASCII strings. A shared bounded parser
now guards PR identities, model-failure prefixes and retro seen/park state.
Eighteen new cases cover reconciliation and valid-state preservation. Initial
baseline thirteen cases had eleven failures and two controls passing; 57
focused tests initially passed. Independent review found valid string park
expiries could be lost during prune/save/reload; two true persisted-cycle
regressions now pin the correction. Final implementation checks passed 59
focused tests. Final independent review approved both normalization repairs
with 59 numeric/retro/CI tests, Ruff and a separate two-cycle persisted-park
probe; the future park remains intact and both cycles review zero PRs.

CI annotation start_line values parsed from JSON numeric overflow escaped
the existing fallback conversion. Both overflow signs reproduce the crash
on frozen code. Catching OverflowError uses the existing fallback line1;
the two new cases and 23 existing CI-watch tests pass. Independent CI review
approved the bounded fallback repair.

The PR review-to-fix boundary can start repairs after its deadline expires.
The repair stores a SHA-bound unattempted suffix separately from the completed
review. Open and post-merge intake resume it without repeating analysis or
comments. Checks before each finding and after freshness lookup prevent new
attempts after expiry; pending post-merge work pins the watermark. Eight new
cases and 280 neighboring tests pass. Independent review approved the repair,
including persisted three-cycle open/post-merge tests, disabled/shadow/stale
head controls, and verification that analysis/comments are not duplicated.
The full combined default suite passed 1015 tests with three paid-model skips
in 189.43 seconds, including 28 new regressions; Ruff passed. Exact code commit
`965d787e9116c913c1ca851fb70e204d2bebad49` passed all 1018 live tests with
zero skips, Ruff and canonical CI. Live JUnit SHA256:
`a6c42d86c6412429cf4bff8aa4e67a3f542e6c45d9a386733d88bdb035eab6f9`.
The isolated live run used six calls and 24000 reserved output tokens.

The original eight first-week checklist observations are reconciled, including
the verified zero-alert hourly cycle. Expanded delivery remains incomplete:
three fresh clean rounds, automatic repair lifecycle proof, security review,
CEO quality verdicts and main/mirror/sole-runner delivery remain open.
