# Model transport and live recall

Independent bounded functional review requested changes at `49a4930`.
It found two defects: synchronous shutdown waited for active HTTP, and the
provider-body limit did not account for response encoding and its envelope.
The reviewer passed 72 focused tests and deselected three paid live cases.
This was not a whole-project clean round or security clearance.

The repairs put each provider HTTP request in a tracked subprocess with a total
deadline and explicit cancellation on context exit. Socket handlers run separately
and close at exit. Transport encoding uses compact UTF-8 and an envelope allowance;
oversized results consume their reservation and count as failed. Regression tests
exercise a real local HTTP subprocess cancellation, a large Unicode response, and
oversized-response accounting. Independent follow-up approved both repairs at
`6af8898e7f5c6b1bd3a1b75dbb3283c6e44a51c2`, with 36 focused tests passing.

At `49a4930`, canonical Forgejo CI run 15 passed. Real isolated live evaluation
ran all 792 tests: 791 passed and the median case failed. Six provider calls
completed. The captured provider response for the missed case was an empty
findings array; no grounding filter or truncation explained the miss.

The review prompt now explicitly substitutes assertion inputs, follows branches,
compares actual and expected values, and preserves the test contract when proposing
repairs. With that change, one complete isolated live run passed all 792 tests,
zero skips, six completed model calls. This is evidence of that run, not proof of
stable recall or three consecutive fresh whole-project clean rounds. The combined
prompt and transport repairs then passed all 795 tests, zero failures and zero
skips, on the exact reviewed head `6af8898`. Six real model calls completed with
24,000 reserved output tokens, no failed calls and no active calls at exit.
The JUnit artifact SHA-256 is
`2972fadf55e5b3bb6f5d1cc5432e4d056b7acd23e01a7f7c48765ab96197c36f`.
The immutable runtime was
`sha256:83d40c1bef716674cb6a5c2c0d38477f505deadf684d8a379e69c28cd6b4dda7`.
[Canonical CI run 16](https://git.kyanitelabs.tech/KyaniteLabs/fl4write/actions/runs/16)
also passed on that head. These results clear this checkpoint's scoped tests and
functional review, not the remaining product-wide gates.

Automatic CLI model-budget integration, real owned fix/PR/merge/refresh execution,
and final release gates remain open. The earlier security review remains incomplete
after an automatic restriction; its stopped experiment was not repeated.
