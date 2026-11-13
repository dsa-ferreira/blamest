# blamest

A light-hearted terminal leaderboard of who did what in a GitHub repository: commits, PRs, reviews,
comments, code churn, plus a set of (completely serious) awards.

## Install

```sh
uv tool install .          # from this directory; then `blamest` is on your PATH
```

Requires `git`. PR, review and comment metrics also need [`gh`](https://cli.github.com/), logged in to
the host the repo's remote points at (github.com or GitHub Enterprise). Without it, or when it isn't
logged in to that host, blamest warns and shows only the metrics that come from git (commits, lines,
activity and churn).

## Use

Run it inside a clone of the repository:

```sh
blamest                          # all time
blamest --since 90d              # last 90 days (also: 12w, 6m, 1y)
blamest --since 2026-01-01 --until 2026-03-31
blamest --churn-days 14          # change the churn window
blamest --refresh                # throw away the cache and refetch everything
```

Metrics come from the default branch as your local clone knows it (`origin/<default>`), so
`git fetch` first. Lines, areas and churn are measured on what *landed* on that branch (each PR
once, credited to its author, however it was merged); commit counts and habits use every authored
commit. The first run fetches every PR and runs `git blame` for churn, which can take a few
minutes. Results are cached in `~/.cache/blamest/<host>/<owner>/<repo>.sqlite`, so later runs only
fetch PRs that changed and only blame new commits.

### Keys

| Key | Action |
| --- | --- |
| `h`/`l`, `←/→`, click | previous / next tab |
| `j`/`k`, `↑/↓` | pick a metric / move through a leaderboard (also in profiles and the glossary) |
| `g`/`G` | jump to top / bottom |
| `Ctrl+d`/`Ctrl+u` | page down / up |
| `Tab` | move between a tab's metric list and its leaderboard |
| `Enter` | open the highlighted person's profile |
| `d` | change the date range |
| `J` | join the highlighted person into someone else (saved to `.blamest.toml`) |
| `u` | in a profile: undo the joins into that person |
| `?` | glossary with every metric explained |
| `q` | quit |

Every tab explains the metric it shows. Press `?` to see all of them in one place.

## Configuration (optional)

Put a `.blamest.toml` at the repository root:

```toml
churn_days = 21

# Replace the default ignore list entirely...
# ignore = ["*.lock", "vendor/*"]
# ...or add to it:
extra_ignore = ["src/generated/*", "*.pb.ts"]

# Extra accounts to treat as bots (dependabot, renovate, *[bot] etc. are built in)
bots = ["ci-deployer", "release-butler"]

# People to count as one. Keys can be a GitHub login, a git author name or an email;
# `j` in the dashboard writes these for you.
[join]
jdoe = "jane-doe"
"jane@old-laptop.local" = "jane-doe"
```

Before you need `[join]`: the repo's `.mailmap` is respected, commit emails are matched to GitHub
logins through the API, and an email GitHub can't match takes the login of another email with the
same git name (ignoring accents, spacing and a "(Company)" suffix).

## Development

```sh
uv sync
uv run pytest
```
