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
from .gitdata import GitError, repo_root
from .github import GhError, detect_repo
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
        repo = detect_repo(root)

        with Progress(
            SpinnerColumn(), TextColumn("{task.description}"), BarColumn(), MofNCompleteColumn(),
            TimeElapsedColumn(), console=console, transient=False,
        ) as progress:
            tasks: dict[str, int] = {}

            def report(stage: str, done: int, total: int | None) -> None:
                if stage not in tasks:
                    tasks[stage] = progress.add_task(stage, total=total)
                progress.update(tasks[stage], completed=done, total=total)

            ds = dataset.load(root, repo, cfg, refresh=args.refresh, progress=report)
            ds.ensure_churn(landed_in_range(ds, since, until), progress=report)
    except (GitError, GhError, ValueError) as e:
        console.print(f"[red]error:[/red] {e}")
        return 1
    except KeyboardInterrupt:
        return 130

    from .tui import BlamestApp  # imported late so `--help` stays fast

    BlamestApp(ds, since, until).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
