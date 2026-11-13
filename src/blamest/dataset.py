"""Collects git + GitHub data into one Dataset, with an SQLite cache so re-runs only fetch what changed."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

from .config import Config
from .gitdata import (
    ChurnLine, Commit, churn_for_commit, expand_unattributed_merges, git, read_commits, resolve_branch,
)
from .github import GitHub, RepoInfo, login_from_noreply

# Bump when the cached data's shape or meaning changes; a mismatch wipes the cache once.
SCHEMA_VERSION = "2"  # note: churn is also reset whenever the ignore list changes

# report(stage, done, total) — total is None when unknown
Progress = Callable[[str, int, "int | None"], None]


def _noop(stage: str, done: int, total: int | None) -> None:
    pass


def cache_path(repo: RepoInfo) -> Path:
    base = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    path = base / "blamest" / repo.host / repo.owner / f"{repo.name}.sqlite"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


class Cache:
    def __init__(self, path: Path):
        # Only one thread touches the connection at a time; workers hand results back to the caller.
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE IF NOT EXISTS prs (number INTEGER PRIMARY KEY, updated_at TEXT, data TEXT);
            CREATE TABLE IF NOT EXISTS identity (email TEXT PRIMARY KEY, login TEXT);
            CREATE TABLE IF NOT EXISTS churn_done (sha TEXT PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS churn (sha TEXT, email TEXT, orig_time INTEGER, lines INTEGER);
            CREATE INDEX IF NOT EXISTS churn_sha ON churn (sha);
        """)

    def meta(self, key: str) -> str | None:
        row = self.db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def set_meta(self, key: str, value: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, value))
        self.db.commit()

    def reset(self, *tables: str) -> None:
        for table in tables:
            self.db.execute(f"DELETE FROM {table}")
        self.db.commit()

    def prs(self) -> list[dict]:
        return [json.loads(d) for (d,) in self.db.execute("SELECT data FROM prs ORDER BY number")]

    def save_prs(self, prs: Iterable[dict]) -> None:
        self.db.executemany(
            "INSERT OR REPLACE INTO prs VALUES (?, ?, ?)",
            [(p["number"], p["updated_at"], json.dumps(p)) for p in prs],
        )
        self.db.commit()

    def identities(self) -> dict[str, str | None]:
        return dict(self.db.execute("SELECT email, login FROM identity"))

    def save_identities(self, mapping: dict[str, str | None]) -> None:
        self.db.executemany("INSERT OR REPLACE INTO identity VALUES (?, ?)", list(mapping.items()))
        self.db.commit()

    def churn(self) -> dict[str, list[ChurnLine]]:
        result: dict[str, list[ChurnLine]] = {sha: [] for (sha,) in self.db.execute("SELECT sha FROM churn_done")}
        for sha, email, t, n in self.db.execute("SELECT sha, email, orig_time, lines FROM churn"):
            result.setdefault(sha, []).append(ChurnLine(email, t, n))
        return result

    def save_churn(self, sha: str, lines: list[ChurnLine]) -> None:
        self.db.executemany("INSERT INTO churn VALUES (?, ?, ?, ?)", [(sha, l.email, l.orig_time, l.lines) for l in lines])
        self.db.execute("INSERT OR REPLACE INTO churn_done VALUES (?)", (sha,))


@dataclass
class Dataset:
    root: Path
    repo: RepoInfo
    cfg: Config
    commits: list[Commit]              # every non-merge commit someone authored
    landed: list[Commit]               # first-parent history: changes as they landed on the branch
    prs: list[dict]
    names: dict[str, str]              # email -> most recent git author name
    logins: dict[str, str | None]      # email -> GitHub login
    churn: dict[str, list[ChurnLine]]  # commit sha -> lines it rewrote
    cache: Cache | None = None
    github: bool = True                # False when gh couldn't be used: no PRs, reviews or comments

    def __post_init__(self) -> None:
        # Emails GitHub can't link to an account inherit the login of another email with the same git name.
        self._login_by_name = {
            _norm_name(self.names[e]): login for e, login in self.logins.items() if login and e in self.names
        }
        self.prs_by_number = {p["number"]: p for p in self.prs}

    # --- identities -----------------------------------------------------------------------------

    def canon(self, identity: str) -> str:
        """Follow the [join] table: the person an identity (login, git name or email) counts as."""
        seen = set()
        while (key := identity.lower()) in self.cfg.join and key not in seen:
            seen.add(key)
            identity = self.cfg.join[key]
        return identity

    def who(self, email: str) -> str:
        """The person a commit email counts as: [join], else GitHub login, else login of the same git name,
        else git name."""
        email = email.lower()
        if email in self.cfg.join:
            return self.canon(email)
        name = self.names.get(email)
        base = self.logins.get(email) or (name and self._login_by_name.get(_norm_name(name))) or name or email
        return self.canon(base)

    def email_is_bot(self, email: str) -> bool:
        email = email.lower()
        return any(self.cfg.is_bot(n) for n in (self.who(email), self.names.get(email), email.split("@")[0]))

    def landed_by(self, commit: Commit) -> str | None:
        """Who a landed change belongs to: the PR author when the commit came from a PR, else the commit
        author. None for bots."""
        pr = self.prs_by_number.get(commit.pr_number) if commit.pr_number else None
        if pr:
            if self.cfg.is_bot(pr["author"], pr.get("author_type")):
                return None
            return self.canon(pr["author"])
        return None if self.email_is_bot(commit.email) else self.who(commit.email)

    def identities_of(self, person: str) -> list[str]:
        """Every login, git name and email that is counted as `person`."""
        found: set[str] = set()
        for email, name in self.names.items():
            if self.who(email) == person:
                found.update(x for x in (email, name, self.logins.get(email)) if x)
        for pr in self.prs:
            if self.canon(pr["author"]) == person:
                found.add(pr["author"])
        found.update(k for k in self.cfg.join if self.canon(k) == person)
        unique = {f.lower(): f for f in sorted(found)}  # 'alice' and 'Alice' are the same to a reader
        unique.pop(person.lower(), None)
        return sorted(unique.values(), key=str.lower)

    # --- churn ----------------------------------------------------------------------------------

    def ensure_churn(self, commits: list[Commit], progress: Progress = _noop) -> None:
        """Compute (and cache) churn for any of `commits` not analysed yet. Runs `git blame` in parallel."""
        todo = [c for c in commits if c.sha not in self.churn]
        if not todo:
            return
        progress("Analysing code churn (git blame)", 0, len(todo))
        with ThreadPoolExecutor(max_workers=os.cpu_count() or 4) as pool:
            futures = {pool.submit(churn_for_commit, self.root, c, self.cfg): c for c in todo}
            for done, future in enumerate(as_completed(futures), 1):
                commit = futures[future]
                lines = future.result()
                self.churn[commit.sha] = lines
                if self.cache:
                    self.cache.save_churn(commit.sha, lines)
                    if done % 200 == 0:
                        self.cache.db.commit()
                progress("Analysing code churn (git blame)", done, len(todo))
        if self.cache:
            self.cache.db.commit()


def _norm_name(name: str) -> str:
    """'Zoë Smith (ACME)', 'Zoe Smith' and 'ZoeSmith' compare equal."""
    name = re.sub(r"\(.*?\)", "", name)
    name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]", "", name.lower())


def _resolve_identities(root: Path, ref: str, gh: GitHub | None, cache: Cache, progress: Progress) -> tuple[dict, dict]:
    """email -> name, and email -> GitHub login (resolved via the API and cached; without gh only the
    cache and noreply emails are used)."""
    out = git(root, "log", ref, "--format=%H%x1f%aN%x1f%aE")
    names: dict[str, str] = {}
    sample_sha: dict[str, str] = {}
    for line in out.splitlines():
        sha, name, email = line.split("\x1f")
        email = email.lower()
        if email not in names:  # git log is newest first
            names[email] = name
            sample_sha[email] = sha

    logins = cache.identities()
    unknown = [e for e in names if e not in logins]
    found: dict[str, str | None] = {}
    for email in list(unknown):
        if login := login_from_noreply(email):
            found[email] = login
            unknown.remove(email)
    if unknown and gh:
        progress("Matching commit emails to GitHub users", 0, len(unknown))
        by_sha = gh.logins_for_commits(sample_sha[e] for e in unknown)
        for email in unknown:
            found[email] = by_sha.get(sample_sha[email])
        progress("Matching commit emails to GitHub users", len(unknown), len(unknown))
    if found:
        cache.save_identities(found)
        logins.update(found)
    return names, logins


def load(root: Path, repo: RepoInfo, cfg: Config, gh: GitHub | None, refresh: bool = False,
         progress: Progress = _noop) -> Dataset:
    """Read git history and, when `gh` is given, PRs and logins from GitHub."""
    cache = Cache(cache_path(repo))
    if refresh or cache.meta("schema") != SCHEMA_VERSION:
        cache.reset("prs", "identity", "meta", "churn", "churn_done")
        cache.set_meta("schema", SCHEMA_VERSION)
    if cache.meta("ignore_fingerprint") != cfg.ignore_fingerprint():
        cache.reset("churn", "churn_done")
        cache.set_meta("ignore_fingerprint", cfg.ignore_fingerprint())

    ref = resolve_branch(root, repo.default_branch)
    progress("Reading git history", 0, None)
    commits = read_commits(root, ref)
    landed = expand_unattributed_merges(root, read_commits(root, ref, landed=True), commits)
    progress("Reading git history", len(commits), len(commits))

    names, logins = _resolve_identities(root, ref, gh, cache, progress)
    if gh is None:
        return Dataset(
            root=root, repo=repo, cfg=cfg, commits=commits, landed=landed, prs=[],
            names=names, logins=logins, churn=cache.churn(), cache=cache, github=False,
        )

    last_sync = cache.meta("pr_sync")
    stage = "Fetching pull requests" + (" (changes since last run)" if last_sync else "")
    progress(stage, 0, None)
    fresh = gh.fetch_prs(last_sync, on_page=lambda n: progress(stage, n, None))
    cache.save_prs(fresh)
    if fresh:
        newest = max(p["updated_at"] for p in fresh)
        cache.set_meta("pr_sync", max(newest, last_sync or ""))
    progress(stage, len(fresh), len(fresh))

    return Dataset(
        root=root, repo=repo, cfg=cfg, commits=commits, landed=landed, prs=cache.prs(),
        names=names, logins=logins, churn=cache.churn(), cache=cache,
    )
