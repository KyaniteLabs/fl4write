# Protected-merge policy (guard / auth / merge changes)

**Law:** a pull request that touches FL4WRITE's guard, auth or merge code is
**never auto-merged** — not with green checks, not by the bot, ever. It needs
the second key (`kyanite-factory`) plus a Security review at the exact head,
performed by the external merge gate. This file documents the code contract
introduced by card `t_5a7e1b77` (E2).

## Where the authority lives

The protected path set is **not** stored in this repository. It is a
platform-owned JSON document whose absolute path is read from
`FL4WRITE_PROTECTED_PATHS`. FL4WRITE opens it read-only (`open("r")`), never
writes, creates or chmods it, so a PR cannot widen its own protection. The
platform should install it owner-owned and mode `0444`.

If the variable is unset, the file is missing, unreadable, not valid JSON, or
does not carry a non-empty `protected_paths` list, **every merge is refused**
(fail closed). Merging this feature onto a runner without the policy file
therefore stops owned-PR auto-merges until the file is installed; that is the
required behaviour, not a regression.

## Document shape

```json
{
  "protected_paths": [
    "fl4write/appauth.py",
    "fl4write/forges.py",
    "fl4write/executor.py",
    "fl4write/gatekeeper.py",
    "fl4write/telemetry.py",
    "fl4write/scrub.py",
    "fl4write/writeguard.py",
    ".github/workflows/**",
    "fl4write/mergepolicy.py",
    "fl4write/fixlane.py",
    "fl4write/exhaustive_fix.py"
  ]
}
```

Patterns are POSIX globs matched case-sensitively against each changed file
name (`fnmatch`; `*` spans `/`). The first eight entries are the Product-fixed
set for card `t_5a7e1b77` (E1's outbound guard is `fl4write/writeguard.py`).
`fl4write/mergepolicy.py` (this classifier), `fl4write/fixlane.py`
(`merge_own_pr`, the gate) and `fl4write/exhaustive_fix.py` (the second merge
client) are **required additions**: without them the mechanism could be
edited by an unprotected PR.

A changed `*.fl4write.yaml` is protected when its diff adds or removes a
`repo:` binding, or when the file itself is added, deleted or renamed. Other
edits in that file are unaffected.

## The two live merge clients

FL4WRITE has exactly two code paths that can request a merge of a bot-owned
PR. Both classify the PR's changed-file list against this policy **before**
they request a merge, and both pass the decision through the same gate
(`fixlane.merge_own_pr`):

1. `executor.check_and_merge_own_prs` — the periodic own-PR scan
   (`fix.merge_own_prs`). Cheap gate checks first (`fixlane.merge_precheck`),
   then `GET /pulls/{n}/files` (paginated) -> `mergepolicy.decide` ->
   `fixlane.merge_own_pr` -> the merge request.
2. `exhaustive_fix.attempt_fix_with_regression_pin` — the atomic repair lane
   (`SEC-E2-1`). After its fix PR is pushed and the required checks are
   re-verified green, it reads the same changed-file list through the selected
   forge (`_Forge.changed_files`), classifies with `mergepolicy.decide`, and
   passes the decision to `fixlane.merge_own_pr` before `forge.merge`. A
   refusal happens BEFORE the merge-request receipt, so no forge write follows
   it and a retry re-classifies from scratch.

`exhaustive_fix._recover_merge` cannot initiate a merge. It only verifies a
merge a previous attempt already requested: it returns a MERGED result only
when the forge reports the PR merged and the merge commit on the default
branch; an open PR returns `None` and the ordinary, classified path runs.

The rest of this section describes the scan; the exhaustive lane follows the
same rules with the same classifier and the same gate.

## The enforcement path

1. `executor.check_and_merge_own_prs` runs the cheap gate checks
   (`fixlane.merge_precheck`: config enabled, own author, CI green).
2. It fetches the PR's changed-file list (`GET /pulls/{n}/files`, paginated).
   A transport error, non-list payload, empty list or the 20-page cap yields
   no evidence and the merge is refused.
3. `mergepolicy.decide(...)` matches the changed files against the policy.
4. `fixlane.merge_own_pr(..., merge_decision=...)` — **the gate itself** —
   refuses a missing or non-allowed decision. A refusal emits exactly one
   `merge_blocked` telemetry event:

   ```json
   {"kind": "merge_blocked", "repo": "KyaniteLabs/fl4write", "pr": 47,
    "head": "<40-hex>", "matched_paths": ["fl4write/executor.py"],
    "reason": "protected_path"}
   ```

   and raises `FixLaneBlocked`; the scan skips the PR and it stays open. No
   write request is made, so a refusal can never open a fix PR.

## Landing a protected change

FL4WRITE's scan can never merge it. The PR stays open until the external gate
merges it as `kyanite-factory` after a non-author Security review at the exact
head (card P3 / `t_6a5c0ffe`). A protected refusal is telemetry, not a target:
the fix lane must not try to "repair" the guard that refused it.
