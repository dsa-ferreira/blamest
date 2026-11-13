"""blamest still works, with git-only metrics, when gh is missing or not logged in to the repo's host."""

import asyncio
import os
import stat

import pytest
from textual.widgets import TabbedContent

from blamest import dataset
from blamest.cli import _connect
from blamest.config import Config
from blamest.gitdata import parse_remote, repo_from_git
from blamest.github import github_problem
from blamest.metrics import GITHUB_CATEGORIES, compute
from blamest.tui import BlamestApp

from conftest import T0, make_dataset


@pytest.mark.parametrize("url,expected", [
    ("git@github.com:acme/widgets.git", ("github.com", "acme", "widgets")),
    ("https://github.com/acme/widgets", ("github.com", "acme", "widgets")),
    ("https://user:token@GHE.example.com/acme/widgets.git/", ("ghe.example.com", "acme", "widgets")),
    ("ssh://git@ghe.example.com:2222/acme/widgets.git", ("ghe.example.com", "acme", "widgets")),
    ("/srv/git/widgets.git", None),
    ("../widgets", None),
])
def test_parse_remote(url, expected):
    assert parse_remote(url) == expected


def test_repo_from_git_reads_remote_and_default_branch(repo):
    repo.write("a.txt", ["x"])
    repo.commit("alice", T0)
    repo._git("remote", "add", "origin", "git@ghe.example.com:acme/widgets.git")
    repo._git("update-ref", "refs/remotes/origin/trunk", "HEAD")
    repo._git("symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/trunk")
    info = repo_from_git(repo.path)
    assert (info.host, info.slug, info.default_branch) == ("ghe.example.com", "acme/widgets", "trunk")


def test_repo_without_remote_is_local(repo):
    repo.write("a.txt", ["x"])
    repo.commit("alice", T0)
    info = repo_from_git(repo.path)
    assert (info.host, info.name, info.default_branch) == ("local", repo.path.name, "main")
    assert "no GitHub remote" in github_problem(info.host)


def fake_gh(tmp_path, monkeypatch, exit_code: int) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text(f"#!/bin/sh\nexit {exit_code}\n")
    gh.chmod(gh.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")


def test_warns_when_gh_is_missing(monkeypatch):
    monkeypatch.setattr("blamest.github.shutil.which", lambda _: None)
    assert "isn't installed" in github_problem("github.com")


def test_warns_when_gh_is_not_logged_in_to_the_host(tmp_path, monkeypatch):
    fake_gh(tmp_path, monkeypatch, exit_code=1)
    assert github_problem("ghe.example.com") == (
        "gh isn't logged in to ghe.example.com (run `gh auth login --hostname ghe.example.com`)"
    )


def test_no_warning_when_gh_is_logged_in(tmp_path, monkeypatch):
    fake_gh(tmp_path, monkeypatch, exit_code=0)
    assert github_problem("ghe.example.com") is None


def test_connect_falls_back_to_git(repo, tmp_path, monkeypatch):
    repo.write("a.txt", ["x"])
    repo.commit("alice", T0)
    repo._git("remote", "add", "origin", "https://ghe.example.com/acme/widgets.git")
    fake_gh(tmp_path, monkeypatch, exit_code=1)
    info, gh, warning = _connect(repo.path)
    assert gh is None and info.slug == "acme/widgets"
    assert "isn't logged in to ghe.example.com" in warning
    assert "only the metrics that come from git" in warning


def test_load_without_gh_reads_git_only(repo, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    repo.write("a.txt", ["x"])
    repo.commit("alice", T0)
    repo.write("a.txt", ["y"])
    repo.commit("12345+bob", T0 + 60)
    repo._git("commit", "-q", "--amend", "--no-edit", "--author", "Bob <12345+bob@users.noreply.github.com>",
              env=repo._who("bob", T0 + 60, "+0000"))
    ds = dataset.load(repo.path, repo_from_git(repo.path), Config(), gh=None)
    assert not ds.github and ds.prs == [] and len(ds.commits) == 2
    assert ds.logins == {"12345+bob@users.noreply.github.com": "bob"}  # noreply still resolves
    assert ds.cache.identities() == ds.logins  # emails never looked up aren't cached as "no login"


def test_git_only_report_has_no_github_metrics(repo):
    repo.write("a.txt", ["x"])
    repo.commit("alice", T0)
    report = compute(make_dataset(repo, github=False), None, None)
    assert report.categories == ["Commits", "Churn"]
    assert not GITHUB_CATEGORIES & {m.category for m in report.metrics}
    assert report.values["alice"]["commits"] == 1


def test_git_only_dashboard_hides_github_tabs_and_warns(repo):
    repo.write("a.txt", ["x"])
    repo.commit("alice", T0)
    ds = make_dataset(repo, github=False)

    async def scenario():
        app = BlamestApp(ds, None, None, warning="gh isn't installed")
        async with app.run_test(size=(160, 50)) as pilot:
            await pilot.pause()
            ids = [p.id for p in app.query_one(TabbedContent).query("TabPane")]
            assert ids == ["awards", "overview", "commits", "churn", "activity"]
            assert "git only" in app.sub_title
            assert any("gh isn't installed" in n.message for n in app._notifications)
            await pilot.press("question_mark")  # glossary renders without the GitHub categories
            await pilot.pause()

    asyncio.run(scenario())
