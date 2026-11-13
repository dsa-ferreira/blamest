"""Command-line entry point: `blamest [--since X] [--until Y] [--churn-days N] [--refresh]`."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from rich.console import Console
from rich.progress import BarColumn, MofNCompleteColumn, Progress, SpinnerColumn, TextColumn, TimeElapsedColumn

from . import dataset
from .config import load_config
from .dates import parse_date
from .gitdata import GitError, repo_from_git, repo_root
from .github import GhError, GitHub, RepoInfo, detect_repo, github_problem
from .metrics import landed_in_range


def _args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="blamest",
        description="A light-hearted terminal leaderboard of who did what in the GitHub repo you're in.",
    )
    p.add_argument("--since", help="start of the range: YYYY-MM-DD or relative (30d, 12w, 6m, 1y)")
    p.add_argument("--until", help="end of the range (inclusive): YYYY-MM-DD or relative")
    p.add_argument("--churn-days", type=int, help="code rewritten within this many days counts as churn (default 21)")
    p.add_argument("--refresh", action="store_true", help="ignore the local cache and fetch everything again")
    p.add_argument("-C", dest="path", default=".", help="run as if started in this directory")
    return p.parse_args(argv)


def _connect(root: Path) -> tuple[RepoInfo, GitHub | None, str | None]:
    """The repo, a GitHub client when gh can be used for it, and otherwise a warning saying why not."""
    repo = repo_from_git(root)
    problem = github_problem(repo.host)
    if problem is None:
        try:
            repo = detect_repo(root)  # gh knows the canonical owner/name and the real default branch
            return repo, GitHub(repo), None
        except GhError as e:
            problem = f"gh couldn't look up this repository: {e}"
    return repo, None, f"{problem}. Showing only the metrics that come from git (no PRs, reviews or comments)."


def main(argv: list[str] | None = None) -> int:
    args = _args(argv)
    console = Console(stderr=True)
    try:
        since = parse_date(args.since)
        until = parse_date(args.until, end_of_day=True)
        root = repo_root(Path(args.path).resolve())
        cfg = load_config(root)
        if args.churn_days:
            cfg.churn_days = args.churn_days
        repo, gh, warning = _connect(root)
        if warning:
            console.print(f"[yellow]warning:[/yellow] {warning}")

        with Progress(
            SpinnerColumn(), TextColumn("{task.description}"), BarColumn(), MofNCompleteColumn(),
            TimeElapsedColumn(), console=console, transient=False,
        ) as progress:
            tasks: dict[str, int] = {}

            def report(stage: str, done: int, total: int | None) -> None:
                if stage not in tasks:
                    tasks[stage] = progress.add_task(stage, total=total)
                progress.update(tasks[stage], completed=done, total=total)

            ds = dataset.load(root, repo, cfg, gh, refresh=args.refresh, progress=report)
            ds.ensure_churn(landed_in_range(ds, since, until), progress=report)
    except (GitError, GhError, ValueError) as e:
        console.print(f"[red]error:[/red] {e}")
        return 1
    except KeyboardInterrupt:
        return 130

    from .tui import BlamestApp  # imported late so `--help` stays fast

    BlamestApp(ds, since, until, warning).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
