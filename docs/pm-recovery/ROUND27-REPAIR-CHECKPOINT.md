# Round27 repair checkpoint

Fresh independent functional review of exact `795d832` passed 1028 default
tests with three paid-model skips, Ruff and shell syntax checks. It found two
P2 defects, so this is not a clean round; the counter remains 0/3.

F27-F001: the CI-watch benign conclusion set accepted `canceled` but omitted
`cancelled`. A completed cancelled check created a false CI-red escalation
and consumed the SHA action marker. The repair accepts both spellings.
Three new cases cover persisted cancellation→failure→deduplicated retry and
mixed cancellation/failure checks at the check cap. Baseline: two failures,
one passing spelling control.

F27-F002: explicit notice/warning annotations were promoted to Major findings
and sent to automatic repair. The repair filters those levels before applying
the annotation cap, preserving actual failures and legacy missing levels.
Five cases cover informational-only escalation, mixed rows at a one-annotation
cap and legacy compatibility. Baseline: four failures and one passing control.
An independent reviewer also reproduced the behavior through the real adapter's
annotation normalization and identified the cap-order requirement.

Both repairs pass 31 focused tests and Ruff. Independent scoped review
approved both exact changes: baseline six failures and two passing controls,
33 repaired tests, Ruff and seven real-adapter/persistence controls.
The first combined run found only two README validation failures after the
status cleanup removed historical attribution and the required count form.
The attribution and explicit current candidate counts are restored; the full
suite passed 1036 tests with three paid-model skips in 173.12 seconds.
Exact committed `81a97d5ec7b88ecd664ee9d967eabec10f62a78d` passed all 1039
live tests with zero skips and Ruff; canonical CI passed at that exact head.
The live suite consumed six calls and 24000 reserved output tokens.
JUnit SHA256:
`d1e3ccac67fcd4fda690c70e3eee5171e19db48d1b5ead213fa6739216071779`.
The previously committed
795d832 remains proven by 1031 live tests with zero skips and canonical CI;
that earlier proof does not cover these new repairs.

One automatic lifecycle was proven in round26. Three clean whole-project
rounds, full recon/quorum dogfood, security review, CEO quality adjudication
and final main/mirror/sole-runner delivery remain open.
