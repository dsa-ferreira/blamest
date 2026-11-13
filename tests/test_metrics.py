from datetime import datetime, timezone

import pytest

from blamest.config import Config, load_config
from blamest.dates import parse_date
from blamest.github import login_from_noreply
from blamest.metrics import compute, fmt_duration

from conftest import DAY, T0, make_dataset


def iso(t: int) -> str:
    return datetime.fromtimestamp(t, timezone.utc).isoformat().replace("+00:00", "Z")


def pr(number, author, created, merged=None, closed=None, conversation=(), inline=(), reviews=(), size=(10, 5),
       files=1, ready=None, author_type="User"):
    def c(who, t):
        return {"author": who, "author_type": "Bot" if who.endswith("[bot]") else "User", "at": iso(t)}

    return {
        "number": number, "title": f"PR {number}", "state": "MERGED" if merged else "CLOSED" if closed else "OPEN",
        "created_at": iso(created), "updated_at": iso(merged or closed or created),
        "merged_at": iso(merged) if merged else None, "closed_at": iso(closed or merged) if (closed or merged) else None,
        "additions": size[0], "deletions": size[1], "changed_files": files, "base": "main",
        "ready_at": iso(ready) if ready else None, "author": author, "author_type": author_type,
        "conversation": [c(w, t) for w, t in conversation],
        "inline": [c(w, t) for w, t in inline],
        "reviews": [{**c(w, t), "state": s, "has_body": body} for w, t, s, body in reviews],
    }


def build(repo, prs=None, cfg=None):
    data = make_dataset(repo, prs, cfg)
    data.ensure_churn(data.landed)
    return data


@pytest.fixture
def ds(repo):
    repo.write("src/core/a.py", [f"alice {i}" for i in range(10)])
    repo.commit("alice", T0)
    lines = [f"alice {i}" for i in range(10)]
    lines[0:3] = ["bob 0", "bob 1", "bob 2"]
    repo.write("src/core/a.py", lines)
    repo.commit("bob", T0 + 2 * DAY)
    lines[5] = "alice again"
    repo.write("src/core/a.py", lines)
    repo.commit("alice", T0 + 40 * DAY)  # outside the 21-day window for her own line 5
    repo.write("bot.txt", ["x"])
    repo.commit("dependabot[bot]", T0 + 3 * DAY)

    prs = [
        pr(1, "alice", T0, merged=T0 + 3600, size=(10, 5), files=3,
           conversation=[("bob", T0 + 60), ("alice", T0 + 120), ("github-actions[bot]", T0 + 30)],
           inline=[("bob", T0 + 200), ("bob", T0 + 300)],
           reviews=[("bob", T0 + 1800, "APPROVED", False), ("carol", T0 + 900, "CHANGES_REQUESTED", True),
                    ("bob", T0 + 1900, "APPROVED", False)]),  # re-approval after a push
        pr(2, "alice", T0 + DAY, merged=T0 + DAY + 7, size=(100, 0), files=9),  # self-merged, no review
        pr(3, "bob", T0 + DAY, closed=T0 + 2 * DAY, reviews=[("alice", T0 + DAY + 7200, "COMMENTED", True)]),
        pr(4, "renovate[bot]", T0, merged=T0 + 60, author_type="Bot",
           reviews=[("bob", T0 + 30, "APPROVED", False)]),
    ]
    return build(repo, prs)


def test_commit_metrics_exclude_bots(ds):
    r = compute(ds, None, None)
    assert r.values["alice"]["commits"] == 2
    assert r.values["bob"]["commits"] == 1
    assert "dependabot[bot]" not in r.values
    assert r.extras["alice"]["areas"][0][0] == "src/core"


def test_pr_speed_only_counts_reviewed_prs(ds):
    v = compute(ds, None, None).values
    assert v["alice"]["prs_opened"] == 2
    assert v["alice"]["prs_merged"] == 2
    assert v["alice"]["merged_without_review"] == 1
    assert "unreviewed_pct" not in v["alice"]  # only 2 merged PRs, below the minimum sample
    assert v["alice"]["fastest_pr"] == pytest.approx(1.0)  # the 7-second self-merge doesn't count
    assert v["alice"]["median_ttm"] == pytest.approx(1.0)
    assert v["alice"]["median_pr_size"] == pytest.approx((15 + 100) / 2)
    assert v["alice"]["median_pr_files"] == pytest.approx(6)
    assert v["alice"]["median_first_review"] == pytest.approx(0.25)  # carol at +15 minutes
    assert v["bob"]["prs_closed_unmerged"] == 1
    assert "renovate[bot]" not in v


def test_first_review_starts_when_draft_is_ready(repo):
    prs = [pr(1, "alice", T0, merged=T0 + 10 * 3600, ready=T0 + 8 * 3600,
              reviews=[("bob", T0 + 9 * 3600, "APPROVED", False)])]
    repo.write("a", ["x"])
    repo.commit("alice", T0)
    v = compute(build(repo, prs), None, None).values
    assert v["alice"]["median_first_review"] == pytest.approx(1.0)
    assert v["alice"]["median_ttm"] == pytest.approx(10.0)  # draft time still counts towards merging


def test_review_and_comment_metrics(ds):
    v = compute(ds, None, None).values
    assert v["bob"]["approvals"] == 2  # PR 1 (approved twice) and the bot's PR 4
    assert v["bob"]["silent_approvals"] == 1  # commented on PR 1, silent on PR 4
    assert v["bob"]["prs_reviewed"] == 2
    assert v["carol"]["changes_requested"] == 1
    assert v["bob"]["written_conversation"] == 1 and v["bob"]["written_inline"] == 2
    assert v["bob"]["written_total"] == 3  # approvals without text are not comments
    assert v["carol"]["written_review"] == 1
    # alice's own reply counts as written but not received; the bot comment counts nowhere
    assert v["alice"]["written_conversation"] == 1
    assert v["alice"]["received_conversation"] == 1
    assert v["alice"]["received_inline"] == 2
    assert v["alice"]["received_review"] == 1
    assert v["alice"]["received_total"] == 4
    assert v["bob"]["received_review"] == 1


def test_churn_metrics_respect_window(ds):
    v = compute(ds, None, None).values
    assert v["alice"]["churn_by_others"] == 3
    assert v["bob"]["rewrote_others"] == 3
    assert "churn_self" not in v["alice"]  # her rewrite came 40 days later
    v60 = compute(ds, None, None, churn_days=60).values
    assert v60["alice"]["churn_self"] == 1
    assert v60["alice"]["churn_total"] == 4


def test_merge_commit_prs_are_measured_as_landed(repo):
    repo.write("a.py", [f"base {i}" for i in range(5)])
    repo.commit("alice", T0)
    repo.checkout("-b", "feature")
    repo.write("a.py", [f"base {i}" for i in range(5)] + ["wip 1", "wip 2"])
    repo.commit("bob", T0 + DAY)
    repo.write("a.py", [f"base {i}" for i in range(5)] + ["final"])  # bob reworks his own WIP inside the PR
    repo.commit("bob", T0 + DAY + 3600)
    repo.checkout("main")
    repo.merge("feature", "carol", T0 + 2 * DAY, "Merge pull request #7 from acme/feature")

    v = compute(build(repo, [pr(7, "bob", T0 + DAY, merged=T0 + 2 * DAY)]), None, None).values
    assert v["bob"]["commits"] == 2  # authored commits still count one by one
    assert v["bob"]["lines_added"] == 1  # but lines are the PR's net change, credited to the PR author
    assert "churn_self" not in v["bob"]  # rework inside the PR isn't churn
    assert "lines_added" not in v.get("carol", {})  # clicking merge doesn't make it carol's code


def test_join_merges_people_everywhere(repo, tmp_path):
    repo.write("a.py", [f"alice {i}" for i in range(4)])
    repo.commit("alice", T0)
    repo.write("a.py", ["carol"] + [f"alice {i}" for i in range(1, 4)])
    repo.commit("carol", T0 + DAY)
    prs = [pr(1, "alice", T0, merged=T0 + 3600, reviews=[("carol", T0 + 60, "CHANGES_REQUESTED", True)])]
    cfg = Config(join={"carol": "alice"})

    v = compute(build(repo, prs, cfg), None, None).values
    assert "carol" not in v
    assert v["alice"]["commits"] == 2
    assert v["alice"]["churn_self"] == 1  # carol rewriting alice is now alice rewriting herself
    assert "received_review" not in v["alice"]  # a review by yourself isn't received
    assert "changes_requested" not in v["alice"]


def test_identities_of_lists_everything_joined(repo):
    repo.write("a", ["x"])
    repo.commit("alice", T0)
    data = make_dataset(repo, cfg=Config(join={"alice": "ally"}))
    assert data.who("alice@example.com") == "ally"
    assert data.identities_of("ally") == ["alice", "alice@example.com"]


def test_join_is_saved_without_touching_the_rest_of_the_config(tmp_path):
    path = tmp_path / ".blamest.toml"
    path.write_text("# my settings\nchurn_days = 14  # two weeks\n")
    cfg = load_config(tmp_path)
    cfg.add_join("jdoe", "jane-doe")
    cfg.add_join("Jane D", "jane-doe")

    text = path.read_text()
    assert "# my settings" in text and "# two weeks" in text
    again = load_config(tmp_path)
    assert again.churn_days == 14
    assert again.join == {"jdoe": "jane-doe", "jane d": "jane-doe"}

    assert sorted(again.remove_joins_into("jane-doe")) == ["jane d", "jdoe"]
    assert load_config(tmp_path).join == {}


def test_unmapped_email_falls_back_to_login_with_same_name(ds):
    ds.names["alice@laptop.local"] = "alice  (Old Laptop)"
    ds.logins["alice@laptop.local"] = None
    ds.__post_init__()
    assert ds.who("alice@laptop.local") == "alice"


def test_date_range_filters_everything(ds):
    since = datetime.fromtimestamp(T0 + 30 * DAY, timezone.utc)
    v = compute(ds, since, None).values
    assert v["alice"]["commits"] == 1
    assert "prs_merged" not in v["alice"]
    assert "bob" not in v


def test_leaderboard_ordering(ds):
    r = compute(ds, None, None)
    assert r.leaderboard("commits")[0][0] == "alice"
    assert r.leaderboard("fastest_pr")[0][0] == "alice"
    assert r.rank("bob", "commits") == (2, 2)


@pytest.mark.parametrize("hours,text", [
    (1 / 3600, "1s"), (59 / 3600, "59s"), (61 / 3600, "1m 01s"), (0.5, "30m 00s"),
    (3 + 5 / 60, "3h 05m"), (47.9, "1d 23h"), (51, "2d 03h"),
])
def test_fmt_duration(hours, text):
    assert fmt_duration(hours) == text


def test_parse_date():
    now = datetime(2026, 11, 13, 12, tzinfo=timezone.utc)
    assert parse_date("30d", now=now) == datetime(2026, 10, 14, 12, tzinfo=timezone.utc)
    assert parse_date("") is None
    assert parse_date("2026-01-31", end_of_day=True).hour == 23
    with pytest.raises(ValueError):
        parse_date("last tuesday")


def test_noreply_and_bots():
    assert login_from_noreply("12345+octocat@users.noreply.github.com") == "octocat"
    assert login_from_noreply("octocat@users.noreply.ghe.example.com") == "octocat"
    assert login_from_noreply("octocat@example.com") is None
    cfg = Config(bots={"Release-Butler"})
    assert cfg.is_bot("dependabot[bot]") and cfg.is_bot("renovate") and cfg.is_bot("release-butler")
    assert cfg.is_bot("anyone", "Bot") and not cfg.is_bot("alice")


def test_name_fallback_ignores_accents_spacing_and_company_suffix(ds):
    for email, name in (("zoe@home.example", "Zoe Smith"), ("max@home.example", "MaxMustermann")):
        ds.names[email], ds.logins[email] = name, None
    ds.names["zoe@acme.example"], ds.logins["zoe@acme.example"] = "Zoë Smith (ACME)", "zoe-smith"
    ds.names["max@acme.example"], ds.logins["max@acme.example"] = "Max Mustermann(acme)", "max-mustermann"
    ds.__post_init__()
    assert ds.who("zoe@home.example") == "zoe-smith"
    assert ds.who("max@home.example") == "max-mustermann"


def test_spellings_of_one_name_without_login_count_as_one_person(repo):
    """Git-only mode (no gh, empty cache): no logins at all, so people are grouped by git name."""
    for who, n in (("zoe-work", 1), ("zoe-home", 2), ("wei-a", 1), ("wei-b", 1)):
        for i in range(n):
            repo.write(f"{who}.txt", [str(i)])
            repo.commit(who, T0 + i)
    ds = make_dataset(repo, github=False)
    ds.names.update({"zoe-work@example.com": "Zoë Smith (ACME)", "zoe-home@example.com": "Zoë  Smith",
                     "wei-a@example.com": "张伟", "wei-b@example.com": "王芳"})
    ds.logins = {}
    ds.__post_init__()
    # shown under the most used spelling, company suffix and extra spaces dropped
    assert ds.who("zoe-work@example.com") == ds.who("zoe-home@example.com") == "Zoë Smith"
    assert set(ds.identities_of("Zoë Smith")) == {"Zoë  Smith", "Zoë Smith (ACME)", "zoe-home@example.com",
                                                  "zoe-work@example.com"}
    # names without latin letters don't all collapse into one person
    assert ds.who("wei-a@example.com") == "张伟" and ds.who("wei-b@example.com") == "王芳"
    assert compute(ds, None, None).values["Zoë Smith"]["commits"] == 3


def test_join_on_an_exact_git_name_still_applies(repo):
    repo.write("a.txt", ["x"])
    repo.commit("zoe", T0)
    ds = make_dataset(repo, cfg=Config(join={"zoë smith (acme)": "zoe-smith"}), github=False)
    ds.names["zoe@example.com"], ds.logins = "Zoë Smith (ACME)", {}
    ds.__post_init__()
    assert ds.who("zoe@example.com") == "zoe-smith"


@pytest.mark.parametrize("path", [
    "billing/src/Api/obj/project.assets.json", "billing/src/Api/bin/Debug/net10.0/App.deps.json",
    "user/src/Persistence/Migrations/20260101_Init.Designer.cs", "user/src/Persistence/Migrations/AppDbContextModelSnapshot.cs",
    "app/ios/Acme.xcodeproj/project.pbxproj", "web/package-lock.json",
])
def test_default_ignore_list(path):
    assert Config().is_ignored(path)


def test_default_ignore_list_keeps_real_code():
    assert not Config().is_ignored("user/src/Persistence/Migrations/20260101_Init.cs")
    assert not Config().is_ignored("src/binder/obj_utils.py")


def test_local_merges_credit_the_merged_commits_not_the_merger(repo):
    repo.write("a.py", ["base"])
    repo.commit("alice", T0)
    repo.checkout("-b", "production")
    repo.checkout("main")
    repo.write("b.py", ["bob 1", "bob 2", "bob 3"])
    repo.commit("bob", T0 + DAY)
    repo.checkout("production")
    repo.write("c.py", ["carol"])
    repo.commit("carol", T0 + DAY)
    repo.merge("main", "carol", T0 + 2 * DAY, "Merge branch 'main' into production")
    repo.checkout("main")
    repo._git("merge", "-q", "--ff-only", "production")  # production pushed onto main

    v = compute(build(repo), None, None).values
    assert v["bob"]["lines_added"] == 3
    assert v["carol"]["lines_added"] == 1


def test_unreviewed_share_needs_enough_prs(repo):
    repo.write("a", ["x"])
    repo.commit("alice", T0)
    prs = [pr(n, "alice", T0 + n * DAY, merged=T0 + n * DAY + 60,
              reviews=[("bob", T0 + n * DAY + 30, "APPROVED", False)] if n % 2 else [])
           for n in range(1, 7)]
    v = compute(build(repo, prs), None, None).values
    assert v["alice"]["merged_without_review"] == 3
    assert v["alice"]["unreviewed_pct"] == pytest.approx(50)
