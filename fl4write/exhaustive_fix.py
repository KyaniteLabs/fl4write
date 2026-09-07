"""Atomic exhaustive-loop fix executor.

The public entry point deliberately keeps model/test execution separate from
forge authentication.  It prepares and proves a multi-file patch in disposable
local clones, then publishes and merges only a bot-owned, non-fork PR whose
base and head still match the proved commit.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Callable, Iterator

from .config import ForgeBinding, RepoConfig


_SHA = re.compile(r"^[0-9a-f]{40}$")
_BRANCH = "fl4write/exhaustive-fix-"
_RECEIPT = "exhaustive-fix.json"


class FixError(RuntimeError):
    """A contained fix failure."""


def _result(status: str, reason: str, reviewed_head: str, **extra: Any) -> dict:
    out = {
        "status": status,
        "reason": reason,
        "reviewed_head": reviewed_head,
        "merged_head": None,
        "pr_number": None,
        "pr_url": None,
        "author": None,
        "fork": None,
        "base_sha": None,
        "changed_paths": [],
        "regression_paths": [],
        "test_ids": [],
        "junit_sha256": None,
    }
    out.update(extra)
    return out


def _git(args: list[str], cwd: Path | None = None, env: dict[str, str] | None = None,
         timeout: int = 120) -> str:
    clean = {
        "PATH": os.environ.get("PATH", ""),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "HOME": tempfile.gettempdir(),
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
    }
    if env:
        clean.update(env)
    p = subprocess.run(["git", "-c", "core.hooksPath=/dev/null", *args],
                       cwd=cwd, env=clean, capture_output=True, text="-z" not in args,
                       timeout=timeout)
    if p.returncode:
        raise FixError(f"git {args[0]} failed: {os.fsdecode(p.stderr or p.stdout)[:240]}")
    output = os.fsdecode(p.stdout)
    return output if "-z" in args else output.strip()


def _git_paths(args: list[str], tree: Path) -> set[str]:
    """Read literal Git paths without display quoting or line/space loss."""
    return {path for path in _git([args[0], "-z", *args[1:]], tree).split("\0") if path}


def _changed_paths(tree: Path) -> set[str]:
    return (_git_paths(["diff", "--name-only", "HEAD", "--"], tree)
            | _git_paths(["ls-files", "--others", "--exclude-standard"], tree))


def _safe_path(raw: object) -> str:
    if not isinstance(raw, str) or not raw or raw.startswith(("/", "\\")) \
            or "\\" in raw or ".." in Path(raw).parts \
            or any(ord(c) < 0x20 for c in raw):
        raise FixError("model returned an unsafe patch path")
    if any(part.casefold().rstrip(" .") == ".git" for part in Path(raw).parts):
        raise FixError("repository metadata cannot be patched")
    return raw


def _write_file(tree: Path, rel: str, content: str) -> None:
    target = tree / rel
    if target.is_symlink():
        raise FixError(f"refusing symlink patch path {rel!r}")
    resolved = target.resolve()
    if not str(resolved).startswith(str(tree.resolve()) + os.sep):
        raise FixError(f"patch path escapes tree: {rel!r}")
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(content, encoding="utf-8")


def _parse_patch(raw: str, sources: dict[str, str] | None = None) -> tuple[dict[str, str], set[str]]:
    def unique_keys(pairs):
        obj = {}
        for key, value in pairs:
            if key in obj:
                raise ValueError(f"duplicate JSON key {key!r}")
            obj[key] = value
        return obj

    # Remove only explicit OUTER wrappers. Searching the entire response for
    # reasoning tags also edits literal source strings inside valid JSON.
    text = raw.strip()
    preamble = re.match(r"<think\b[^>]*>.*?</think\s*>", text, flags=re.DOTALL | re.IGNORECASE)
    if preamble:
        text = text[preamble.end():].strip()
    fence = re.fullmatch(r"```(?:json)?[ \t]*\r?\n(.*)\r?\n```[ \t]*",
                         text, flags=re.DOTALL | re.IGNORECASE)
    if fence:
        text = fence.group(1)
    # Complete decoding rejects trailing/multiple envelopes, incomplete
    # wrappers and arbitrary prose instead of guessing at embedded objects.
    obj = json.loads(text, object_pairs_hook=unique_keys)
    return _validate_patch(obj, sources)


def _validate_patch(obj: object, sources: dict[str, str] | None = None) -> tuple[dict[str, str], set[str]]:
    """Validate parsed patch data without treating source strings as model prose."""
    if not isinstance(obj, dict):
        raise FixError("model patch must be an object")
    rows = obj.get("files")
    if not isinstance(rows, list) or len(rows) < 2 or len(rows) > 24:
        raise FixError("model patch must contain 2-24 files")
    files: dict[str, str] = {}
    regressions: set[str] = set()
    for row in rows:
        if not isinstance(row, dict) or set(row) not in (
                {"path", "content", "regression"}, {"path", "edits", "regression"}):
            raise FixError("model patch row has an invalid shape")
        path = _safe_path(row["path"])
        if "edits" in row:
            edits = row["edits"]
            if (sources is None or path not in sources or not isinstance(edits, list)
                    or not 1 <= len(edits) <= 64):
                raise FixError("model edits require a supplied source and 1-64 replacements")
            content = sources[path]
            for edit in edits:
                if (not isinstance(edit, dict) or set(edit) != {"old", "new"}
                        or not isinstance(edit["old"], str) or not edit["old"]
                        or not isinstance(edit["new"], str)
                        or content.find(edit["old"]) < 0
                        or content.find(edit["old"]) != content.rfind(edit["old"])):
                    raise FixError("model edit must match exactly one current source fragment")
                content = content.replace(edit["old"], edit["new"], 1)
                if len(content.encode()) > 1_000_000:
                    raise FixError(f"model patch file is too large: {path}")
        else:
            content = row["content"]
        regression = row["regression"]
        if path in files or not isinstance(content, str) or not isinstance(regression, bool):
            raise FixError("model patch contains a duplicate or malformed file")
        if len(content.encode()) > 1_000_000:
            raise FixError(f"model patch file is too large: {path}")
        files[path] = content
        if regression:
            regressions.add(path)
    if not regressions or regressions == set(files):
        raise FixError("patch must contain regression and implementation files")
    return files, regressions


def _model_patch(config: RepoConfig, reviewed_head: str,
                 findings: list[dict], tree: Path) -> tuple[dict[str, str], set[str]]:
    from .analyzer import _call_model
    from .law import SYSTEM_PROMPT_ADDENDUM

    bounded = json.dumps(findings, sort_keys=True)
    if len(bounded.encode()) > 256_000:
        raise FixError("findings exceed the bounded model prompt")
    sources: dict[str, str] = {}
    source_bytes = 0
    paths = set()
    for finding in findings:
        if not isinstance(finding, dict) or "path" not in finding:
            raise FixError("every finding must name a source path")
        paths.add(_safe_path(finding["path"]))
    # README may contain machine-checked suite counts or execution instructions.
    readme = tree / "README.md"
    if readme.is_file() and not readme.is_symlink():
        paths.add("README.md")
    for path in sorted(paths):
        target = tree / path
        if target.is_symlink() or not target.is_file():
            raise FixError(f"finding source is not a regular file: {path}")
        resolved = target.resolve()
        if not str(resolved).startswith(str(tree.resolve()) + os.sep):
            raise FixError(f"finding source escapes repository: {path}")
        try:
            content = resolved.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise FixError(f"finding source is unreadable text: {path}") from exc
        source_bytes += len(content.encode())
        if source_bytes > 512_000:
            raise FixError("finding sources exceed the bounded model prompt")
        sources[path] = content
    prompt = json.dumps({
        "repo": config.repo,
        "reviewed_head": reviewed_head,
        "findings": findings,
        "sources": sources,
        "repository_laws": config.review,
        "instruction": "Use exact old/new replacements for supplied existing files; complete contents for new regression files.",
        "verification": "Preserve existing tests and their assertions. If adding tests changes machine-checked README counts, update those counts as part of the repair.",
    }, sort_keys=True)
    system = (
        "You are a bounded code repairer. Return JSON only: "
        '{"files":[{"path":"existing.py","edits":[{"old":"exact unique source fragment",'
        '"new":"replacement"}],"regression":false},'
        '{"path":"tests/test_fix.py","content":"complete new test file contents",'
        '"regression":true}]}. Each row has exactly three keys: path, regression, '
        'and either edits or content. Never add other keys or combine edits and content. '
        'Use edits for ALL supplied existing files, including README; use content only '
        'for new files. The regression value must be a JSON boolean. '
        'Edits apply sequentially and each old fragment must match exactly once. '
        'Include multiple files, at least one regression '
        "test and at least one implementation file. Never return commands, diffs, "
        "symlinks, binary data, deletions, or paths outside the repository.\n\n"
        + SYSTEM_PROMPT_ADDENDUM
    )
    return _parse_patch(_call_model(config.model, prompt, system=system), sources)


def _clone_at(source: Path, destination: Path, sha: str) -> None:
    _git(["clone", "--no-local", "--no-checkout", "--", str(source.resolve()),
          str(destination)])
    _git(["checkout", "--detach", sha], destination)
    if _git(["rev-parse", "HEAD"], destination) != sha:
        raise FixError("hardened clone did not resolve the reviewed SHA")


def _tree_hash(tree: Path) -> str:
    return _git(["write-tree"], tree)


def _assert_index_matches_worktree(tree: Path, paths: set[str]) -> None:
    for path in sorted(paths):
        raw_blob = _git(["hash-object", "--no-filters", "--", path], tree)
        staged_blob = _git(["rev-parse", ":" + path], tree)
        if raw_blob != staged_blob:
            raise FixError("staged source bytes differ from the tested worktree")


def _source_hashes(tree: Path, paths: set[str]) -> dict[str, str]:
    result = {}
    for path in sorted(paths):
        target = tree / path
        if target.is_symlink() or not target.is_file():
            raise FixError("tested source is missing or is a symlink")
        result[path] = hashlib.sha256(target.read_bytes()).hexdigest()
    return result


def _verify_unchanged(command, tree, junit, timeout, verify_suite, paths):
    before = _source_hashes(tree, paths)
    try:
        return verify_suite(command, tree, junit, timeout)
    finally:
        if _source_hashes(tree, paths) != before:
            raise FixError("test execution mutated source or regression files")


def _prove_patch(source: Path, reviewed_head: str, files: dict[str, str],
                 regressions: set[str], test_command: list[str], evidence_dir: Path,
                 verify_suite: Callable, test_timeout: int = 1800, *,
                 work_root: Path) -> tuple[Path, set[str], str, set[str]]:
    # The caller owns this workspace through publication and always removes it.
    baseline, pin_only, fixed = (work_root / n for n in ("baseline", "pin-only", "fixed"))
    for tree in (baseline, pin_only, fixed):
        _clone_at(source, tree, reviewed_head)
    tracked = _git_paths(["ls-files"], baseline)
    evidence_dir.mkdir(parents=True, exist_ok=True)
    baseline_ids, _ = _verify_unchanged(test_command, baseline,
                                       evidence_dir / "baseline.xml", test_timeout, verify_suite, tracked)
    for path in regressions:
        _write_file(pin_only, path, files[path])
    try:
        _verify_unchanged(test_command, pin_only, evidence_dir / "regression-red.xml", test_timeout,
                          verify_suite, tracked | regressions)
    except Exception as exc:
        from .exhaustive import NonGreen, _junit

        if not isinstance(exc, NonGreen):
            raise FixError("regression proof unavailable; runner failure is not a red pin") from exc
        red_path = evidence_dir / "regression-red.xml"
        red_ids, green, _ = _junit(red_path)
        root = ET.fromstring(red_path.read_bytes())
        if green or not root.findall(".//testcase/failure") or root.findall(".//testcase/error") \
                or root.findall(".//testcase/skipped") or not set(baseline_ids).issubset(red_ids):
            raise FixError("regression proof needs assertion failures and the complete baseline")
    else:
        raise FixError("regression pins did not fail on the original source")
    for path, content in files.items():
        _write_file(fixed, path, content)
    test_ids, junit_hash = _verify_unchanged(test_command, fixed,
                                           evidence_dir / "fixed-green.xml", test_timeout,
                                           verify_suite, tracked | set(files))
    baseline_ids, test_ids = set(baseline_ids), set(test_ids)
    if not baseline_ids or not baseline_ids.issubset(test_ids):
        raise FixError("full test baseline was not preserved")
    return fixed, test_ids, junit_hash, baseline_ids


def _primary(config: RepoConfig) -> tuple[str, ForgeBinding]:
    return next((name, binding) for name, binding in config.forges.items()
                if binding.role == "primary")


def _is_github(binding: ForgeBinding) -> bool:
    return urllib.parse.urlsplit(binding.api_base).hostname in {"api.github.com", "github.com"}


@contextlib.contextmanager
def _credential(config: RepoConfig, binding: ForgeBinding) -> Iterator[str]:
    """Acquire late and restore process state on every exit path."""
    if _is_github(binding):
        from .appauth import get_repository_token
        token = get_repository_token(config.repo, config.bot_login)
    else:
        token = os.environ.get(binding.token_env, "")
    if not token or token != token.strip() or any(ord(c) < 0x20 for c in token):
        raise FixError("configured forge credential is unavailable or unusable")
    old = os.environ.pop(binding.token_env, None)
    try:
        yield token
    finally:
        if old is not None:
            os.environ[binding.token_env] = old


class _Forge:
    def __init__(self, binding: ForgeBinding, token: str):
        self.binding, self.token = binding, token
        self.base = binding.api_base.rstrip("/")
        self.github = _is_github(binding)

    def call(self, method: str, path: str, data: dict | None = None) -> Any:
        headers = {"Accept": "application/vnd.github+json" if self.github else "application/json",
                   "Authorization": ("Bearer " if self.github else "token ") + self.token,
                   "Content-Type": "application/json", "User-Agent": "fl4write-exhaustive-fix/1"}
        req = urllib.request.Request(self.base + path,
            data=json.dumps(data).encode() if data is not None else None,
            headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=30) as response:
                body = response.read().decode()
                return json.loads(body) if body else {}
        except urllib.error.HTTPError as exc:
            raise FixError(f"forge {method} {path}: HTTP {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            raise FixError(f"forge {method} {path}: unavailable") from exc

    def repo(self, repo: str) -> dict:
        row = self.call("GET", f"/repos/{repo}")
        if not isinstance(row, dict):
            raise FixError("forge repository response is malformed")
        return row

    def user(self) -> str:
        if self.github:
            from .appauth import verified_app_login

            return verified_app_login()
        row = self.call("GET", "/user")
        login = row.get("login") if isinstance(row, dict) else None
        if not isinstance(login, str) or not login:
            raise FixError("forge identity response is malformed")
        return login

    def head(self, repo: str, branch: str) -> str:
        quoted = urllib.parse.quote(branch, safe="")
        path = f"/repos/{repo}/commits/{quoted}" if self.github else f"/repos/{repo}/branches/{quoted}"
        row = self.call("GET", path)
        if self.github:
            sha = row.get("sha") if isinstance(row, dict) else None
        else:
            commit = row.get("commit") if isinstance(row, dict) and row.get("name") == branch else None
            sha = commit.get("id") if isinstance(commit, dict) else None
        if not isinstance(sha, str) or not _SHA.fullmatch(sha):
            raise FixError("forge branch head response is malformed")
        return sha

    def pages(self, path: str, field: str | None = None) -> list[dict]:
        rows = []
        for page in range(1, 101):
            param = "per_page" if self.github else "limit"
            value = self.call("GET", path + ("&" if "?" in path else "?")
                              + f"{param}=100&page={page}")
            batch = value.get(field) if field and isinstance(value, dict) else value
            if not isinstance(batch, list) or any(not isinstance(row, dict) for row in batch):
                raise FixError("forge paginated response is malformed")
            rows.extend(batch)
            if len(batch) < 100:
                if field and value.get("total_count") != len(rows):
                    raise FixError("forge paginated enumeration changed or is incomplete")
                return rows
        raise FixError("forge pagination cap reached; enumeration is incomplete")

    def find_pr(self, repo: str, branch: str) -> dict | None:
        owner = repo.split("/", 1)[0]
        head = urllib.parse.quote(f"{owner}:{branch}" if self.github else branch, safe="")
        rows = self.pages(f"/repos/{repo}/pulls?state=open&head={head}")
        matches = [r for r in rows if isinstance(r, dict)
                   and isinstance(r.get("head"), dict)
                   and r["head"].get("ref") == branch]
        if len(matches) > 1:
            raise FixError("multiple open PRs exist for the stable bot branch")
        return matches[0] if matches else None

    def create_pr(self, repo: str, branch: str, base: str, title: str, body: str) -> dict:
        row = self.call("POST", f"/repos/{repo}/pulls",
                        {"head": branch, "base": base, "title": title, "body": body})
        if not isinstance(row, dict):
            raise FixError("forge PR creation response is malformed")
        return row

    def required_contexts(self, repo: str, branch: str) -> set[str]:
        quoted = urllib.parse.quote(branch, safe="")
        info = self.call("GET", f"/repos/{repo}/branches/{quoted}")
        if not isinstance(info, dict) or info.get("name") != branch or type(info.get("protected")) is not bool:
            raise FixError("branch protection policy is unqueryable")
        if not info["protected"]:
            return set()
        if self.github:
            policy = self.call("GET", f"/repos/{repo}/branches/{quoted}/protection/required_status_checks")
            if not isinstance(policy, dict) or not isinstance(policy.get("contexts"), list):
                raise FixError("required status-check policy is unqueryable")
            if any(not isinstance(c, dict) or c.get("app_id") not in (None, -1)
                   for c in policy.get("checks", [])):
                raise FixError("required App-bound checks need independently queryable check-run identity")
            rules = self.call("GET", f"/repos/{repo}/rules/branches/{quoted}")
            if not isinstance(rules, list):
                raise FixError("branch rules are unqueryable")
            contexts = list(policy["contexts"])
            for rule in rules:
                if not isinstance(rule, dict):
                    raise FixError("branch rule is malformed")
                if rule.get("type") == "required_workflows":
                    raise FixError("required workflow policy cannot be established from status contexts")
                if rule.get("type") == "required_status_checks":
                    required = (rule.get("parameters") or {}).get("required_status_checks")
                    if not isinstance(required, list):
                        raise FixError("required branch rule is malformed")
                    for check in required:
                        if not isinstance(check, dict) or check.get("integration_id") is not None:
                            raise FixError("required integration-bound check identity is unqueryable")
                        contexts.append(check.get("context"))
        else:
            if type(info.get("enable_status_check")) is not bool:
                raise FixError("Forgejo status-check policy is unqueryable")
            contexts = info.get("status_check_contexts") if info["enable_status_check"] else []
        if not isinstance(contexts, list) or any(not isinstance(c, str) or not c for c in contexts):
            raise FixError("required status contexts are malformed")
        return set(contexts)

    def checks_green(self, repo: str, sha: str, required: set[str] | None = None) -> bool | None:
        required = required or set()
        if self.github:
            runs = self.pages(f"/repos/{repo}/actions/runs?head_sha={sha}", "workflow_runs")
            status = self.call("GET", f"/repos/{repo}/commits/{sha}/status?per_page=100")
            contexts = status.get("statuses") if isinstance(status, dict) else None
            if not isinstance(contexts, list) or status.get("total_count") != len(contexts):
                return None
            if not runs and not contexts:
                return None
            if any(r.get("head_sha") != sha or r.get("status") != "completed" for r in runs):
                return None
            if contexts and status.get("state") == "pending":
                return None
            successful = {row.get("context") for row in contexts if isinstance(row, dict) and row.get("state") == "success"}
            if required - successful:
                for run in runs:
                    if type(run.get("id")) is not int:
                        return None
                    jobs = self.pages(f"/repos/{repo}/actions/runs/{run['id']}/jobs", "jobs")
                    successful.update(job.get("name") for job in jobs
                                      if job.get("status") == "completed" and job.get("conclusion") == "success")
            if required - successful:
                return None
            return ((not contexts or status.get("state") == "success")
                    and all(r.get("conclusion") == "success" for r in runs))
        rows = self.pages(f"/repos/{repo}/commits/{sha}/statuses")
        if not rows:
            return None
        latest = {}
        for row in rows:
            context, identifier = row.get("context"), row.get("id")
            if not isinstance(context, str) or not context or type(identifier) is not int:
                return None
            if context not in latest or identifier > latest[context]["id"]:
                latest[context] = row
        states = [row.get("status") for row in latest.values()]
        if required - latest.keys():
            return None
        if any(s in ("pending", None) for s in states):
            return None
        return all(s == "success" for s in states)

    def merge(self, repo: str, number: int, sha: str) -> dict:
        if self.github:
            return self.call("PUT", f"/repos/{repo}/pulls/{number}/merge",
                             {"merge_method": "squash", "sha": sha})
        self.call("POST", f"/repos/{repo}/pulls/{number}/merge",
                  {"Do": "squash", "head_commit_id": sha})
        # Forgejo's successful merge response is empty. Prove it through the PR.
        observed = self.call("GET", f"/repos/{repo}/pulls/{number}")
        if (not isinstance(observed, dict) or observed.get("merged") is not True
                or ((observed.get("head") or {}).get("sha") != sha)):
            return {}
        return {"merged": True, "sha": observed.get("merge_commit_sha")}


def _remote_url(binding: ForgeBinding, repo: str) -> str:
    if _is_github(binding):
        return f"https://github.com/{repo}.git"
    parsed = urllib.parse.urlsplit(binding.api_base)
    path = parsed.path.rstrip("/")
    if path.endswith("/api/v1"):
        path = path[:-7]
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc,
                                    f"{path}/{repo}.git", "", ""))


@contextlib.contextmanager
def _askpass(token: str) -> Iterator[dict[str, str]]:
    directory = Path(tempfile.mkdtemp(prefix="fl4write-askpass-"))
    helper = directory / "askpass.sh"
    helper.write_text("#!/bin/sh\nprintf '%s\\n' \"$FL4WRITE_PUSH_TOKEN\"\n", encoding="utf-8")
    helper.chmod(0o500)
    try:
        yield {"GIT_ASKPASS": str(helper), "GIT_TERMINAL_PROMPT": "0",
               "FL4WRITE_PUSH_TOKEN": token}
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def fetch_merged_head(repo: Path, config: RepoConfig, merged_head: str) -> None:
    """Fetch a verified merge from the selected forge with scoped credentials."""
    if not isinstance(merged_head, str) or not _SHA.fullmatch(merged_head):
        raise FixError("verified merge identity is invalid")
    _, binding = _primary(config)
    try:
        with _credential(config, binding) as token, _askpass(token) as env:
            _git(["-c", "credential.helper=", "-c", "http.extraHeader=",
                  "-c", "http.followRedirects=false", "fetch", "--no-tags", "--",
                  _remote_url(binding, config.repo), merged_head], repo, env=env)
    except (FixError, OSError, subprocess.SubprocessError):
        raise FixError("authenticated merged-head fetch unavailable") from None


def _receipt(path: Path, result: dict) -> None:
    path.mkdir(parents=True, exist_ok=True)
    tmp = path / ("." + _RECEIPT + ".tmp")
    tmp.write_text(json.dumps(result, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path / _RECEIPT)


def _prepared_patch(config, reviewed_head, findings, repo, evidence_dir, test_command=None):
    """Reuse the exact model patch on pending-PR retries."""
    path = evidence_dir / "prepared-patch.json"
    request = {"head": reviewed_head, "findings": findings,
               "config": config.model_dump(mode="json"), "test_command": test_command or []}
    request_sha = hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest()
    if path.exists():
        saved = json.loads(path.read_bytes())
        if saved.get("reviewed_head") != reviewed_head or saved.get("request_sha256") != request_sha:
            raise FixError("prepared patch belongs to another repair request")
        return _validate_patch(saved["patch"])
    files, regressions = _model_patch(config, reviewed_head, findings, repo)
    patch = {"files": [{"path": p, "content": text, "regression": p in regressions}
                        for p, text in sorted(files.items())]}
    _validate_patch(patch)
    temporary = evidence_dir / ".prepared-patch.tmp"
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump({"reviewed_head": reviewed_head, "request_sha256": request_sha,
                   "patch": patch}, stream, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.chmod(0o400)
    temporary.replace(path)
    return files, regressions


def _recover_merge(forge, config, reviewed_head, commit_sha, author, default, evidence_dir):
    receipt = evidence_dir / _RECEIPT
    if not receipt.exists():
        return None
    saved = json.loads(receipt.read_bytes())
    if saved.get("phase") not in {"merging", "merged"}:
        return None
    if (saved.get("reviewed_head") != reviewed_head or saved.get("commit_sha") != commit_sha
            or saved.get("author") != author
            or saved.get("base_branch", saved.get("default_branch")) != default
            or type(saved.get("pr_number")) is not int or saved["pr_number"] <= 0):
        raise FixError("merge recovery receipt does not match the proved transaction")
    pr = forge.call("GET", f"/repos/{config.repo}/pulls/{saved['pr_number']}")
    if (not isinstance(pr, dict) or ((pr.get("user") or {}).get("login") != author)
            or ((pr.get("head") or {}).get("sha") != commit_sha)
            or ((pr.get("base") or {}).get("ref") != default)
            or (((pr.get("base") or {}).get("repo") or {}).get("full_name") != config.repo)
            or (((pr.get("head") or {}).get("repo") or {}).get("full_name") != config.repo)):
        raise FixError("merge recovery PR identity changed")
    if pr.get("merged") is not True:
        if pr.get("state") == "open":
            return None
        raise FixError("proved PR closed without a merge")
    merged = pr.get("merge_commit_sha")
    if not isinstance(merged, str) or not _SHA.fullmatch(merged):
        raise FixError("merge recovery has no verified merge commit")
    current = forge.head(config.repo, default)
    if current != merged:
        comparison = forge.call("GET", f"/repos/{config.repo}/compare/{merged}...{current}")
        if (not isinstance(comparison, dict) or comparison.get("status") != "ahead"
                or ((comparison.get("merge_base_commit") or {}).get("sha") != merged)):
            raise FixError("default branch no longer contains the proved merge")
    if forge.head(config.repo, default) != current:
        raise FixError("default branch changed during merge recovery")
    result = {**saved, "status": "merged", "phase": "merged",
              "reason": "recovered verified bot-owned merge", "merge_commit_sha": merged,
              "merged_head": current}
    _receipt(evidence_dir, result)
    return result


def attempt_fix_with_regression_pin(
    repo: Path,
    config: RepoConfig,
    reviewed_head: str,
    findings: list[dict],
    test_command: list[str],
    evidence_dir: Path,
    *,
    verify_suite: Callable,
    max_model_calls: int = 1,
    test_timeout: int = 1800,
    base_branch: str | None = None,
) -> dict:
    """Prove, publish, and merge one atomic multi-file repair.

    ``verify_suite(command, tree, junit_path, timeout)`` must return
    ``(set(test_ids), junit_sha256)`` for green or raise for red/unavailable.
    """
    result = _result("error", "uninitialized", reviewed_head)
    workspace = None
    try:
        prior_path = evidence_dir / _RECEIPT
        if prior_path.exists():
            prior = json.loads(prior_path.read_bytes())
            if (isinstance(prior, dict) and prior.get("reviewed_head") == reviewed_head
                    and prior.get("phase") in {"merging", "merged"}):
                result = prior
        if not repo.is_dir() or not _SHA.fullmatch(reviewed_head):
            raise FixError("repo or reviewed_head is invalid")
        if max_model_calls != 1:
            raise FixError("exactly one bounded model call is supported")
        if type(test_timeout) is not int or test_timeout <= 0:
            raise FixError("test timeout must be a positive integer")
        if base_branch is not None:
            if (not isinstance(base_branch, str) or not base_branch or base_branch.startswith("-")
                    or _git(["check-ref-format", "--branch", base_branch], repo) != base_branch):
                raise FixError("selected base branch is invalid")
        if config.shadow or not config.fix.enabled or not config.fix.merge_own_prs:
            return _result("blocked", "fix and own-PR merge capabilities must be enabled", reviewed_head)
        if not findings or not isinstance(test_command, list) or not test_command \
                or any(not isinstance(x, str) or not x or "\x00" in x for x in test_command):
            raise FixError("findings and fixed-argv test command are required")
        if _git(["rev-parse", "HEAD"], repo) != reviewed_head:
            return _result("pending", "local reviewed HEAD drifted", reviewed_head)
        _, binding = _primary(config)
        evidence_dir.mkdir(parents=True, exist_ok=True)
        files, regressions = _prepared_patch(config, reviewed_head, findings, repo, evidence_dir, test_command)
        workspace = tempfile.TemporaryDirectory(prefix="fl4write-exhaustive-fix-")
        fixed, test_ids, junit_hash, _ = _prove_patch(
            repo, reviewed_head, files, regressions, test_command,
            evidence_dir, verify_suite, test_timeout, work_root=Path(workspace.name))
        changed = _changed_paths(fixed)
        if changed != set(files):
            raise FixError(
                "tested patch paths differ from the structured model patch: "
                f"changed={sorted(changed)!r} expected={sorted(files)!r}")
        _git(["add", "--", *sorted(files)], fixed)
        _assert_index_matches_worktree(fixed, set(files))
        tested_tree = _tree_hash(fixed)
        commit_time = _git(["show", "-s", "--format=%ct", reviewed_head], fixed) + " +0000"
        _git(["-c", "user.name=fl4write bot", "-c", "user.email=fl4write@invalid",
              "commit", "-m", f"fix: exhaustive findings at {reviewed_head[:12]}"], fixed,
             env={"GIT_AUTHOR_DATE": commit_time, "GIT_COMMITTER_DATE": commit_time})
        commit_sha = _git(["rev-parse", "HEAD"], fixed)
        if _git(["rev-parse", "HEAD^{tree}"], fixed) != tested_tree:
            raise FixError("committed tree differs from the tested patch")
        branch = _BRANCH + reviewed_head[:12]
        if base_branch is not None:
            branch += "-" + hashlib.sha256(base_branch.encode()).hexdigest()[:8]
        with _credential(config, binding) as token:
            forge = _Forge(binding, token)
            info = forge.repo(config.repo)
            full_name = info.get("full_name")
            default = info.get("default_branch")
            fork = info.get("fork")
            if full_name != config.repo or fork is not False or not isinstance(default, str) or not default:
                return _result("blocked", "canonical repository identity or non-fork rail failed", reviewed_head,
                               fork=fork)
            default = base_branch or default
            author = forge.user()
            if author != config.bot_login:
                return _result("blocked", "authenticated identity is not the configured bot", reviewed_head,
                               author=author, fork=False)
            recovered = _recover_merge(forge, config, reviewed_head, commit_sha, author, default, evidence_dir)
            if recovered is not None:
                return recovered
            base_sha = forge.head(config.repo, default)
            if base_sha != reviewed_head:
                return _result("pending", "remote selected base HEAD drifted", reviewed_head,
                               author=author, fork=False, base_sha=base_sha,
                               changed_paths=sorted(changed), regression_paths=sorted(regressions),
                               test_ids=sorted(test_ids), junit_sha256=junit_hash)
            _git(["remote", "set-url", "origin", _remote_url(binding, config.repo)], fixed)
            with _askpass(token) as push_env:
                try:
                    _git(["push", "origin", f"{commit_sha}:refs/heads/{branch}"], fixed,
                         env=push_env)
                finally:
                    push_env.clear()
            pr = forge.find_pr(config.repo, branch)
            if pr is None:
                pr = forge.create_pr(config.repo, branch, default,
                    f"fix: exhaustive findings at {reviewed_head[:12]}",
                    "Atomic fix with regression pins. Evidence is retained by the runner.")
            number = pr.get("number") if isinstance(pr, dict) else None
            pr_author = ((pr.get("user") or {}).get("login")
                         if isinstance(pr, dict) and isinstance(pr.get("user"), dict) else None)
            pr_head = ((pr.get("head") or {}).get("sha")
                       if isinstance(pr, dict) and isinstance(pr.get("head"), dict) else None)
            pr_base = ((pr.get("base") or {}).get("sha")
                       if isinstance(pr, dict) and isinstance(pr.get("base"), dict) else None)
            pr_url = pr.get("html_url") if isinstance(pr, dict) else None
            head_repo = (((pr.get("head") or {}).get("repo") or {}).get("full_name")
                         if isinstance(pr, dict) and isinstance(pr.get("head"), dict)
                         and isinstance((pr.get("head") or {}).get("repo"), dict) else None)
            if isinstance(number, bool) or not isinstance(number, int) or number <= 0 \
                    or pr_author != author or pr_head != commit_sha or pr_base != base_sha \
                    or head_repo != config.repo \
                    or ((pr.get("base") or {}).get("ref") != default) \
                    or (((pr.get("base") or {}).get("repo") or {}).get("full_name") != config.repo):
                return _result("blocked", "PR ownership, head, or base rail failed", reviewed_head,
                               pr_number=number, pr_url=pr_url, author=pr_author, fork=False,
                               base_sha=base_sha, changed_paths=sorted(changed),
                               regression_paths=sorted(regressions), test_ids=sorted(test_ids),
                               junit_sha256=junit_hash)
            proof = dict(pr_number=number, pr_url=pr_url, author=author, fork=False,
                         commit_sha=commit_sha, default_branch=info["default_branch"], base_branch=default,
                         base_sha=base_sha, changed_paths=sorted(changed),
                         regression_paths=sorted(regressions), test_ids=sorted(test_ids),
                         junit_sha256=junit_hash)
            required = forge.required_contexts(config.repo, default)
            proof["required_contexts"] = sorted(required)
            ci = forge.checks_green(config.repo, commit_sha, required)
            if ci is None:
                result = _result("pending", "required checks are pending or unqueryable",
                                 reviewed_head, **proof)
                _receipt(evidence_dir, result)
                return result
            if ci is False:
                result = _result("blocked", "required checks failed", reviewed_head, **proof)
                _receipt(evidence_dir, result)
                return result
            # Re-read every mutable authority field immediately before merge.
            if forge.head(config.repo, default) != base_sha:
                result = _result("pending", "selected base branch drifted before merge",
                                 reviewed_head, **proof)
                _receipt(evidence_dir, result)
                return result
            if (forge.required_contexts(config.repo, default) != required
                    or forge.checks_green(config.repo, commit_sha, required) is not True):
                result = _result("pending", "required-check evidence changed before merge", reviewed_head, **proof)
                _receipt(evidence_dir, result)
                return result
            current = forge.find_pr(config.repo, branch)
            if not current or ((current.get("user") or {}).get("login") != author) \
                    or ((current.get("head") or {}).get("sha") != commit_sha) \
                    or ((current.get("base") or {}).get("sha") != base_sha) \
                    or ((current.get("base") or {}).get("ref") != default) \
                    or (((current.get("base") or {}).get("repo") or {}).get("full_name") != config.repo) \
                    or (((current.get("head") or {}).get("repo") or {}).get("full_name") != config.repo):
                return _result("blocked", "PR changed before merge", reviewed_head, **proof)
            proof["phase"] = "merging"
            result = _result("pending", "merge requested; outcome requires verification", reviewed_head, **proof)
            _receipt(evidence_dir, result)
            merged = forge.merge(config.repo, number, commit_sha)
            merge_sha = merged.get("sha") if isinstance(merged, dict) else None
            if not isinstance(merged, dict) or merged.get("merged") is not True \
                    or not isinstance(merge_sha, str) or not _SHA.fullmatch(merge_sha):
                result = _result("pending", "merge outcome was not proven", reviewed_head, **proof)
                _receipt(evidence_dir, result)
                return result
            merged_head = forge.head(config.repo, default)
            if merged_head != merge_sha or merged_head == base_sha:
                result = _result("pending", "remote default did not prove the merge SHA",
                                 reviewed_head, **proof)
                _receipt(evidence_dir, result)
                return result
            result = _result("merged", "verified bot-owned PR merge", reviewed_head,
                             merged_head=merged_head, **proof)
            result["phase"] = "merged"
            _receipt(evidence_dir, result)
            return result
    except Exception as exc:  # containment: credentials are already unwound
        result = {**result, "status": "pending" if result.get("phase") == "merging" else "error",
                  "reason": str(exc)[:300]}
        try:
            _receipt(evidence_dir, result)
        except OSError:
            pass
        return result
    finally:
        if workspace is not None:
            workspace.cleanup()


__all__ = ["attempt_fix_with_regression_pin"]
