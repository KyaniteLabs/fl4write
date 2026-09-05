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
                       cwd=cwd, env=clean, capture_output=True, text=True,
                       timeout=timeout)
    if p.returncode:
        raise FixError(f"git {args[0]} failed: {(p.stderr or p.stdout)[:240]}")
    return p.stdout.strip()


def _safe_path(raw: object) -> str:
    if not isinstance(raw, str) or not raw or raw.startswith(("/", "\\")) \
            or "\\" in raw or ".." in Path(raw).parts \
            or any(ord(c) < 0x20 for c in raw):
        raise FixError("model returned an unsafe patch path")
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


def _parse_patch(raw: str) -> tuple[dict[str, str], set[str]]:
    from .analyzer import extract_json

    obj = extract_json(raw, envelope_key="files")
    rows = obj.get("files")
    if not isinstance(rows, list) or len(rows) < 2 or len(rows) > 24:
        raise FixError("model patch must contain 2-24 files")
    files: dict[str, str] = {}
    regressions: set[str] = set()
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"path", "content", "regression"}:
            raise FixError("model patch row has an invalid shape")
        path = _safe_path(row["path"])
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
    for finding in findings:
        if not isinstance(finding, dict) or "path" not in finding:
            raise FixError("every finding must name a source path")
        path = _safe_path(finding["path"])
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
        "instruction": "Return complete contents for every changed file, including regression tests.",
    }, sort_keys=True)
    system = (
        "You are a bounded code repairer. Return JSON only: "
        '{"files":[{"path":"relative/path","content":"complete file contents",'
        '"regression":true}]}. Include multiple files, at least one regression '
        "test and at least one implementation file. Never return commands, diffs, "
        "symlinks, binary data, deletions, or paths outside the repository.\n\n"
        + SYSTEM_PROMPT_ADDENDUM
    )
    return _parse_patch(_call_model(config.model, prompt, system=system))


def _clone_at(source: Path, destination: Path, sha: str) -> None:
    _git(["clone", "--no-local", "--no-checkout", "--", str(source.resolve()),
          str(destination)])
    _git(["checkout", "--detach", sha], destination)
    if _git(["rev-parse", "HEAD"], destination) != sha:
        raise FixError("hardened clone did not resolve the reviewed SHA")


def _tree_hash(tree: Path) -> str:
    return _git(["write-tree"], tree)


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
                 verify_suite: Callable) -> tuple[Path, set[str], str, set[str]]:
    work_root = Path(tempfile.mkdtemp(prefix="fl4write-exhaustive-fix-"))
    baseline, pin_only, fixed = (work_root / n for n in ("baseline", "pin-only", "fixed"))
    for tree in (baseline, pin_only, fixed):
        _clone_at(source, tree, reviewed_head)
    tracked = set(_git(["ls-files"], baseline).splitlines())
    evidence_dir.mkdir(parents=True, exist_ok=True)
    baseline_ids, _ = _verify_unchanged(test_command, baseline,
                                       evidence_dir / "baseline.xml", 1800, verify_suite, tracked)
    for path in regressions:
        _write_file(pin_only, path, files[path])
    try:
        _verify_unchanged(test_command, pin_only, evidence_dir / "regression-red.xml", 1800,
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
                                           evidence_dir / "fixed-green.xml", 1800,
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
        from .appauth import get_installation_token
        token = get_installation_token(config.repo)
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
        row = self.call("GET", "/user")
        login = row.get("login") if isinstance(row, dict) else None
        if not isinstance(login, str) or not login:
            raise FixError("forge identity response is malformed")
        return login

    def head(self, repo: str, branch: str) -> str:
        quoted = urllib.parse.quote(branch, safe="")
        path = f"/repos/{repo}/commits/{quoted}"
        row = self.call("GET", path)
        sha = row.get("sha") if isinstance(row, dict) else None
        if not isinstance(sha, str) or not _SHA.fullmatch(sha):
            raise FixError("forge branch head response is malformed")
        return sha

    def find_pr(self, repo: str, branch: str) -> dict | None:
        owner = repo.split("/", 1)[0]
        head = urllib.parse.quote(f"{owner}:{branch}" if self.github else branch, safe="")
        rows = self.call("GET", f"/repos/{repo}/pulls?state=open&head={head}&limit=50&per_page=50")
        if not isinstance(rows, list):
            raise FixError("forge PR lookup response is malformed")
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

    def checks_green(self, repo: str, sha: str) -> bool | None:
        if self.github:
            checks = self.call("GET", f"/repos/{repo}/commits/{sha}/check-runs?per_page=100")
            status = self.call("GET", f"/repos/{repo}/commits/{sha}/status")
            runs = checks.get("check_runs") if isinstance(checks, dict) else None
            if not isinstance(runs, list) or not isinstance(status, dict):
                return None
            if not runs or status.get("state") == "pending" or any(
                    not isinstance(r, dict) or r.get("status") != "completed" for r in runs):
                return None
            return status.get("state") == "success" and all(
                r.get("conclusion") in ("success", "neutral", "skipped") for r in runs)
        rows = self.call("GET", f"/repos/{repo}/commits/{sha}/statuses?limit=100")
        if not isinstance(rows, list) or not rows:
            return None
        states = [r.get("status") if isinstance(r, dict) else None for r in rows]
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


def _receipt(path: Path, result: dict) -> None:
    path.mkdir(parents=True, exist_ok=True)
    tmp = path / ("." + _RECEIPT + ".tmp")
    tmp.write_text(json.dumps(result, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path / _RECEIPT)


def _prepared_patch(config, reviewed_head, findings, repo, evidence_dir):
    """Reuse the exact model patch on pending-PR retries."""
    path = evidence_dir / "prepared-patch.json"
    if path.exists():
        saved = json.loads(path.read_bytes())
        if saved.get("reviewed_head") != reviewed_head:
            raise FixError("prepared patch belongs to another reviewed HEAD")
        return _parse_patch(json.dumps(saved["patch"]))
    files, regressions = _model_patch(config, reviewed_head, findings, repo)
    patch = {"files": [{"path": p, "content": text, "regression": p in regressions}
                        for p, text in sorted(files.items())]}
    _parse_patch(json.dumps(patch))
    temporary = evidence_dir / ".prepared-patch.tmp"
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump({"reviewed_head": reviewed_head, "patch": patch}, stream, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.chmod(0o400)
    temporary.replace(path)
    return files, regressions


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
) -> dict:
    """Prove, publish, and merge one atomic multi-file repair.

    ``verify_suite(command, tree, junit_path, timeout)`` must return
    ``(set(test_ids), junit_sha256)`` for green or raise for red/unavailable.
    """
    result = _result("error", "uninitialized", reviewed_head)
    try:
        if not repo.is_dir() or not _SHA.fullmatch(reviewed_head):
            raise FixError("repo or reviewed_head is invalid")
        if max_model_calls != 1:
            raise FixError("exactly one bounded model call is supported")
        if config.shadow or not config.fix.enabled or not config.fix.merge_own_prs:
            return _result("blocked", "fix and own-PR merge capabilities must be enabled", reviewed_head)
        if not findings or not isinstance(test_command, list) or not test_command \
                or any(not isinstance(x, str) or not x or "\x00" in x for x in test_command):
            raise FixError("findings and fixed-argv test command are required")
        if _git(["rev-parse", "HEAD"], repo) != reviewed_head:
            return _result("pending", "local reviewed HEAD drifted", reviewed_head)
        _, binding = _primary(config)
        evidence_dir.mkdir(parents=True, exist_ok=True)
        files, regressions = _prepared_patch(config, reviewed_head, findings, repo, evidence_dir)
        fixed, test_ids, junit_hash, _ = _prove_patch(
            repo, reviewed_head, files, regressions, test_command,
            evidence_dir, verify_suite)
        changed = set(_git(["diff", "--name-only", "HEAD", "--"], fixed).splitlines())
        changed.update(_git(["ls-files", "--others", "--exclude-standard"], fixed).splitlines())
        if changed != set(files):
            raise FixError(
                "tested patch paths differ from the structured model patch: "
                f"changed={sorted(changed)!r} expected={sorted(files)!r}")
        _git(["add", "--", *sorted(files)], fixed)
        tested_tree = _tree_hash(fixed)
        commit_time = _git(["show", "-s", "--format=%ct", reviewed_head], fixed) + " +0000"
        _git(["-c", "user.name=fl4write bot", "-c", "user.email=fl4write@invalid",
              "commit", "-m", f"fix: exhaustive findings at {reviewed_head[:12]}"], fixed,
             env={"GIT_AUTHOR_DATE": commit_time, "GIT_COMMITTER_DATE": commit_time})
        commit_sha = _git(["rev-parse", "HEAD"], fixed)
        if _git(["rev-parse", "HEAD^{tree}"], fixed) != tested_tree:
            raise FixError("committed tree differs from the tested patch")
        branch = _BRANCH + reviewed_head[:12]
        with _credential(config, binding) as token:
            forge = _Forge(binding, token)
            info = forge.repo(config.repo)
            full_name = info.get("full_name")
            default = info.get("default_branch")
            fork = info.get("fork")
            if full_name != config.repo or fork is not False or not isinstance(default, str) or not default:
                return _result("blocked", "canonical repository identity or non-fork rail failed", reviewed_head,
                               fork=fork)
            author = forge.user()
            if author != config.bot_login:
                return _result("blocked", "authenticated identity is not the configured bot", reviewed_head,
                               author=author, fork=False)
            base_sha = forge.head(config.repo, default)
            if base_sha != reviewed_head:
                return _result("pending", "remote default HEAD drifted", reviewed_head,
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
                    or head_repo not in (None, config.repo):
                return _result("blocked", "PR ownership, head, or base rail failed", reviewed_head,
                               pr_number=number, pr_url=pr_url, author=pr_author, fork=False,
                               base_sha=base_sha, changed_paths=sorted(changed),
                               regression_paths=sorted(regressions), test_ids=sorted(test_ids),
                               junit_sha256=junit_hash)
            proof = dict(pr_number=number, pr_url=pr_url, author=author, fork=False,
                         base_sha=base_sha, changed_paths=sorted(changed),
                         regression_paths=sorted(regressions), test_ids=sorted(test_ids),
                         junit_sha256=junit_hash)
            ci = forge.checks_green(config.repo, commit_sha)
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
                result = _result("pending", "default branch drifted before merge",
                                 reviewed_head, **proof)
                _receipt(evidence_dir, result)
                return result
            current = forge.find_pr(config.repo, branch)
            if not current or ((current.get("user") or {}).get("login") != author) \
                    or ((current.get("head") or {}).get("sha") != commit_sha) \
                    or ((current.get("base") or {}).get("sha") != base_sha) \
                    or (isinstance((current.get("head") or {}).get("repo"), dict)
                        and ((current.get("head") or {}).get("repo") or {}).get("full_name")
                        != config.repo):
                return _result("blocked", "PR changed before merge", reviewed_head, **proof)
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
            _receipt(evidence_dir, result)
            return result
    except Exception as exc:  # containment: credentials are already unwound
        result = _result("error", str(exc)[:300], reviewed_head)
        try:
            _receipt(evidence_dir, result)
        except OSError:
            pass
        return result


__all__ = ["attempt_fix_with_regression_pin"]
