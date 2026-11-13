import asyncio

from textual.widgets import DataTable, OptionList, TabbedContent

from blamest.tui import BlamestApp, GlossaryScreen, ProfileScreen

from test_metrics import ds  # noqa: F401  (fixture)


def test_dashboard_renders_every_tab_and_profile(ds):  # noqa: F811
    async def scenario():
        app = BlamestApp(ds, None, None)
        async with app.run_test(size=(160, 50)) as pilot:
            tabs = app.query_one(TabbedContent)
            for pane_id in ("awards", "overview", "commits", "pull-requests", "reviews", "comments", "churn", "activity"):
                tabs.active = pane_id
                await pilot.pause()
                table = tabs.get_pane(pane_id).query_one(DataTable)
                assert table.row_count > 0, pane_id

            tabs.active = "overview"
            await pilot.pause()
            tabs.get_pane("overview").query_one(DataTable).focus()
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, ProfileScreen)
            await pilot.press("escape")
            await pilot.pause()

            await pilot.press("question_mark")
            await pilot.pause()
            assert isinstance(app.screen, GlossaryScreen)

    asyncio.run(scenario())


def test_changing_date_range_recomputes(ds):  # noqa: F811
    async def scenario():
        app = BlamestApp(ds, None, None)
        async with app.run_test(size=(160, 50)) as pilot:
            await pilot.press("d")
            await pilot.pause()
            await pilot.press(*"2026-01-30", "enter")  # into "since"
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert app.since is not None
            assert app.report.values["alice"]["commits"] == 1
            assert "bob" not in app.report.values
            assert "2026-01-30" in app.sub_title

            await pilot.press("d")
            await pilot.pause()
            app.screen.query_one("#since").value = "2030-01-01"  # a range with no data at all
            await pilot.press("enter")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert app.report.values == {}

    asyncio.run(scenario())


def test_join_from_dashboard_and_undo_from_profile(ds, tmp_path):  # noqa: F811
    ds.cfg.path = tmp_path / ".blamest.toml"

    async def scenario():
        app = BlamestApp(ds, None, None)
        async with app.run_test(size=(160, 50)) as pilot:
            tabs = app.query_one(TabbedContent)
            tabs.active = "overview"
            await pilot.pause()
            table = tabs.get_pane("overview").query_one(DataTable)
            table.focus()
            table.move_cursor(row=table.get_row_index("carol"))
            await pilot.press("J")
            await pilot.pause()
            await pilot.press(*"alice", "enter")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert "carol" not in app.report.values
            assert 'carol = "alice"' in ds.cfg.path.read_text()

            table.move_cursor(row=table.get_row_index("alice"))
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, ProfileScreen)
            await pilot.press("u")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert "carol" in app.report.values

    asyncio.run(scenario())


def test_overview_sorting_keeps_missing_values_last(ds):  # noqa: F811
    async def scenario():
        app = BlamestApp(ds, None, None)
        async with app.run_test(size=(220, 50)) as pilot:
            tabs = app.query_one(TabbedContent)
            tabs.active = "overview"
            await pilot.pause()
            pane = tabs.get_pane("overview").query_one("OverviewPane")
            table = pane.query_one(DataTable)

            def order():
                return [table.coordinate_to_cell_key((i, 0)).row_key.value for i in range(table.row_count)]

            assert pane.sort_key == "prs_merged" and order()[0] == "alice"
            pane.sort_key, pane.sort_desc = "commits", False
            pane._build()
            assert order()[:2] == ["bob", "alice"] and order()[-1] == "carol"  # '—' last even ascending
            labels = {key.value: str(col.label) for key, col in table.columns.items()}
            assert labels["commits"].endswith("▲") and not labels["prs_merged"].endswith(("▲", "▼"))

    asyncio.run(scenario())


def test_vim_navigation(ds):  # noqa: F811
    async def scenario():
        app = BlamestApp(ds, None, None)
        async with app.run_test(size=(160, 50)) as pilot:
            await pilot.pause()
            tabs = app.query_one(TabbedContent)
            assert tabs.active == "awards" and isinstance(app.focused, DataTable)  # focus starts in the content

            await pilot.press("l")
            await pilot.pause()
            assert tabs.active == "overview" and isinstance(app.focused, DataTable)
            table = app.focused
            await pilot.press("j", "j")
            assert table.cursor_row == 2
            await pilot.press("k")
            assert table.cursor_row == 1
            await pilot.press("G")
            assert table.cursor_row == table.row_count - 1
            await pilot.press("g")
            assert table.cursor_row == 0

            await pilot.press("l")  # Commits tab: focus lands on the metric list, j/k change the metric
            await pilot.pause()
            pane = tabs.get_pane("commits").query_one("CategoryPane")
            assert app.focused is pane.query_one(OptionList)
            await pilot.press("j")
            await pilot.pause()
            assert pane.current == "lines_added"

            await pilot.press("h", "h")
            await pilot.pause()
            assert tabs.active == "awards"
            await pilot.press("h")  # wraps around
            await pilot.pause()
            assert tabs.active == "activity"
            await pilot.press("right")  # arrows still switch tabs from inside a table
            await pilot.pause()
            assert tabs.active == "awards"

            await pilot.press("enter")  # profile: j/k move, h/l don't switch tabs underneath
            await pilot.pause()
            assert isinstance(app.screen, ProfileScreen)
            profile_table = app.screen.query_one(DataTable)
            await pilot.press("j", "l")
            await pilot.pause()
            assert profile_table.cursor_row == 1 and tabs.active == "awards"
            await pilot.press("q")
            await pilot.pause()
            assert not isinstance(app.screen, ProfileScreen)

    asyncio.run(scenario())
