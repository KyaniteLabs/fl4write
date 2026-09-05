# Round 22 repair checkpoint

Fresh independent functional review of `56af12b` found two P2 defects. Its
full default suite passed 924 tests with three paid-model skips; Ruff, shell
and configuration checks passed. The clean-review counter remains 0/3.

F22-001: retaining only the newest 200 retry IDs after advancing the issue
watermark permanently hid older unresolved issues. The manual repair retains
the oldest pending IDs and lowers the watermark before any omitted pending
ID. Higher open issues remain discoverable through ordinary collection.
Complete listing removes closed retries; shadow mode preserves live state.
The tradeoff is possible repeat review/update of successful higher issues
when overflow forces a rewind. Eight regressions cover overflow, existing
retries, deadline deferral, recovery, closure and shadow behavior. Independent
review passed 28 tests and 100 additional seeded recovery trials, plus Ruff.

F22-002: completed full-tree scans returned before checking effective scan
scope, so removing an exclusion at unchanged HEAD never scanned newly included
files. Completed scans now check the existing effective fingerprint before
reusing completion. Unknown legacy scope finishes an old publication retry,
then schedules a fresh audit without allowing fixes from unknown coverage.
Uncertain listings defer completion reuse; cap violations invalidate it.
Nine new cases cover both filter types, unchanged controls, legacy migration,
cap expansion and uncertain listings. Seventy-three neighboring tests passed
with Ruff. An old clean-publication fixture now keeps its seeded HEAD and
fingerprint consistent while testing the same publication flag behavior.
Independent scope review passed 47 tests and Ruff, reproduced seven failures
and two unchanged controls against baseline, and verified a multicycle legacy
publication-failure/recovery/listing/rescan sequence.

A separate real generated retry-overflow trial ran against `8bbf7f3`.
Its baseline passed 927 live tests and its two new regression cases failed
as expected; the fixed suite passed 929 live tests. It opened PR19 at
`becd842100e25d82571684f2f45f8417d86f698c` and stopped pending CI.
Independent review rejected it: shadow mode with a watermark of 500 and
202 open retries rewrites the live watermark to 200, while baseline retains
500. The trial had finished before interruption could take effect. PR19 was
closed unmerged after review. Original cache and proof evidence remain
unchanged; the trial consumed 19 calls and 84000 reserved output tokens of
its original 49-call/196000-token budget. No automated lifecycle success is
claimed. The separately approved manual repair preserves shadow state.

Combined default validation passed 941 tests with three paid-model skips
in 80.43 seconds; Ruff passed. Exact `8fc1de51e66c043524dab8bc19e0c595e65f97bd`
live validation passed all 944 tests with zero skips and Ruff; canonical CI
passed. Six live calls reserved 24000 output tokens. Live JUnit SHA256:
`f9d5c54e9b0adaa3a6d769efcf314f484b1ba1c968ef60e7292074ec0463552f`.
Fresh round23 remains active and has reported a merged-PR timestamp-ordering
defect; green tests do not clear that new finding.

Three fresh clean rounds, automated lifecycle proof,
security review, CEO quality adjudication and main/mirror/runner delivery
remain open. Production is unchanged.
