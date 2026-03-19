from __future__ import annotations

import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from expense_tracker.cli import main
from expense_tracker.tui import ExpenseTrackerApp, render_summary_text


class ExpenseTrackerCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.data_file = Path(self.temp_dir.name) / "expenses.json"

    def test_tui_command_launches_app(self) -> None:
        with patch("expense_tracker.cli.launch_tui") as launch_tui:
            exit_code = main(["--data-file", str(self.data_file), "tui"])

        self.assertEqual(exit_code, 0)
        launch_tui.assert_called_once_with(self.data_file)


class ExpenseTrackerAppTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addAsyncCleanup(self._cleanup_temp_dir)
        self.data_file = Path(self.temp_dir.name) / "expenses.json"

    async def _cleanup_temp_dir(self) -> None:
        self.temp_dir.cleanup()

    async def test_add_expense_from_form_updates_view(self) -> None:
        app = ExpenseTrackerApp(self.data_file)

        async with app.run_test():
            app.query_one("#description").value = "Coffee"
            app.query_one("#amount").value = "4.50"
            app.query_one("#category").value = "Food"
            app.query_one("#spent-on").value = "2026-03-19"

            app.add_expense_from_form()

            self.assertEqual(len(app._row_ids), 1)
            self.assertIn("Food", str(app.query_one("#summary").renderable))
            self.assertIn("Added expense", str(app.query_one("#status").renderable))

    async def test_delete_selected_expense_removes_item(self) -> None:
        app = ExpenseTrackerApp(self.data_file)

        async with app.run_test():
            app.tracker.add_expense(
                description="Bus ticket",
                amount="2.75",
                category="Travel",
                spent_on="2026-03-19",
            )
            app.refresh_view()

            app.delete_selected_expense()

            self.assertEqual(app._row_ids, [])
            self.assertIn("Deleted expense", str(app.query_one("#status").renderable))


class TuiRenderingTests(unittest.TestCase):
    def test_render_summary_text_formats_totals(self) -> None:
        rendered = render_summary_text(
            {"Food": Decimal("18.00"), "Travel": Decimal("8.00")}
        )

        self.assertIn("Category totals:", rendered)
        self.assertIn("- Food: $18.00", rendered)
        self.assertIn("Grand total: $26.00", rendered)
