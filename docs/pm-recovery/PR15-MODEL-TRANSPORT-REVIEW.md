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
oversized-response accounting. Independent follow-up remains pending.

At `49a4930`, canonical Forgejo CI run 15 passed. Real isolated live evaluation
ran all 792 tests: 791 passed and the median case failed. Six provider calls
completed. The captured provider response for the missed case was an empty
findings array; no grounding filter or truncation explained the miss.

The review prompt now explicitly substitutes assertion inputs, follows branches,
compares actual and expected values, and preserves the test contract when proposing
repairs. With that change, one complete isolated live run passed all 792 tests,
zero skips, six completed model calls. This is evidence of that run, not proof of
stable recall or three consecutive fresh whole-project clean rounds. The combined
prompt and transport repairs still require full validation and independent review.

Automatic CLI model-budget integration, real owned fix/PR/merge/refresh execution,
and final release gates remain open. The earlier security review remains incomplete
after an automatic restriction; its stopped experiment was not repeated.
