"""Turns a Dataset into per-person numbers, leaderboards and awards for a given date range."""

from __future__ import annotations

import math
import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

from .dataset import Dataset
from .gitdata import Commit

MIN_COMMITS_FOR_HABITS = 5  # night-owl / weekend percentages need a minimum sample
MIN_PRS_FOR_RATES = 5  # same for the share of PRs merged without review


def fmt_int(v: float) -> str:
    return f"{int(round(v)):,}"


def fmt_pct(v: float) -> str:
    return f"{v:.0f}%"


def fmt_duration(hours: float) -> str:
    """Compound, fixed-shape durations so sorted columns read in order: 7s, 4m 12s, 3h 05m, 2d 03h."""
    secs = int(round(hours * 3600))
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}m {secs % 60:02d}s"
    if secs < 86400:
        return f"{secs // 3600}h {secs % 3600 // 60:02d}m"
    return f"{secs // 86400}d {secs % 86400 // 3600:02d}h"


@dataclass(frozen=True)
class Award:
    emoji: str
    name: str
    blurb: str


@dataclass(frozen=True)
class Metric:
    key: str
    category: str
    title: str
    description: str
    fmt: Callable[[float], str] = fmt_int
    ascending: bool = False  # True when the smallest value ranks first
    award: Award | None = None


CATEGORIES = ["Commits", "Pull requests", "Reviews", "Comments", "Churn"]
GITHUB_CATEGORIES = {"Pull requests", "Reviews", "Comments"}  # need PR data from gh; the rest is git only

_REVIEWED = (
    "Only PRs that got at least one review from someone else before merging count; PRs merged without "
    "review are tracked separately so instant self-merges don't win."
)


def build_metrics(churn_days: int, github: bool = True) -> list[Metric]:
    metrics = _all_metrics(churn_days)
    return metrics if github else [m for m in metrics if m.category not in GITHUB_CATEGORIES]


def _all_metrics(churn_days: int) -> list[Metric]:
    M, A = Metric, Award
    return [
        # --- Commits ---------------------------------------------------------------------------
        M("commits", "Commits", "Commits",
          "Non-merge commits this person authored that are on the default branch. Commits of PRs merged "
          "with a merge commit or rebase count one by one; a squash-merged PR counts as one commit, so "
          "this depends on how people merge. Compare with 'PRs merged' for a fairer view.",
          award=A("🏭", "Commit Machine", "Never met a change too small to commit.")),
        M("lines_added", "Commits", "Lines added",
          "Lines added by the changes that landed on the default branch, credited to the PR author (or "
          "the commit author for direct pushes). Each PR counts once, at its final state, so lines "
          "written and removed again inside a PR don't count. Lockfiles, vendored and generated files "
          "are excluded (configurable in .blamest.toml).",
          award=A("✍️", "Keyboard Warrior", "Wrote the most lines. Some of them were even needed.")),
        M("lines_deleted", "Commits", "Lines deleted",
          "Lines removed by the changes that landed on the default branch, credited like 'Lines added'. "
          "Deleting code is a feature.",
          award=A("✂️", "The Eraser", "The best code is no code.")),
        M("active_days", "Commits", "Active days",
          "Distinct calendar days (in the author's own timezone) with at least one authored commit.",
          award=A("📅", "Ever Present", "Shows up. Every. Single. Day.")),
        M("night_owl_pct", "Commits", "Night owl %",
          "Share of authored commits made between 22:00 and 06:00 in the author's own timezone. Note: "
          "a squash-merged PR's commit carries the time the PR was merged, not when the code was written. "
          f"Only shown with at least {MIN_COMMITS_FOR_HABITS} commits in range.",
          fmt=fmt_pct, award=A("🦉", "Night Owl", "Does their best work when everyone else is asleep.")),
        M("weekend_pct", "Commits", "Weekend %",
          "Share of authored commits made on Saturday or Sunday in the author's own timezone (same "
          f"squash caveat as Night owl %). Only shown with at least {MIN_COMMITS_FOR_HABITS} commits.",
          fmt=fmt_pct, award=A("🏖️", "Weekend Warrior", "Weekends are just weekdays with fewer meetings.")),
        # --- Pull requests ---------------------------------------------------------------------
        M("prs_opened", "Pull requests", "PRs opened",
          "Pull requests this person opened in the date range, drafts and PRs into non-default branches "
          "(stacked PRs) included."),
        M("prs_merged", "Pull requests", "PRs merged",
          "Pull requests authored by this person that were merged in the date range, into any branch.",
          award=A("🚢", "Shipaholic", "Ships it. Then ships it again.")),
        M("prs_closed_unmerged", "Pull requests", "PRs closed unmerged",
          "Pull requests authored by this person that were closed without merging in the date range.",
          award=A("🪦", "PR Graveyard Keeper", "Here lie the PRs that never were.")),
        M("merged_without_review", "Pull requests", "Merged without review",
          "PRs by this person merged in range without a single review (approve, request changes or "
          "comment review) from anyone else before the merge.",
          award=A("🤠", "Cowboy", "Reviews are for people who make mistakes.")),
        M("unreviewed_pct", "Pull requests", "Merged without review %",
          "Share of this person's PRs merged in range that had no review from anyone else before the "
          "merge. Unlike the count above, this doesn't grow just because someone merges a lot. Only "
          f"shown with at least {MIN_PRS_FOR_RATES} merged PRs.",
          fmt=fmt_pct, award=A("🎲", "Living Dangerously", "Production is the real code review.")),
        M("fastest_pr", "Pull requests", "Fastest reviewed PR",
          "The quickest open→merge time among this person's PRs merged in range. Draft time counts. "
          + _REVIEWED,
          fmt=fmt_duration, ascending=True, award=A("⚡", "The Flash", "Blink and you missed the review.")),
        M("median_ttm", "Pull requests", "Median time to merge",
          "Median open→merge time (wall clock, nights, weekends and draft time included) of this "
          "person's PRs merged in range. Half their PRs merged faster than this. " + _REVIEWED,
          fmt=fmt_duration, ascending=True, award=A("🏎️", "Speed Demon", "Consistently fast, not just lucky once.")),
        M("median_pr_size", "Pull requests", "Median PR size (lines)",
          "Median lines changed (additions + deletions, as GitHub counts them, generated files "
          "included) per PR merged in range. Median, so one giant rename doesn't decide it.",
          award=A("📚", "The Novelist", "Their PRs come with a table of contents.")),
        M("median_pr_files", "Pull requests", "Median files per PR",
          "Median number of files changed per PR merged in range, as GitHub counts them.",
          award=A("🗂️", "Scatterbrain", "One PR, every folder.")),
        M("median_first_review", "Pull requests", "Median time to first review",
          "For PRs this person opened in range: median time from the PR being ready for review (opened, "
          "or marked ready if it started as a draft) to the first review by someone else. PRs never "
          "reviewed are left out. Lower means their PRs get picked up faster.",
          fmt=fmt_duration, ascending=True, award=A("🧲", "Review Magnet", "Their PRs get reviewed before the CI finishes.")),
        # --- Reviews ---------------------------------------------------------------------------
        M("prs_reviewed", "Reviews", "PRs reviewed",
          "Distinct pull requests by other people where this person submitted at least one review "
          "(any kind) in the date range.",
          award=A("🔍", "Eagle Eye", "Nothing gets merged without passing their gaze.")),
        M("approvals", "Reviews", "PRs approved",
          "Distinct pull requests by other people this person approved in range. Re-approving the same "
          "PR after new pushes doesn't count twice.",
          award=A("✅", "Seal of Approval", "Their approval is the one everyone waits for.")),
        M("silent_approvals", "Reviews", "Silent approvals",
          "PRs this person approved without leaving a single comment of any kind on that PR (no "
          "conversation, inline or review text).",
          award=A("👍", "Rubber Stamp", "LGTM. LGTM. LGTM.")),
        M("changes_requested", "Reviews", "Changes requested",
          "Times this person submitted a 'Request changes' review on someone else's PR. Each round counts.",
          award=A("🚧", "The Gatekeeper", "You shall not merge!")),
        # --- Comments ---------------------------------------------------------------------------
        M("written_total", "Comments", "Comments written (total)",
          "Conversation + inline code comments + review summaries this person wrote, on any PR (their "
          "own included). Bots are excluded.",
          award=A("🗣️", "Chatterbox", "Has an opinion. On every line.")),
        M("written_conversation", "Comments", "Conversation comments written",
          "Comments in a PR's main conversation tab (not attached to a code line)."),
        M("written_inline", "Comments", "Inline comments written",
          "Comments attached to a specific line of code in a PR diff, including replies in threads.",
          award=A("🔬", "Nitpicker Supreme", "Found a trailing space on line 412.")),
        M("written_review", "Comments", "Review summaries written",
          "Text written in the summary box when submitting a review. Reviews without text are not counted."),
        M("received_total", "Comments", "Comments received (total)",
          "Conversation + inline + review-summary comments other (non-bot) people wrote on this person's "
          "PRs. Their own replies don't count.",
          award=A("🔥", "Hot Topic", "Their PRs are where the discussion happens.")),
        M("received_conversation", "Comments", "Conversation comments received",
          "Conversation-tab comments by others on this person's PRs."),
        M("received_inline", "Comments", "Inline comments received",
          "Line-level code comments by others on this person's PRs."),
        M("received_review", "Comments", "Review summaries received",
          "Review summaries with text, written by others on this person's PRs."),
        # --- Churn -----------------------------------------------------------------------------
        M("churn_total", "Churn", "Code churn (total)",
          f"Lines this person landed on the default branch that a later landed change modified or "
          f"deleted within {churn_days} days. Found with `git blame` on the branch before each change. "
          "Rework inside a PR before it merges doesn't count; whitespace-only edits and ignored files "
          "are skipped.",
          award=A("🌪️", "Churn Champion", "Their code doesn't age, it gets replaced.")),
        M("churn_by_others", "Churn", "Churned by others",
          f"Lines this person landed that someone else's change modified or deleted within {churn_days} days.",
          award=A("🪃", "Boomerang", "Their code keeps coming back... rewritten.")),
        M("churn_self", "Churn", "Self-churn",
          f"Lines this person landed and then modified or deleted in a later change of their own within "
          f"{churn_days} days (e.g. a follow-up fix PR).",
          award=A("🔁", "Groundhog Day", "Rewrites their own code before the ink dries.")),
        M("rewrote_others", "Churn", "Rewrote others' code",
          f"Lines landed by someone else in the previous {churn_days} days that this person's change "
          "modified or deleted. The 'rewriter' board.",
          award=A("🏗️", "The Renovator", "Sees your fresh code and thinks: I can fix that.")),
    ]


@dataclass
class Report:
    since: datetime | None
    until: datetime | None
    churn_days: int
    metrics: list[Metric]
    values: dict[str, dict[str, float]]
    extras: dict[str, dict] = field(default_factory=dict)
    bucket_days: float = 7.0

    @property
    def categories(self) -> list[str]:
        return [c for c in CATEGORIES if any(m.category == c for m in self.metrics)]

    def has_metric(self, key: str) -> bool:
        return any(m.key == key for m in self.metrics)

    @property
    def authors(self) -> list[str]:
        return sorted(self.values, key=str.lower)

    def metric(self, key: str) -> Metric:
        return next(m for m in self.metrics if m.key == key)

    def leaderboard(self, key: str) -> list[tuple[str, float]]:
        metric = self.metric(key)
        rows = [(a, v[key]) for a, v in self.values.items() if key in v]
        return sorted(rows, key=lambda r: (r[1] if metric.ascending else -r[1], r[0].lower()))

    def rank(self, author: str, key: str) -> tuple[int, int] | None:
        board = self.leaderboard(key)
        for i, (a, _) in enumerate(board, 1):
            if a == author:
                return i, len(board)
        return None


def _ts(iso: str | None) -> float | None:
    return datetime.fromisoformat(iso).timestamp() if iso else None


def _area(path: str) -> str:
    parts = path.split("/")[:-1]
    return "/".join(parts[:2]) if parts else "(root)"


def _bounds(since: datetime | None, until: datetime | None) -> tuple[float, float]:
    return (since.timestamp() if since else -math.inf, until.timestamp() if until else math.inf)


def authored_in_range(ds: Dataset, since: datetime | None, until: datetime | None) -> list[Commit]:
    lo, hi = _bounds(since, until)
    return [c for c in ds.commits if lo <= c.time <= hi and not ds.email_is_bot(c.email)]


def landed_in_range(ds: Dataset, since: datetime | None, until: datetime | None) -> list[Commit]:
    """Changes that landed on the default branch in range: what lines and churn are measured on."""
    lo, hi = _bounds(since, until)
    return [c for c in ds.landed if lo <= c.time <= hi and ds.landed_by(c) is not None]


def compute(ds: Dataset, since: datetime | None, until: datetime | None, churn_days: int | None = None) -> Report:
    churn_days = churn_days or ds.cfg.churn_days
    lo, hi = _bounds(since, until)

    def in_range(t: float | None) -> bool:
        return t is not None and lo <= t <= hi

    counts: dict[str, Counter] = defaultdict(Counter)
    extras: dict[str, dict] = defaultdict(dict)
    authored = authored_in_range(ds, since, until)
    landed = landed_in_range(ds, since, until)

    # --- authored commits: count and habits -----------------------------------------------------
    days: dict[str, set] = defaultdict(set)
    night: Counter = Counter()
    weekend: Counter = Counter()
    for c in authored:
        a = ds.who(c.email)
        counts[a]["commits"] += 1
        local = datetime.fromtimestamp(c.local_time, timezone.utc)
        days[a].add(local.date())
        night[a] += local.hour >= 22 or local.hour < 6
        weekend[a] += local.weekday() >= 5
    for a, d in days.items():
        counts[a]["active_days"] = len(d)

    # --- landed changes: lines and areas ------------------------------------------------------
    areas: dict[str, Counter] = defaultdict(Counter)
    for c in landed:
        a = ds.landed_by(c)
        for f in c.files:
            if ds.cfg.is_ignored(f.path):
                continue
            counts[a]["lines_added"] += f.added
            counts[a]["lines_deleted"] += f.deleted
            areas[a][_area(f.path)] += f.added + f.deleted
    for a, area in areas.items():
        extras[a]["areas"] = area.most_common(3)

    # activity sparkline buckets
    bucket_days = 7.0
    if authored:
        first, last = min(c.time for c in authored), max(c.time for c in authored)
        start = max(lo, first) if since else first
        end = min(hi, last + 1) if until else last + 1
        span = max(end - start, 1)
        n_buckets = max(1, min(40, math.ceil(span / (7 * 86400))))
        width = span / n_buckets
        bucket_days = width / 86400
        for c in authored:
            spark = extras[ds.who(c.email)].setdefault("activity", [0] * n_buckets)
            spark[min(int((c.time - start) / width), n_buckets - 1)] += 1

    # --- pull requests, reviews and comments -------------------------------------------------
    ttm: dict[str, list[float]] = defaultdict(list)
    sizes: dict[str, list[int]] = defaultdict(list)
    files: dict[str, list[int]] = defaultdict(list)
    first_review: dict[str, list[float]] = defaultdict(list)
    reviewed: dict[str, set] = defaultdict(set)
    approved: dict[str, set] = defaultdict(set)
    silent: dict[str, set] = defaultdict(set)
    for pr in ds.prs:
        author_bot = ds.cfg.is_bot(pr["author"], pr.get("author_type"))
        author = ds.canon(pr["author"])
        created, merged, closed = _ts(pr["created_at"]), _ts(pr["merged_at"]), _ts(pr["closed_at"])

        def human(item: dict) -> str | None:
            return None if ds.cfg.is_bot(item["author"], item["author_type"]) else ds.canon(item["author"])

        reviews = [(human(r), _ts(r["at"]), r) for r in pr["reviews"] if r["state"] != "PENDING"]
        reviews = [(who, t, r) for who, t, r in reviews if who and who != author]
        commenters = {human(c) for c in pr["conversation"] + pr["inline"]} | {
            human(r) for r in pr["reviews"] if r["has_body"]
        }

        if not author_bot:
            if in_range(created) and reviews:
                ready = _ts(pr.get("ready_at")) or created
                first_review[author].append(max(min(t for _, t, _ in reviews) - ready, 0) / 3600)
            if in_range(created):
                counts[author]["prs_opened"] += 1
            if merged and in_range(merged):
                counts[author]["prs_merged"] += 1
                sizes[author].append(pr["additions"] + pr["deletions"])
                files[author].append(pr.get("changed_files", 0))
                if any(t <= merged for _, t, _ in reviews):
                    hours = max(merged - created, 0) / 3600
                    ttm[author].append(hours)
                    best = extras[author].get("fastest_pr")
                    if best is None or hours < best[2]:
                        extras[author]["fastest_pr"] = (pr["number"], pr["title"], hours)
                else:
                    counts[author]["merged_without_review"] += 1
            elif not merged and in_range(closed):
                counts[author]["prs_closed_unmerged"] += 1

        for kind, items in (("conversation", pr["conversation"]), ("inline", pr["inline"]),
                            ("review", [r for r in pr["reviews"] if r["has_body"]])):
            for c in items:
                who = human(c)
                if not who or not in_range(_ts(c["at"])):
                    continue
                counts[who][f"written_{kind}"] += 1
                counts[who]["written_total"] += 1
                if who != author and not author_bot:
                    counts[author][f"received_{kind}"] += 1
                    counts[author]["received_total"] += 1

        for who, t, r in reviews:
            if not in_range(t):
                continue
            reviewed[who].add(pr["number"])
            if r["state"] == "APPROVED":
                approved[who].add(pr["number"])
                if who not in commenters:
                    silent[who].add(pr["number"])
            elif r["state"] == "CHANGES_REQUESTED":
                counts[who]["changes_requested"] += 1

    for key, sets in (("prs_reviewed", reviewed), ("approvals", approved), ("silent_approvals", silent)):
        for a, prs in sets.items():
            counts[a][key] = len(prs)

    # --- churn (on landed changes) ------------------------------------------------------------
    window = churn_days * 86400
    for c in landed:
        rewriter = ds.landed_by(c)
        for line in ds.churn.get(c.sha, []):
            if c.time - line.orig_time > window or ds.email_is_bot(line.email):
                continue
            original = ds.who(line.email)
            counts[original]["churn_total"] += line.lines
            if original == rewriter:
                counts[original]["churn_self"] += line.lines
            else:
                counts[original]["churn_by_others"] += line.lines
                counts[rewriter]["rewrote_others"] += line.lines

    # --- derived values -----------------------------------------------------------------------
    values: dict[str, dict[str, float]] = {}
    for a, cnt in counts.items():
        v: dict[str, float] = {k: float(n) for k, n in cnt.items() if n}
        if cnt["commits"] >= MIN_COMMITS_FOR_HABITS:
            v["night_owl_pct"] = 100 * night[a] / cnt["commits"]
            v["weekend_pct"] = 100 * weekend[a] / cnt["commits"]
        if ttm[a]:
            v["fastest_pr"] = min(ttm[a])
            v["median_ttm"] = statistics.median(ttm[a])
        if cnt["prs_merged"] >= MIN_PRS_FOR_RATES:
            v["unreviewed_pct"] = 100 * cnt["merged_without_review"] / cnt["prs_merged"]
        if sizes[a]:
            v["median_pr_size"] = statistics.median(sizes[a])
            v["median_pr_files"] = statistics.median(files[a])
        if first_review[a]:
            v["median_first_review"] = statistics.median(first_review[a])
        if v:
            values[a] = v

    return Report(
        since=since, until=until, churn_days=churn_days, metrics=build_metrics(churn_days, ds.github),
        values=values, extras={a: extras[a] for a in values}, bucket_days=bucket_days,
    )
