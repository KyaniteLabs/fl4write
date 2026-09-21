# Round29 metrics repair checkpoint

Fresh independent review of exact `81a97d5` found one Minor defect, F29-001:
acceptance metrics recognize only single-backtick resolved markers, while the
real renderer widens code fences for paths containing backticks. A resolved
finding at such a path disappears from resolved/addressed totals, producing
`n/a` instead of the normal-path `100%` acceptance result.

The independent default suite passed 1036 tests with three expected paid-model
skips in 192.27 seconds; Ruff, shell syntax and configuration loads passed.
JUnit SHA256:
`b04b1f09a70e78050f378d0a6f1c1e9d3d33a522364ec70d366c358b6945816e`.
The new valid defect resets the clean functional review counter to 0/3.

The repair extends the existing line-start marker contract to a backtick run,
matching the renderer's fence widths. Fourteen new real-renderer, normal-path,
near-match and malformed-marker cases yielded three baseline failures and
eleven passing controls. The repaired focused suite passed 36 tests and Ruff.
Exact independent recheck approved the repair: three baseline failures and
eleven controls, 36 repaired focused tests, Ruff and mixed renderer/acceptance
controls. Full combined default validation passed 1050 tests with three
expected paid-model skips in 233.56 seconds. Ruff passed. Canonical CI passed
at exact repair commit `a946bd948b56a2f32a7af6bde0b7c06d774e8927`.

The configured provider returned HTTP 402 during the separate real recon run.
No further provider calls are being made pending restored access, so these
new repairs do not yet have exact committed live validation. Prior `81a97d5`
live 1039-test success remains historical evidence for that earlier code only.
Security, CEO quality verdicts, real full-recon/quorum/clean product rounds
and final main/mirror/sole-runner delivery remain open.
