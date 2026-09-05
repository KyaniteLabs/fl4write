# Round 23 repair checkpoint

Fresh independent functional review of `8fc1de5` found one Major and one
Minor defect. Its default suite passed 941 tests with three paid-model skips;
Ruff, shell and configuration checks passed. This is not a clean round, and
the clean-review counter remains 0/3.

R23-001 (Major): forge intake filtered timestamps chronologically but sorted
them as strings. With a per-cycle cap, mixed fractional precision or UTC
offsets could move the watermark beyond an older unreviewed PR permanently.
Related state and retro cursor comparisons had the same representation error.
The repair shares timezone-aware parsing across intake, watermark advancement
and retro ordering/filtering. It preserves the original persisted spelling;
equal instants do not advance the watermark. Existing microsecond precision
is retained, so finer timestamps compare conservatively equal and remain
discoverable. Twenty-nine regressions cover both real adapters, capped cycles,
state and retro controls. The initial baseline had 14 failures and two passing
controls; 84 focused tests and Ruff pass after repair. Independent review
identified a persistence-policy mismatch, now repaired with actual save/load
pins so accepted aware timestamp spellings survive the next cycle. Final
independent review approved the repair with 96 focused tests, Ruff and real
adapter cap-split probes covering equal instants and mixed representations.

R23-002 (Minor): the resolved-findings section encoded an already parsed path
identity again, doubling visible backslashes and changing the displayed path.
The generated repair removes only that redundant encoding call. Two new
render/parse/resolve cases fail before repair. Independent review of the exact
generated artifact passed 25 tests, 11 additional path lifecycle cases and
Ruff. Normal paths, escaped paths, backticks and Unicode preserve their prior
identity through resolution. This is scoped approval, not full validation.

A real generated repair trial ran against `b83dddd`. Its baseline
passed 944 live tests, the new regressions failed as expected, and the fixed
suite passed 946 live tests. PR20 at `44c0587c4efa5545ff5480f8cdb98853df08432f`
has green exact-head CI. Automatic resume repeated a green baseline and
expected regression failures, but fixed-suite live evaluation missed the
planted unsorted-median bug. The trial stopped without merging; PR20 remains
open and unmerged. Durable usage was 37 calls and 156000 reserved output tokens.
Original failure evidence is preserved. The independently approved exact path
repair is included with the timestamp repair; this is manual integration and
does not satisfy automated lifecycle success.

Combined default verification passed 972 tests with three paid-model skips
in 85.88 seconds; Ruff passed. Exact committed live verification remains open.
Three fresh clean rounds, security review, CEO quality
adjudication and main/mirror/runner delivery remain open. Production is
unchanged.
