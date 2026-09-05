# Shared round budget validation

Independent functional review approved the integration at `42e59b9`, with 95
focused tests passing and no material functional finding. Recon, repair generation
and optional live tests now share durable reservations through one host proxy.
Reservations are written before forwarding and survive provider failures and
invocation retries. Tests cover the real recon subprocess, Unix transport, repair
caller, suite transport selection, malformed records and retry exhaustion.

The full isolated live suite on that head ran 801 cases: 799 passed, two failed,
zero skipped. One failure was the README's exact count format, restored in
`e0c76e2`; canonical CI run 18 independently showed that same documentation failure
in its default suite (797 passed, one failed, three skipped). The other live failure
was another empty model findings response on the median case. All six provider
requests completed; the earlier prompt change did not establish stable recall.

The product's own configured sampling temperature is now zero for reproducibility.
This changes the real route used by evaluation and the product's own reviews,
not the corpus or its assertions. DeepInfra documents lower temperature as less
random, not a guarantee of correctness or identical results:
[chat completion parameters](https://docs.deepinfra.com/chat/overview).
At `545771e`, this configuration passed all 801 tests in the real isolated live
suite, with zero skips and six completed model calls. Canonical CI run 20 also
passed. No successful run erases a
recorded miss, and no bounded review is counted as a fresh whole-project round.

The feature still needs actual owned fix/PR/merge/refresh execution, fresh whole-
project review rounds, remaining review clearance, and delivery sync. The earlier
security review remains incomplete after its automatic restriction; the stopped
experiment has not been repeated.
