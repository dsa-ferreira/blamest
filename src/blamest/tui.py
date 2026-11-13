"""The interactive terminal dashboard (Textual)."""

from __future__ import annotations

from datetime import datetime

from rich.markup import escape
from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (
    Button, DataTable, Footer, Header, Input, Label, LoadingIndicator, Markdown, OptionList, Static,
    TabbedContent, TabPane,
)
from textual.widgets.option_list import Option

from .dataset import Dataset
from .dates import describe_range, parse_date
from .metrics import CATEGORIES, Report, compute, fmt_duration, landed_in_range

SPARK = "▁▂▃▄▅▆▇█"

# Vim-style movement, hidden from the footer to keep it readable (listed in the README and glossary).
_VIM = [("j", "cursor_down"), ("k", "cursor_up"), ("ctrl+d", "page_down"), ("ctrl+u", "page_up")]


class VimDataTable(DataTable):
    BINDINGS = [Binding(key, action, show=False) for key, action in _VIM] + [
        Binding("g", "scroll_top", show=False), Binding("G", "scroll_bottom", show=False),
        # tables use row cursors, so left/right are free to keep switching tabs
        Binding("left", "app.switch_tab(-1)", show=False), Binding("right", "app.switch_tab(1)", show=False),
    ]


class VimOptionList(OptionList):
    BINDINGS = [Binding(key, action, show=False) for key, action in _VIM] + [
        Binding("g", "first", show=False), Binding("G", "last", show=False),
    ]


class VimScroll(VerticalScroll):
    BINDINGS = [
        Binding("j", "scroll_down", show=False), Binding("k", "scroll_up", show=False),
        Binding("ctrl+d", "page_down", show=False), Binding("ctrl+u", "page_up", show=False),
        Binding("g", "scroll_home", show=False), Binding("G", "scroll_end", show=False),
    ]
MEDALS = {1: "🥇", 2: "🥈", 3: "🥉"}


def sparkline(values: list[int]) -> str:
    top = max(values, default=0)
    if not top:
        return SPARK[0] * len(values)
    return "".join(" " if v == 0 else SPARK[min(int(v / top * (len(SPARK) - 1) + 0.5), len(SPARK) - 1)] for v in values)


def bar(value: float, top: float, width: int = 24) -> Text:
    filled = 0 if top <= 0 else value / top * width
    full = int(filled)
    partial = " ▏▎▍▌▋▊▉"[int((filled - full) * 8)] if full < width else ""
    return Text("█" * full + partial.strip(), style="bold cyan")


def rank_label(rank: int) -> str:
    return MEDALS.get(rank, f"#{rank}")


class Explain(Static):
    """A box describing what the currently shown metric means."""

    DEFAULT_CSS = """
    Explain { border: round $accent; padding: 0 1; margin: 0 0 1 0; height: auto; }
    """


class CategoryPane(Vertical):
    """Metric picker on the left, explanation + leaderboard on the right."""

    DEFAULT_CSS = """
    CategoryPane { layout: horizontal; }
    CategoryPane OptionList { width: 36; height: 100%; }
    CategoryPane .board { width: 1fr; padding: 0 1; }
    CategoryPane DataTable { height: 1fr; }
    """

    def __init__(self, category: str, **kwargs):
        super().__init__(**kwargs)
        self.category = category
        self.report: Report | None = None
        self.current: str | None = None

    def compose(self) -> ComposeResult:
        yield VimOptionList()
        with Vertical(classes="board"):
            yield Explain()
            yield VimDataTable(cursor_type="row", zebra_stripes=True)

    def update_report(self, report: Report) -> None:
        self.report = report
        metrics = [m for m in report.metrics if m.category == self.category]
        options = self.query_one(OptionList)
        options.clear_options()
        options.add_options([Option(m.title, id=m.key) for m in metrics])
        keys = [m.key for m in metrics]
        index = keys.index(self.current) if self.current in keys else 0
        options.highlighted = index
        self.show(keys[index])

    @on(OptionList.OptionHighlighted)
    def _picked(self, event: OptionList.OptionHighlighted) -> None:
        if event.option.id:
            self.show(event.option.id)

    def show(self, key: str) -> None:
        if not self.report:
            return
        self.current = key
        metric = self.report.metric(key)
        direction = "Lower is better." if metric.ascending else "Higher ranks first."
        award = f"\n🏆 Award: {metric.award.emoji} {metric.award.name}" if metric.award else ""
        self.query_one(Explain).update(
            Text.assemble((metric.title, "bold"), "\n", metric.description, " ", (direction, "italic dim"), award)
        )
        board = self.report.leaderboard(key)
        table = self.query_one(DataTable)
        table.clear(columns=True)
        table.add_columns("Rank", "Author", metric.title, "")
        if not board:
            return
        top = max(abs(v) for _, v in board)
        for i, (author, value) in enumerate(board, 1):
            # bars are always proportional to the value; on "lower is better" boards the shortest bar wins
            table.add_row(rank_label(i), author, metric.fmt(value), bar(value, top), key=author)


class AwardsPane(VerticalScroll):
    def compose(self) -> ComposeResult:
        yield Explain(
            "The winner of each metric gets a (completely serious) award. Every award is tied to one "
            "metric; the 'Metric' column says which, and the other tabs explain how it's measured. "
            "Press Enter on a row to open the winner's profile."
        )
        yield VimDataTable(cursor_type="row", zebra_stripes=True)

    def update_report(self, report: Report) -> None:
        table = self.query_one(DataTable)
        table.clear(columns=True)
        table.add_columns("Award", "Winner", "Score", "Citation", "Metric", "Runner-up")
        for m in report.metrics:
            if not m.award:
                continue
            board = report.leaderboard(m.key)
            if not board:
                continue
            winner, value = board[0]
            if value == 0 and not m.ascending:
                continue  # nobody earned it
            runner = f"{board[1][0]} ({m.fmt(board[1][1])})" if len(board) > 1 else "—"
            table.add_row(
                f"{m.award.emoji} {m.award.name}", Text(winner, style="bold"), m.fmt(value),
                Text(m.award.blurb, style="italic"), m.title, runner, key=winner + "\x00" + m.key,
            )


OVERVIEW_COLUMNS = [
    "prs_merged", "median_ttm", "unreviewed_pct", "prs_reviewed", "written_total",
    "received_total", "commits", "lines_added", "lines_deleted", "churn_total",
]


class OverviewPane(Vertical):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.report: Report | None = None
        self.sort_key, self.sort_desc = "prs_merged", True

    def compose(self) -> ComposeResult:
        yield Explain(
            "Everyone at a glance, one row per person (bots excluded). Columns are explained in their "
            "own tabs. Click a column header to sort by it (click again to flip ▲/▼; '—' always goes "
            "last). Enter opens a person's profile; J joins the highlighted person into someone else."
        )
        yield VimDataTable(cursor_type="row", zebra_stripes=True)

    def update_report(self, report: Report) -> None:
        self.report = report
        self._build()

    def _build(self) -> None:
        report, key = self.report, self.sort_key
        table = self.query_one(DataTable)
        table.clear(columns=True)
        arrow = " ▼" if self.sort_desc else " ▲"
        table.add_column("Person" + (arrow if key == "author" else ""), key="author")
        for k in OVERVIEW_COLUMNS:
            table.add_column(report.metric(k).title + (arrow if k == key else ""), key=k)

        if key == "author":
            people = sorted(report.authors, key=str.lower, reverse=self.sort_desc)
        else:
            have = [a for a in report.authors if key in report.values[a]]
            missing = [a for a in report.authors if key not in report.values[a]]
            have.sort(key=lambda a: (report.values[a][key], a.lower()), reverse=self.sort_desc)
            people = have + missing
        for author in people:
            v = report.values[author]
            table.add_row(
                author,
                *[Text(report.metric(k).fmt(v[k]) if k in v else "—", justify="right") for k in OVERVIEW_COLUMNS],
                key=author,
            )

    @on(DataTable.HeaderSelected)
    def _sort(self, event: DataTable.HeaderSelected) -> None:
        key = event.column_key.value
        if key == self.sort_key:
            self.sort_desc = not self.sort_desc
        else:
            # first click shows the "best" end first
            self.sort_key = key
            self.sort_desc = not (key == "author" or self.report.metric(key).ascending)
        self._build()


class ActivityPane(Vertical):
    def compose(self) -> ComposeResult:
        yield Explain(id="activity-explain")
        yield VimDataTable(cursor_type="row", zebra_stripes=True)

    def update_report(self, report: Report) -> None:
        self.query_one(Explain).update(
            "Commit activity over the selected range. Each sparkline character is one bucket of "
            f"{report.bucket_days:.1f} days; taller means more commits (scaled per person). 'Favourite "
            "areas' are the top-level directories (up to 2 levels deep) where the person changed the most "
            "lines, ignored files excluded."
        )
        table = self.query_one(DataTable)
        table.clear(columns=True)
        table.add_columns("Author", "Activity", "Commits", "Active days", "Night owl %", "Weekend %", "Favourite areas")
        board = report.leaderboard("commits")
        for author, _ in board:
            v, x = report.values[author], report.extras.get(author, {})
            table.add_row(
                author,
                Text(sparkline(x.get("activity", [])), style="green"),
                f"{int(v['commits']):,}",
                f"{int(v.get('active_days', 0)):,}",
                f"{v['night_owl_pct']:.0f}%" if "night_owl_pct" in v else "—",
                f"{v['weekend_pct']:.0f}%" if "weekend_pct" in v else "—",
                ", ".join(f"{area} ({n:,})" for area, n in x.get("areas", [])),
                key=author,
            )


class ProfileScreen(ModalScreen):
    BINDINGS = [Binding("escape,q", "dismiss", "Close"), Binding("u", "split", "Undo joins into this person")]
    DEFAULT_CSS = """
    ProfileScreen { align: center middle; }
    ProfileScreen > Vertical { width: 110; height: 90%; border: thick $accent; background: $surface; padding: 1 2; }
    ProfileScreen DataTable { height: 1fr; }
    """

    def __init__(self, report: Report, author: str, ds: Dataset):
        super().__init__()
        self.report, self.author, self.ds = report, author, ds

    def compose(self) -> ComposeResult:
        r, a = self.report, self.author
        v, x = r.values.get(a, {}), r.extras.get(a, {})
        lines = [f"[b]{escape(a)}[/b]", f"[dim]{describe_range(r.since, r.until)}[/dim]"]
        if aka := self.ds.identities_of(a):
            lines.append("Also counted as: " + escape(", ".join(aka)))
        joined = [k for k, t in self.ds.cfg.join.items() if t.lower() == a.lower()]
        if joined:
            lines.append(f"[dim]Joined in .blamest.toml: {escape(', '.join(joined))} — press u to undo[/dim]")
        if "activity" in x:
            lines.append(f"Activity: [green]{sparkline(x['activity'])}[/green]")
        if x.get("areas"):
            lines.append("Favourite areas: " + ", ".join(f"{d} ({n:,} lines)" for d, n in x["areas"]))
        if "fastest_pr" in x:
            num, title, hours = x["fastest_pr"]
            lines.append(f"Fastest reviewed PR: #{num} {escape(title)} — merged in {fmt_duration(hours)}")
        awards = [
            m for m in r.metrics
            if m.award and (r.rank(a, m.key) or (0,))[0] == 1 and (m.ascending or v.get(m.key, 0) > 0)
        ]
        if awards:
            lines.append("Awards: " + "  ".join(f"{m.award.emoji} {m.award.name}" for m in awards))
        with Vertical():
            yield Static("\n".join(lines))
            yield Label("")
            table = VimDataTable(zebra_stripes=True, cursor_type="row")
            table.add_columns("Category", "Metric", "Value", "Rank")
            for m in r.metrics:
                rank = r.rank(a, m.key)
                table.add_row(
                    m.category, m.title, m.fmt(v[m.key]) if m.key in v else "—",
                    f"{rank_label(rank[0])} of {rank[1]}" if rank else "—",
                )
            yield table
            yield Label("[dim]Esc to close · u undoes joins into this person · metric definitions are in each tab[/dim]")

    def action_split(self) -> None:
        removed = self.ds.cfg.remove_joins_into(self.author)
        if not removed:
            self.notify("Nothing was joined into this person.", severity="warning")
            return
        self.notify(f"Split {', '.join(removed)} from {self.author}")
        self.dismiss()
        self.app.recompute()


class JoinScreen(ModalScreen[str | None]):
    """Pick the person the highlighted identity should be counted as."""

    BINDINGS = [
        Binding("escape", "dismiss(None)", "Cancel"),
        Binding("down,ctrl+n", "move(1)", "Next", show=False),
        Binding("up,ctrl+p", "move(-1)", "Previous", show=False),
    ]
    DEFAULT_CSS = """
    JoinScreen { align: center middle; }
    JoinScreen > Vertical { width: 80; height: 80%; border: thick $accent; background: $surface; padding: 1 2; }
    JoinScreen Static { width: 100%; margin-bottom: 1; }
    JoinScreen OptionList { height: 1fr; }
    """

    def __init__(self, person: str, candidates: list[str]):
        super().__init__()
        self.person, self.candidates = person, candidates

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(
                f"[b]Join {escape(self.person)} into…[/b]\n"
                f"Everything counted for {escape(self.person)} (commits, PRs, reviews, comments, churn) will be "
                "counted for the person you pick. It's saved in the [join] table of .blamest.toml; press u in "
                "the target's profile to undo.\n[dim]Type to filter · ↑/↓ or Ctrl+n/Ctrl+p to move · Enter to pick[/dim]"
            )
            yield Input(placeholder="type to filter")
            yield VimOptionList(*[Option(c, id=c) for c in self.candidates])

    def action_move(self, step: int) -> None:
        options = self.query_one(OptionList)
        if options.option_count:
            current = options.highlighted if options.highlighted is not None else -step
            options.highlighted = max(0, min(options.option_count - 1, current + step))

    @on(Input.Changed)
    def _filter(self, event: Input.Changed) -> None:
        options = self.query_one(OptionList)
        options.clear_options()
        needle = event.value.lower()
        options.add_options([Option(c, id=c) for c in self.candidates if needle in c.lower()])
        if options.option_count:
            options.highlighted = 0

    @on(Input.Submitted)
    def _submit_first(self) -> None:
        options = self.query_one(OptionList)
        if options.option_count:
            self.dismiss(options.get_option_at_index(options.highlighted or 0).id)

    @on(OptionList.OptionSelected)
    def _picked(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(event.option.id)


class DateRangeScreen(ModalScreen[tuple[datetime | None, datetime | None] | None]):
    BINDINGS = [Binding("escape", "dismiss(None)", "Cancel")]
    DEFAULT_CSS = """
    DateRangeScreen { align: center middle; }
    DateRangeScreen > Vertical { width: 70; height: auto; border: thick $accent; background: $surface; padding: 1 2; }
    DateRangeScreen Input { margin-bottom: 1; }
    DateRangeScreen Horizontal { height: auto; }
    DateRangeScreen Static { width: 100%; }
    DateRangeScreen #error { color: $error; }
    """

    def __init__(self, since: datetime | None, until: datetime | None):
        super().__init__()
        self.since, self.until = since, until

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static("[b]Date range[/b]\nUse YYYY-MM-DD or relative values like 30d, 12w, 6m, 1y. Leave empty for no limit.\n")
            yield Label("Since")
            yield Input(self.since.date().isoformat() if self.since else "", id="since", placeholder="e.g. 2026-01-01 or 90d")
            yield Label("Until")
            yield Input(self.until.date().isoformat() if self.until else "", id="until", placeholder="empty = today")
            yield Label("", id="error")
            with Horizontal():
                yield Button("Apply", variant="primary", id="apply")
                yield Button("Cancel", id="cancel")

    @on(Input.Submitted)
    @on(Button.Pressed, "#apply")
    def _apply(self) -> None:
        try:
            since = parse_date(self.query_one("#since", Input).value)
            until = parse_date(self.query_one("#until", Input).value, end_of_day=True)
        except ValueError as e:
            self.query_one("#error", Label).update(str(e))
            return
        if since and until and since > until:
            self.query_one("#error", Label).update("'Since' must be before 'Until'.")
            return
        self.dismiss((since, until))

    @on(Button.Pressed, "#cancel")
    def _cancel(self) -> None:
        self.dismiss(None)


class BlamestApp(App):
    TITLE = "blamest"
    CSS = """
    TabPane { padding: 1 1 0 1; }
    #busy { height: 3; display: none; }
    #busy.visible { display: block; }
    """
    BINDINGS = [
        Binding("d", "date_range", "Date range"),
        Binding("J", "join", "Join person"),
        Binding("h", "switch_tab(-1)", "Previous tab", show=False),
        Binding("l", "switch_tab(1)", "Next tab", show=False),
        Binding("left", "switch_tab(-1)", show=False),
        Binding("right", "switch_tab(1)", show=False),
        Binding("question_mark", "glossary", "All metrics"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self, ds: Dataset, since: datetime | None, until: datetime | None):
        super().__init__()
        self.ds, self.since, self.until = ds, since, until
        self.report = compute(ds, since, until)

    def compose(self) -> ComposeResult:
        yield Header()
        yield LoadingIndicator(id="busy")
        with TabbedContent():
            with TabPane("🏆 Awards", id="awards"):
                yield AwardsPane()
            with TabPane("Overview", id="overview"):
                yield OverviewPane()
            for cat in CATEGORIES:
                with TabPane(cat, id=cat.lower().replace(" ", "-")):
                    yield CategoryPane(cat)
            with TabPane("Activity", id="activity"):
                yield ActivityPane()
        yield Footer()

    def on_mount(self) -> None:
        self.refresh_panes()
        self._focus_active_tab()

    def _on_dashboard(self) -> bool:
        """False while a profile, dialog or the glossary is open on top."""
        return len(self.screen_stack) == 1

    def action_switch_tab(self, step: int) -> None:
        if not self._on_dashboard():
            return
        tabs = self.query_one(TabbedContent)
        ids = [pane.id for pane in tabs.query(TabPane)]
        tabs.active = ids[(ids.index(tabs.active) + step) % len(ids)]

    @on(TabbedContent.TabActivated)
    def _tab_changed(self) -> None:
        self.call_after_refresh(self._focus_active_tab)

    def _focus_active_tab(self) -> None:
        """Put focus inside the tab (metric list or table) so j/k work right away."""
        tabs = self.query_one(TabbedContent)
        pane = tabs.get_pane(tabs.active)
        target = next(iter(pane.query("VimOptionList, VimDataTable")), None)
        if target is not None:
            target.focus()

    def refresh_panes(self) -> None:
        self.sub_title = f"{self.ds.repo.slug} · {describe_range(self.since, self.until)} · churn window {self.report.churn_days}d"
        for pane in self.query("AwardsPane, OverviewPane, CategoryPane, ActivityPane"):
            pane.update_report(self.report)

    # --- actions ------------------------------------------------------------------------------

    @on(DataTable.RowSelected)
    def _row_selected(self, event: DataTable.RowSelected) -> None:
        if event.row_key.value and not isinstance(self.screen, ProfileScreen):
            self.push_screen(ProfileScreen(self.report, event.row_key.value.split("\x00")[0], self.ds))

    def _highlighted_person(self) -> str | None:
        table = self.focused if isinstance(self.focused, DataTable) else None
        if table is None or table.row_count == 0:
            return None
        key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
        return key.split("\x00")[0] if key else None

    def action_join(self) -> None:
        if not self._on_dashboard():
            return
        person = self._highlighted_person()
        if not person:
            self.notify("Highlight a person in a table first, then press j.", severity="warning")
            return
        people = {self.ds.canon(p["author"]) for p in self.ds.prs if not self.ds.cfg.is_bot(p["author"], p.get("author_type"))}
        people |= {self.ds.who(e) for e in self.ds.names if not self.ds.email_is_bot(e)}
        candidates = sorted(people - {person}, key=str.lower)

        def chosen(target: str | None) -> None:
            if not target:
                return
            try:
                self.ds.cfg.add_join(person, target)
            except OSError as e:
                self.notify(f"Couldn't write .blamest.toml: {e}", severity="error")
                return
            self.notify(f"{person} now counts as {target}")
            self.recompute()

        self.push_screen(JoinScreen(person, candidates), chosen)

    def action_glossary(self) -> None:
        self.push_screen(GlossaryScreen(self.report))

    def action_date_range(self) -> None:
        def chosen(result):
            if result is not None:
                self.since, self.until = result
                self.recompute()

        self.push_screen(DateRangeScreen(self.since, self.until), chosen)

    @work(thread=True, exclusive=True)
    def recompute(self) -> None:
        self.call_from_thread(self.query_one("#busy").add_class, "visible")
        try:
            commits = landed_in_range(self.ds, self.since, self.until)
            self.ds.ensure_churn(
                commits,
                lambda stage, done, total: self.call_from_thread(self.notify, f"{stage}: {done}/{total}", timeout=1)
                if total and done % 100 == 0 else None,
            )
            self.report = compute(self.ds, self.since, self.until)
            self.call_from_thread(self.refresh_panes)
        finally:
            self.call_from_thread(self.query_one("#busy").remove_class, "visible")


class GlossaryScreen(ModalScreen):
    BINDINGS = [Binding("escape,q,question_mark", "dismiss", "Close")]
    DEFAULT_CSS = """
    GlossaryScreen { align: center middle; }
    GlossaryScreen > VimScroll { width: 100; height: 90%; border: thick $accent; background: $surface; padding: 1 2; }
    """

    def __init__(self, report: Report):
        super().__init__()
        self.report = report

    def compose(self) -> ComposeResult:
        parts = ["# Every metric, explained\n",
                 "**Keys:** `j`/`k` down/up · `g`/`G` top/bottom · `Ctrl+d`/`Ctrl+u` page · `h`/`l` previous/next "
                 "tab · `Tab` moves between the metric list and its leaderboard · `Enter` opens a profile · "
                 "`J` joins people · `d` date range · `q` quit.\n",
                 "Bots (dependabot, renovate, `*[bot]`, … plus `bots` in `.blamest.toml`) are excluded everywhere. "
                 "People are identified by GitHub login. Commit emails are matched to logins via the GitHub API "
                 "and `.mailmap`; an email GitHub can't match takes the login of another email with the same git "
                 "name. Anything still split can be joined with `j` (saved in the `[join]` table of "
                 "`.blamest.toml`).\n"]
        for cat in CATEGORIES:
            parts.append(f"## {cat}\n")
            for m in self.report.metrics:
                if m.category == cat:
                    award = f" — 🏆 *{m.award.emoji} {m.award.name}*" if m.award else ""
                    parts.append(f"- **{m.title}**{award}: {m.description}")
            parts.append("")
        with VimScroll():
            yield Markdown("\n".join(parts))

    def on_mount(self) -> None:
        self.query_one(VimScroll).focus()
