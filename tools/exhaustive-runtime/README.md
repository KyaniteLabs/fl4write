# Isolated exhaustive test runtime

The exhaustive CLI defaults to Docker isolation. Automatic fix verification
requires this mode and an immutable local image ID supplied with `--test-image`.
`--isolation process` remains available for trusted local diagnostics; it cannot
verify automatic repairs.

Build the runtime from this directory with an explicitly selected Python base:

```sh
docker build --build-arg PYTHON_BASE=python@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea -t fl4write-test-runtime:isolated-v1 .
docker image inspect --format '{{.Id}}' fl4write-test-runtime:isolated-v1
```

Use the returned image ID with `--test-image sha256:...`. The test command must
contain a standalone `{junit}` argument, for example
`python3 -m pytest -q --junitxml {junit}`. Install project dependencies in a
reviewed derivative image before running tests; runtime network access is disabled.

Tests run as a non-root child of the trusted supervisor, with a read-only source
mount and bounded temporary filesystems, memory, CPU and process count. Host home,
credentials, container socket and writable host output directories are not mounted.
The supervisor records the exit status in a separate root-owned directory. The
host reads a bounded regular JUnit file through the supervisor and removes its
own container after each invocation. Runtime failures and timeouts defer the round.

## Execution evidence and limits

On 2026-09-05 a real Linux Docker run using image
`sha256:f9f9dcf09bfd7249c323a8107626852415eb948ba5df35da5c911a17c57ab3e8`
produced these results:

- Boundary probe: one passing test, non-root identity, source write denied,
  supervisor directory write denied, no external network route, no Docker socket.
- Known failing assertion: one failed test and exit status 1.
- Timeout: deferred with no JUnit success artifact.
- Cleanup: no containers with the invocation prefix remained after these probes.

These probes validate the runtime boundary only. They do not prove a full project
repair, publication, merge or three-round certification. This runtime currently
has no model-service transport: FL4WRITE's three paid live model tests cannot run
inside it. Full live validation and independent review remain open release gates.
