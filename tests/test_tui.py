from __future__ import annotations

import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from expense_tracker.cli import main
from expense_tracker.tui import (
    ExpenseTrackerApp,
    SuggestionItem,
    render_balances_text,
    render_category_totals_text,
    split_category_value,
)


class ExpenseTrackerCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.data_file = Path(self.temp_dir.name) / "ledger.json"

    def test_tui_command_launches_app(self) -> None:
        with patch("expense_tracker.cli.launch_tui") as launch_tui:
            exit_code = main(["--data-file", str(self.data_file), "tui"])

        self.assertEqual(exit_code, 0)
        launch_tui.assert_called_once_with(self.data_file)


class ExpenseTrackerAppTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addAsyncCleanup(self._cleanup_temp_dir)
        self.data_file = Path(self.temp_dir.name) / "ledger.json"

    async def _cleanup_temp_dir(self) -> None:
        self.temp_dir.cleanup()

    async def test_add_transaction_from_form_updates_view(self) -> None:
        app = ExpenseTrackerApp(self.data_file)

        async with app.run_test():
            app.query_one("#payee-field").value = "Amazon"
            app.query_one("#account-field").value = "Cash"
            app.query_one("#category-field").value = "General / Delivery"
            app.query_one("#amount").value = "-4.50"
            app.query_one("#spent-on").value = "2026-03-19"
            app.query_one("#notes").value = "Snacks"

            app.add_transaction_from_form()

            self.assertEqual(len(app._row_ids), 1)
            self.assertIn("Cash", str(app.query_one("#balances").renderable))
            self.assertIn("Added transaction", str(app.query_one("#status").renderable))

    async def test_filters_can_reduce_visible_rows(self) -> None:
        app = ExpenseTrackerApp(self.data_file)

        async with app.run_test():
            app.tracker.add_transaction(
                account_name="Cash",
                payee_name="Amazon",
                category_name="General",
                amount="-5.00",
                spent_on="2026-03-10",
            )
            app.tracker.add_transaction(
                account_name="Credit",
                payee_name="Pharmacy",
                category_name="Medical",
                amount="-20.00",
                spent_on="2026-03-12",
            )
            app.query_one("#filter-account").value = "Credit"
            app.refresh_view()

            self.assertEqual(len(app._row_ids), 1)
            self.assertIn("Credit", str(app.query_one("#transaction-table").get_row_at(0)))

    async def test_picker_suggestions_include_add_new_option(self) -> None:
        app = ExpenseTrackerApp(self.data_file)

        async with app.run_test():
            suggestions = app._build_name_suggestions("Zeta", ["Cash", "Credit"], "Account")

            self.assertIn(SuggestionItem(label="Add new account: Zeta", value="Zeta"), suggestions)


class TuiRenderingTests(unittest.TestCase):
    def test_render_summary_text_formats_totals(self) -> None:
        rendered = render_category_totals_text(
            {"General": Decimal("-18.00"), "Income": Decimal("26.00")}
        )

        self.assertIn("Category totals:", rendered)
        self.assertIn("General: $-18.00", rendered)

    def test_render_balances_text_formats_amounts(self) -> None:
        rendered = render_balances_text({"Cash": Decimal("-12.50")})

        self.assertIn("Account balances:", rendered)
        self.assertIn("Cash: $-12.50", rendered)

    def test_split_category_value_handles_subcategory(self) -> None:
        self.assertEqual(split_category_value("General / Grocery"), ("General", "Grocery"))
        self.assertEqual(split_category_value("Medical"), ("Medical", None))


if __name__ == "__main__":
    unittest.main()
