# Round26 repair checkpoint

Fresh independent functional review of frozen `965d787` found two Major
defects. Default validation passed 1015 tests with three paid-model skips;
Ruff, Bash syntax and 131 configuration loads passed. This is not a clean
round; the counter remains 0/3.

F26-001: exhaustive recon validation transformed ordinary grounded source
filenames into a shared nonexistent display path. Offline validation-to-repair
execution failed before the model was called. Internal source identity must
remain exact through worker output, sealed evidence, repair and cached replay;
presentation transformations remain at presentation boundaries.

F26-002: atomic repair used line-delimited Git path output. Git quoted an
ordinary Unicode filename, and source hashing looked for that quoted spelling
instead of the real file. The proof failed before any baseline test ran.
Tracked, changed and untracked paths need byte-faithful NUL enumeration.

Both manual repairs are implemented with nine new regressions. The source
identity tests exercise offline worker output, sealed evidence, repair input
and cached replay. Git-path tests use real disposable repositories, including
Unicode, leading whitespace and CRLF-bearing names. Ninety-seven focused
tests and Ruff pass. Independent scoped review approved both repairs with
97 tests and extra real Git path probes; baseline nine new cases yielded
eight failures and one passing control.

A bounded live F26-001 trial runs against
`f71e8b1` on the existing integration branch. The harness requires independent
approval of the exact prepared patch before publication, then waits on real
CI status; all original ownership/base/head checks remain in the repair API.
Budget ceiling: 49 calls and 196000 reserved output tokens. The baseline
passed 1018 live tests, the new regressions failed as expected, and the fixed
suite passed 1022 live tests. Exact generated artifact SHA256:
`8226494beaf77db6e01aff1a88cf45730da2f1e59de6c0806fb3fa18c6970ef6`.
Independent exact-artifact review approved the three generated file rows,
with four intended baseline failures, 125 repaired tests, Ruff and six controls.
The approval marker was released only after the live fixed suite passed.
PR21 at `fef78d92457f8e6caa01f87111cc0c7bb147daa9` passed CI and was
automatically merged into the integration branch at
`cdf2b78fc3a1689e646e3526472b515884988f81`. The verified merge checkout
passed all 1022 live tests in the automatic refresh. Refresh JUnit SHA256:
`e3f03c12460a4382e54ed5f024fdba616b9297cd7056bad8040a47d2b71ddcd3`.
The trial consumed 25 calls and 108000 reserved output tokens. This proves
one real automatic repair, PR, CI, merge and refresh lifecycle; it does not
certify three clean whole-project rounds or final main delivery.
The combined local suite passed 1028 tests with three paid-model skips in
214.07 seconds, including four generated and nine manual regressions; Ruff
passed. The generated merge was fast-forwarded into the working candidate;
the separately reviewed manual repairs and deeper tests remain included.

Three fresh clean rounds, full recon/quorum dogfood, security review, CEO
quality verdicts and final main/mirror/sole-runner delivery remain open.
