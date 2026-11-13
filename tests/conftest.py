from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from blamest.config import Config
from blamest.dataset import Dataset
from blamest.gitdata import expand_unattributed_merges, read_commits
from blamest.github import RepoInfo

DAY = 86400
T0 = 1_767_225_600  # 2026-01-01T00:00:00Z


class Repo:
    def __init__(self, path: Path):
        self.path = path
        self._git("init", "-q", "-b", "main")

    def _git(self, *args: str, env: dict | None = None) -> str:
        return subprocess.run(
            ["git", "-C", str(self.path), *args], check=True, capture_output=True, text=True,
            env={**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1", **(env or {})},
        ).stdout

    def write(self, name: str, lines: list[str]) -> None:
        f = self.path / name
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("".join(l + "\n" for l in lines))

    def commit(self, who: str, when: int, message: str = "change", tz: str = "+0000") -> str:
        self._git("add", "-A")
        self._git("commit", "-q", "-m", message, env=self._who(who, when, tz))
        return self._git("rev-parse", "HEAD").strip()

    def checkout(self, *args: str) -> None:
        self._git("checkout", "-q", *args)

    def merge(self, branch: str, who: str, when: int, message: str) -> str:
        self._git("merge", "-q", "--no-ff", "-m", message, branch, env=self._who(who, when, "+0000"))
        return self._git("rev-parse", "HEAD").strip()

    @staticmethod
    def _who(who: str, when: int, tz: str) -> dict:
        date = f"@{when} {tz}"
        return {"GIT_AUTHOR_NAME": who.title(), "GIT_AUTHOR_EMAIL": f"{who}@example.com",
                "GIT_COMMITTER_NAME": who.title(), "GIT_COMMITTER_EMAIL": f"{who}@example.com",
                "GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date}


@pytest.fixture
def repo(tmp_path: Path) -> Repo:
    return Repo(tmp_path)


def make_dataset(repo: Repo, prs: list[dict] | None = None, cfg: Config | None = None, github: bool = True) -> Dataset:
    commits = read_commits(repo.path, "refs/heads/main")
    landed = expand_unattributed_merges(repo.path, read_commits(repo.path, "refs/heads/main", landed=True), commits)
    return Dataset(
        root=repo.path,
        repo=RepoInfo("github.example.com", "acme", "widgets", "main"),
        cfg=cfg or Config(),
        commits=commits,
        landed=landed,
        prs=prs or [],
        names={c.email: c.name for c in commits},
        logins={c.email: c.email.split("@")[0] for c in commits},
        churn={},
        github=github,
    )
