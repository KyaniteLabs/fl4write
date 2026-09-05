# Round 21 repair checkpoint

Fresh independent functional review of `bb3625a` found two Minor defects.
The review passed 881 default tests with three paid-model skips and Ruff,
but this is another non-clean round. The clean counter remains 0/3.

F21-001: diff-path decoding corrupted literal Unicode filenames, so grounding
silently discarded findings for those files. The generated repair preserves
unquoted Unicode and adds two direct regression cases. A separate manual
extension decodes quoted C escapes as bytes while preserving literal UTF-8;
five additional cases cover quoted and mixed paths plus actual analyzer
grounding. All five fail against the original parser. Independent scoped
review approved both artifacts separately: generated tests and existing
controls passed, as did nine manual-extension cases and Ruff.

F21-002: rendering stripped backticks from accepted custom rule IDs. Parsing
then reconstructed a different identity, causing unchanged findings to appear
new and resolved. The repair encodes rule identities losslessly in explicitly
labeled finding lines while preserving legacy parsing. Sixteen regressions
cover unusual IDs, literal escapes, legacy controls and repeated reviews.
Independent review through the actual engine parser passed 47 tests and Ruff.

The generated Unicode trial created PR18 at
`7bc102aef80de7db9240b85a005e7eee79eb5957`, targeting the feature branch.
Its original baseline passed 884 live tests; regression proof was red; the
fixed suite passed 886 live tests. Exact-head CI passed. The cached resume
repeated baseline/red/fixed proof successfully, then recreated a different
commit and failed a non-fast-forward push. It consumed 37 calls and 156000
reserved output tokens of the original 49-call/196000-token budget. This is
a failed automated lifecycle trial; its evidence remains preserved.

The independently approved PR18 was subsequently merged manually after
rechecking its exact head, base, ownership and green CI. The feature merge is
`986b5c546f58176b4a5a94bafaa11246bd1c767f`. This manual integration does not
clear the automated-resume gate. The combined candidate adds the quoted-path
extension and rule-ID repair; full default verification passed 904 tests
with three paid-model skips in 84.75 seconds. Ruff passed. Exact committed
live verification remains pending.

Full combined validation and delivery remain open. PR15 is a draft; main
merge, mirror/runner delivery, security review, three clean fresh rounds and
CEO quality adjudication remain open. Production is unchanged.
