# Round28 functional review and real recon checkpoint

Fresh independent review of exact
`81a97d5ec7b88ecd664ee9d967eabec10f62a78d` found no new confirmed
functional defect. This is the first clean functional review candidate (1/3),
not exhaustive certification or release approval.

Independent validation passed 1036 default tests with three expected paid-model
skips in 206.17 seconds. Ruff and all three shell syntax checks passed.
All 131 shipped configurations loaded, representing 129 distinct repositories.
Additional offline probes verified state SHA persistence, scheduler fallback,
and archive/chunk round trips across 271 tracked text files. Source remained
unchanged. Independent JUnit SHA256:
`42b82fa3c1d2ae9e866aa9a46ee8f44233e3b0fa4d7789e7fcafa3d072a6a184`.
The parent previously verified the same exact code with all 1039 live tests,
zero skips, Ruff and canonical CI; round27 records that separate live evidence.

## Real product recon

Whole-archive preflight at the same SHA found 271 text files, 280 chunks at
the configured 48000-character limit, and no unreadable or overlong-line
blockers. Default 64-call recon would be incomplete for this repository.
One actual product invocation selected the configured model and a durable
ceiling of 300 calls and 1200000 reserved output tokens, including test
headroom. It ran with Docker/live-test support and without repair or ledger
publication enabled, to validate real recon before acting on its findings.

The first request failed; the product recorded a deferral and no clean round
or certification. One bounded diagnostic repeated the exact first payload
through the configured provider and received HTTP 402. Combined durable use
is two calls and 8000 reserved output tokens. The original state and evidence
remain preserved. No further provider calls are being made pending restored
access; the owner has been notified. The diagnostic records only status and
counts, never credentials or response content.

The run therefore proves correct deferral on this provider failure, not
successful full recon. Remaining gates: two more clean functional reviews,
successful real full recon/quorum and clean product rounds, security review,
CEO quality verdicts and final main/mirror/sole-runner delivery. Security
coverage remains incomplete under the previously recorded automatic restriction.
