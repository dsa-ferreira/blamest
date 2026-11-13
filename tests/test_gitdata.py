from blamest.config import Config
from blamest.gitdata import _rename_target, churn_for_commit, read_commits

from conftest import DAY, T0


def test_read_commits_parses_authors_stats_and_timezone(repo):
    repo.write("src/app.py", [f"line {i}" for i in range(10)])
    repo.commit("alice", T0, "initial", tz="-0500")
    repo.write("src/app.py", [f"line {i}" for i in range(8)] + ["new"])
    repo.commit("bob", T0 + DAY, "tweak")

    commits = read_commits(repo.path, "refs/heads/main")

    assert [c.email for c in commits] == ["bob@example.com", "alice@example.com"]
    bob, alice = commits
    assert (alice.files[0].path, alice.files[0].added, alice.files[0].deleted) == ("src/app.py", 10, 0)
    assert (bob.files[0].added, bob.files[0].deleted) == (1, 2)
    assert alice.tz_minutes == -300
    assert alice.local_time == T0 - 5 * 3600


def test_rename_paths_resolve_to_new_name():
    assert _rename_target("src/{old => new}/x.py") == "src/new/x.py"
    assert _rename_target("src/{ => sub}/x.py") == "src/sub/x.py"
    assert _rename_target("a.py => b.py") == "b.py"
    assert _rename_target("plain.py") == "plain.py"


def test_churn_attributes_rewritten_lines_to_original_author(repo):
    repo.write("a.py", [f"alice {i}" for i in range(10)])
    repo.commit("alice", T0)
    # bob modifies lines 2-4 and deletes line 9
    lines = [f"alice {i}" for i in range(10)]
    lines[2:5] = ["bob 2", "bob 3", "bob 4"]
    del lines[9]
    repo.write("a.py", lines)
    repo.commit("bob", T0 + 2 * DAY)

    bob_commit = read_commits(repo.path, "refs/heads/main")[0]
    churn = churn_for_commit(repo.path, bob_commit, Config())

    assert [(c.email, c.orig_time, c.lines) for c in churn] == [("alice@example.com", T0, 4)]


def test_churn_ignores_whitespace_only_changes_and_ignored_files(repo):
    repo.write("a.py", ["x = 1", "y = 2"])
    repo.write("package-lock.json", ["{", "}"])
    repo.commit("alice", T0)
    repo.write("a.py", ["x  =  1", "y = 2"])
    repo.write("package-lock.json", ["{", "  \"a\": 1", "}"])
    repo.commit("bob", T0 + DAY)

    bob_commit = read_commits(repo.path, "refs/heads/main")[0]
    assert churn_for_commit(repo.path, bob_commit, Config()) == []


def test_root_commit_has_no_churn(repo):
    repo.write("a.py", ["x"])
    repo.commit("alice", T0)
    assert churn_for_commit(repo.path, read_commits(repo.path, "refs/heads/main")[0], Config()) == []
