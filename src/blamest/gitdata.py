"""Everything that comes from the local git history: commits, line stats and code churn."""

from __future__ import annotations

import re
import subprocess
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from .config import Config

_SEP = "\x1f"
_REC = "\x1e"
_GIT = ["git", "-c", "core.quotePath=false", "-c", "diff.noprefix=false", "-c", "diff.mnemonicPrefix=false"]
_SHA = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")
_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+\d+(?:,\d+)? @@")
_BRACE_RENAME = re.compile(r"\{([^{}]*) => ([^{}]*)\}")
_MERGE_PR = re.compile(r"^Merge pull request #(\d+)")
_SQUASH_PR = re.compile(r"\(#(\d+)\)\s*$")


class GitError(RuntimeError):
    pass


def git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        [*_GIT, "-C", str(repo), *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if proc.returncode != 0:
        raise GitError(f"git {' '.join(args[:3])} failed: {proc.stderr.strip()}")
    return proc.stdout


@dataclass
class FileStat:
    path: str
    added: int
    deleted: int


@dataclass
class Commit:
    sha: str
    parents: list[str]
    name: str
    email: str
    time: int          # author time, unix seconds
    tz_minutes: int    # author's UTC offset, in minutes
    subject: str
    files: list[FileStat] = field(default_factory=list)

    @property
    def pr_number(self) -> int | None:
        """The PR this commit landed through, from GitHub's merge / squash commit message."""
        m = _MERGE_PR.match(self.subject) if len(self.parents) > 1 else _SQUASH_PR.search(self.subject)
        return int(m.group(1)) if m else None

    @property
    def local_time(self) -> int:
        return self.time + self.tz_minutes * 60


@dataclass
class ChurnLine:
    """`lines` lines written by `email` at `orig_time` were rewritten by a later commit."""

    email: str
    orig_time: int
    lines: int


def repo_root(path: Path) -> Path:
    return Path(git(path, "rev-parse", "--show-toplevel").strip())


def resolve_branch(repo: Path, branch: str) -> str:
    """Prefer the remote-tracking ref (closest to what GitHub sees), fall back to the local branch."""
    for ref in (f"refs/remotes/origin/{branch}", f"refs/heads/{branch}"):
        try:
            git(repo, "rev-parse", "--verify", "--quiet", ref)
            return ref
        except GitError:
            continue
    raise GitError(f"Branch '{branch}' not found locally (tried origin/{branch} and {branch}). Try `git fetch`.")


def _parse_tz(offset: str) -> int:
    sign = -1 if offset.startswith("-") else 1
    digits = offset.lstrip("+-")
    return sign * (int(digits[:2]) * 60 + int(digits[2:4]))


def _rename_target(path: str) -> str:
    if "=>" not in path:
        return path
    if "{" in path:
        return _BRACE_RENAME.sub(lambda m: m.group(2), path).replace("//", "/").strip("/")
    return path.split(" => ", 1)[1]


def read_commits(repo: Path, ref: str, landed: bool = False) -> list[Commit]:
    """Commits on `ref`, newest first, with per-file line stats. Uses .mailmap for names/emails.

    landed=False: every non-merge commit reachable from `ref`, i.e. every commit someone authored,
                  including the individual commits of PRs that were merged with a merge commit.
    landed=True:  only the first-parent history, i.e. each change as it landed on the branch (a squash
                  commit, a merge commit diffed against the branch, or a direct/rebased commit).
    """
    fmt = _REC + _SEP.join(["%H", "%P", "%aN", "%aE", "%at", "%ai", "%s"])
    mode = ["--first-parent", "--diff-merges=first-parent"] if landed else ["--no-merges"]
    out = git(repo, "log", ref, *mode, "-M", "--numstat", f"--format={fmt}")
    commits: list[Commit] = []
    for record in out.split(_REC)[1:]:
        header, _, body = record.partition("\n")
        sha, parents, name, email, at, ai, subject = header.split(_SEP, 6)
        commit = Commit(
            sha=sha,
            parents=parents.split(),
            name=name,
            email=email.lower(),
            time=int(at),
            tz_minutes=_parse_tz(ai.rsplit(" ", 1)[-1]),
            subject=subject,
        )
        for line in body.splitlines():
            parts = line.split("\t", 2)
            if len(parts) != 3:
                continue
            added, deleted, path = parts
            commit.files.append(FileStat(
                path=_rename_target(path),
                added=int(added) if added.isdigit() else 0,
                deleted=int(deleted) if deleted.isdigit() else 0,
            ))
        commits.append(commit)
    return commits


def expand_unattributed_merges(repo: Path, landed: list[Commit], authored: list[Commit]) -> list[Commit]:
    """Replace merge commits that aren't GitHub PR merges (e.g. a local "Merge branch 'main' into x" that
    ended up on the first-parent line) with the individual commits they brought in. Diffing such a merge
    against its first parent would credit everyone else's work to whoever ran the merge."""
    by_sha = {c.sha: c for c in authored}
    seen = {c.sha for c in landed}
    result: list[Commit] = []
    for c in landed:
        if len(c.parents) < 2 or c.pr_number:
            result.append(c)
            continue
        for sha in git(repo, "rev-list", "--no-merges", c.sha, *(f"^{p}" for p in c.parents[:1])).split():
            if sha in by_sha and sha not in seen:
                seen.add(sha)
                result.append(by_sha[sha])
    return result


def _removed_ranges(diff: str) -> dict[str, list[tuple[int, int]]]:
    """Parse a -U0 diff into {old_path: [(start, end), ...]} for every removed/modified line range."""
    ranges: dict[str, list[tuple[int, int]]] = {}
    old_path: str | None = None
    for line in diff.splitlines():
        if line.startswith("--- "):
            target = line[4:].rstrip("\t")
            old_path = target[2:] if target.startswith("a/") else None
        elif line.startswith("@@") and old_path:
            m = _HUNK.match(line)
            if not m:
                continue
            start, count = int(m.group(1)), int(m.group(2) or "1")
            if count > 0:
                ranges.setdefault(old_path, []).append((start, start + count - 1))
    return ranges


def _parse_blame(out: str) -> Counter[tuple[str, int]]:
    info: dict[str, dict[str, str]] = {}
    counts: Counter[str] = Counter()
    current = ""
    for line in out.splitlines():
        if line.startswith("\t"):
            counts[current] += 1
            continue
        head, _, rest = line.partition(" ")
        if _SHA.fullmatch(head) and rest and rest.replace(" ", "").isdigit():
            current = head
            info.setdefault(current, {})
        elif head == "author-mail":
            info[current]["email"] = rest.strip("<>").lower()
        elif head == "author-time":
            info[current]["time"] = rest
    result: Counter[tuple[str, int]] = Counter()
    for sha, n in counts.items():
        meta = info.get(sha, {})
        if "email" in meta and "time" in meta:
            result[(meta["email"], int(meta["time"]))] += n
    return result


def churn_for_commit(repo: Path, commit: Commit, cfg: Config) -> list[ChurnLine]:
    """Who originally wrote each line that `commit` removed or modified (whitespace-only changes ignored)."""
    if not commit.parents:
        return []
    parent = commit.parents[0]
    diff = git(
        repo, "diff", "-U0", "-w", "-M", "--no-color", "--no-ext-diff",
        "--src-prefix=a/", "--dst-prefix=b/", parent, commit.sha,
    )
    totals: Counter[tuple[str, int]] = Counter()
    for path, ranges in _removed_ranges(diff).items():
        if cfg.is_ignored(path):
            continue
        for i in range(0, len(ranges), 200):  # keep argv bounded
            args = [arg for a, b in ranges[i:i + 200] for arg in ("-L", f"{a},{b}")]
            try:
                out = git(repo, "blame", "--porcelain", "-w", *args, parent, "--", path)
            except GitError:
                continue  # submodules, binary oddities, etc.
            totals.update(_parse_blame(out))
    return [ChurnLine(email, t, n) for (email, t), n in totals.items()]
