# Round30 renamed-path repair checkpoint

Fresh independent offline review of exact `a946bd948b56a2f32a7af6bde0b7c06d774e8927`
found one Minor defect, R30-001. A real tracked rename from `old.py` to
`dir b/new.py` is parsed as `new.py` by both forge readers. A correctly
anchored finding is then dropped, and the review engine posts a clean result
and marks the SHA reviewed. The header-only separator heuristic cannot
unambiguously recover the destination of every unquoted renamed path.

The independent default suite passed 1050 tests with three expected paid-model
skips in 157.82 seconds. Ruff, 131 configuration loads and three shell syntax
checks passed. The new valid finding keeps the clean functional counter at
0/3. Offline review cannot establish a complete clean round while exact live
validation remains unavailable.

The first repair passed 96 focused tests and the reviewer's eleven original
real-Git controls. Independent combined controls then exposed the same path
ambiguity in quoted Unicode deletions and binary modifications whose names
also contain ` b/`. Approval was withheld and the repair reopened. The first
combined suite was intentionally interrupted after 155 passing tests; it is
not a full-suite result.

The extended repair resolves quoted token boundaries before searching
unquoted separators. Twelve new cases now pass, including the two added
baseline failures; 98 focused checks and Ruff pass. Complete diff readers
prefer rename/copy or new-file metadata before the first hunk or binary body,
and both forge readers and analyzer line spans share that final identity.
Independent final review approved the exact repair: all sixteen real-Git
controls passed (the baseline failed twelve and passed four), 64 targeted
tests passed in 3.40 seconds, and scoped Ruff and diff checks passed. The
controls compare both forge readers and analyzer maps against Git's actual
NUL-delimited changed-file list. They include copy, pure rename, quoted and
mixed headers, control-bearing paths, deletion, binary changes and misleading
hunk text. Full combined default validation passed 1062 tests with three
expected paid-model skips in 194.33 seconds. Full Ruff and diff checks passed.
Full-suite JUnit SHA256:
`50ad02557fae44dcc74822886b33c7c840ac34555caff3ed00ea1699ad0dbec6`.
Canonical CI is tracked on the PR commit status; this checkpoint does not
claim a new live-provider result.

Reviewed SHA256 identities:

- `fl4write/analyzer.py`: `c2a5fea748bb4acf820efa256421d5d20a362e3bd9bc3c339aa3492f4a09e04f`
- `fl4write/cli.py`: `231ace15251355c0abf9d26ec9acc75a3f5288e2e31405822b0009e1833ca0be`
- `fl4write/forges.py`: `f252a4fd40ff254783026283fdb7841758f12ee11e2d89bb5e8642ae1bd0cfba`
- `tests/test_renamed_diff_paths.py`: `fa9e27e71724be62abef1676ab8bf888d1f7eea2c5ab108b3c3e5f0711af4a44`

The configured provider's confirmed HTTP 402 blocks further paid-model validation and real
full-recon execution pending restored access. No prior live result certifies
this new repair. Security review, CEO quality adjudication, full-recon/quorum
and three clean product rounds, and main/mirror/sole-runner delivery remain
open.
