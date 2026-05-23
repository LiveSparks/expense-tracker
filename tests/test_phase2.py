"""Tests for Phase 2: transaction time ordering and weekday date labels."""

from __future__ import annotations

import tempfile
import time
import unittest
from datetime import date, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from expense_tracker.formatting import format_list_date
from expense_tracker.tracker import ExpenseTracker
from expense_tracker.web import create_app


# ---------------------------------------------------------------------------
# format_list_date weekday label tests
# ---------------------------------------------------------------------------


class FormatListDateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.today = date(2026, 5, 23)  # Saturday

    def test_today_shows_today_prefix(self) -> None:
        result = format_list_date(self.today, today=self.today)
        self.assertTrue(result.startswith("Today, "), f"Expected 'Today, …', got: {result!r}")

    def test_yesterday_shows_yesterday_prefix(self) -> None:
        yesterday = self.today - timedelta(days=1)
        result = format_list_date(yesterday, today=self.today)
        self.assertTrue(result.startswith("Yesterday, "), f"Expected 'Yesterday, …', got: {result!r}")

    def test_two_days_ago_shows_weekday_name(self) -> None:
        two_days_ago = self.today - timedelta(days=2)
        result = format_list_date(two_days_ago, today=self.today)
        # two_days_ago = 2026-05-21 (Thursday)
        self.assertTrue(result.startswith("Thursday, "), f"Expected 'Thursday, …', got: {result!r}")

    def test_six_days_ago_shows_weekday_name(self) -> None:
        six_days_ago = self.today - timedelta(days=6)
        result = format_list_date(six_days_ago, today=self.today)
        # six_days_ago = 2026-05-17 (Sunday)
        self.assertTrue(result.startswith("Sunday, "), f"Expected 'Sunday, …', got: {result!r}")

    def test_seven_days_ago_shows_plain_date(self) -> None:
        seven_days_ago = self.today - timedelta(days=7)
        result = format_list_date(seven_days_ago, today=self.today)
        # Should NOT have a weekday prefix
        self.assertNotIn("Today", result)
        self.assertNotIn("Yesterday", result)
        for day in ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"):
            self.assertNotIn(day, result)

    def test_old_date_shows_plain_format(self) -> None:
        old_date = date(2025, 1, 15)
        result = format_list_date(old_date, today=self.today)
        self.assertIn("January", result)
        self.assertIn("15", result)
        self.assertIn("2025", result)

    def test_accepts_string_iso_date(self) -> None:
        result = format_list_date("2026-05-23", today=self.today)
        self.assertTrue(result.startswith("Today, "))

    def test_accepts_date_object(self) -> None:
        result = format_list_date(date(2026, 5, 23), today=self.today)
        self.assertTrue(result.startswith("Today, "))


# ---------------------------------------------------------------------------
# Transaction ordering by created_at tests
# ---------------------------------------------------------------------------


class TransactionTimeOrderingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.data_file = Path(self.temp_dir.name) / "ledger.db"
        self.tracker = ExpenseTracker(self.data_file)

    def test_same_day_transactions_ordered_by_created_at(self) -> None:
        """Transactions on the same day should be ordered newest-created first."""
        self.tracker.add_transaction(
            account_name="Cash",
            payee_name="First shop",
            category_name="General",
            subcategory_name="Grocery",
            amount="-10.00",
            spent_on="2026-03-20",
        )
        # Small sleep to ensure different created_at timestamps
        time.sleep(0.02)
        self.tracker.add_transaction(
            account_name="Cash",
            payee_name="Second shop",
            category_name="General",
            subcategory_name="Grocery",
            amount="-20.00",
            spent_on="2026-03-20",
        )

        transactions = self.tracker.list_transactions()
        # newest created_at should come first
        self.assertEqual(len(transactions), 2)
        self.assertEqual(transactions[0].payee_name, "Second shop")
        self.assertEqual(transactions[1].payee_name, "First shop")

    def test_transaction_record_has_created_at_field(self) -> None:
        self.tracker.add_transaction(
            account_name="Cash",
            payee_name="Amazon",
            category_name="General",
            subcategory_name="Delivery",
            amount="-50.00",
            spent_on="2026-03-20",
        )
        records = self.tracker.list_transactions()
        self.assertEqual(len(records), 1)
        self.assertIsNotNone(records[0].created_at)

    def test_different_dates_ordered_by_date_desc(self) -> None:
        self.tracker.add_transaction(
            account_name="Cash",
            payee_name="Earlier",
            category_name="General",
            subcategory_name="Grocery",
            amount="-5.00",
            spent_on="2026-03-01",
        )
        self.tracker.add_transaction(
            account_name="Cash",
            payee_name="Later",
            category_name="General",
            subcategory_name="Grocery",
            amount="-5.00",
            spent_on="2026-03-20",
        )

        transactions = self.tracker.list_transactions()
        self.assertEqual(transactions[0].payee_name, "Later")
        self.assertEqual(transactions[1].payee_name, "Earlier")


# ---------------------------------------------------------------------------
# Transaction time visible in edit view only
# ---------------------------------------------------------------------------


class TransactionTimeVisibilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.data_file = Path(self.temp_dir.name) / "ledger.db"
        self.client = TestClient(create_app(self.data_file, process_reviews_inline=True))

    def test_created_at_time_visible_in_edit_view(self) -> None:
        self.client.post(
            "/api/transactions",
            data={
                "account_name": "Cash",
                "payee_name": "Amazon",
                "category_value": "General / Delivery",
                "amount": "-50.00",
                "spent_on": "2026-03-20",
            },
        )
        tracker = ExpenseTracker(self.data_file)
        created = tracker.list_transactions()[0]

        response = self.client.get(f"/transactions/{created.id}/edit")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Created at", response.text)

    def test_transaction_list_shows_relative_date_for_today(self) -> None:
        from datetime import date as ddate
        today_str = ddate.today().isoformat()
        self.client.post(
            "/api/transactions",
            data={
                "account_name": "Cash",
                "payee_name": "TodayShop",
                "category_value": "General / Grocery",
                "amount": "-10.00",
                "spent_on": today_str,
            },
        )
        response = self.client.get("/transactions")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Today,", response.text)


if __name__ == "__main__":
    unittest.main()
