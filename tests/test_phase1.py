"""Tests for Phase 1: migrations, SMS logs, exact merchant match, review UX."""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from expense_tracker.config import AppConfig
from expense_tracker.migrations import MIGRATIONS, apply_migrations, current_version
from expense_tracker.review_workflow import ReviewWorkflowStore
from expense_tracker.sms_history import SmsHistoryStore
from expense_tracker.sms_pipeline import SmsMessage, extract_sms_markers
from expense_tracker.sqlite_utils import sqlite_connection
from expense_tracker.tracker import ExpenseTracker
from expense_tracker.web import create_app


# ---------------------------------------------------------------------------
# Migration framework tests
# ---------------------------------------------------------------------------


class MigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.data_file = Path(self.temp_dir.name) / "ledger.db"

    def test_current_version_returns_zero_for_new_file(self) -> None:
        self.assertEqual(current_version(self.data_file), 0)

    def test_apply_migrations_creates_schema_version_table(self) -> None:
        apply_migrations(self.data_file)
        with sqlite_connection(self.data_file) as connection:
            row = connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
            ).fetchone()
        self.assertIsNotNone(row)

    def test_apply_migrations_records_applied_versions(self) -> None:
        newly = apply_migrations(self.data_file)
        self.assertEqual(set(newly), {m[0] for m in MIGRATIONS})
        self.assertEqual(current_version(self.data_file), max(m[0] for m in MIGRATIONS))

    def test_apply_migrations_is_idempotent(self) -> None:
        apply_migrations(self.data_file)
        second = apply_migrations(self.data_file)
        # No new migrations applied on second run
        self.assertEqual(second, [])

    def test_migration_1_creates_sms_log_table(self) -> None:
        apply_migrations(self.data_file)
        with sqlite_connection(self.data_file) as connection:
            row = connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='sms_log'"
            ).fetchone()
        self.assertIsNotNone(row)

    def test_migration_applied_to_existing_db_without_data_loss(self) -> None:
        """Simulates upgrading an existing DB that already has the core schema."""
        # Create core schema without migrations (simulates old DB)
        with sqlite_connection(self.data_file) as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS sms_reviews (id TEXT PRIMARY KEY, sender TEXT NOT NULL)"
            )
            connection.execute("INSERT INTO sms_reviews VALUES ('rev-1', 'HDFC')")

        # Now apply migrations (should add sms_log without touching existing data)
        apply_migrations(self.data_file)

        with sqlite_connection(self.data_file) as connection:
            row = connection.execute("SELECT id FROM sms_reviews WHERE id = 'rev-1'").fetchone()
            log_table = connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='sms_log'"
            ).fetchone()

        self.assertIsNotNone(row)
        self.assertIsNotNone(log_table)


# ---------------------------------------------------------------------------
# SMS log tests
# ---------------------------------------------------------------------------


class SmsLogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.data_file = Path(self.temp_dir.name) / "ledger.db"
        self.store = ReviewWorkflowStore(self.data_file)

    def test_enqueue_creates_intake_log_entry(self) -> None:
        with patch("expense_tracker.review_workflow.OpenAIDraftClient.is_configured", return_value=False):
            self.store.enqueue_review(
                sender="JM-HDFCBK-S",
                content="Sent Rs.500.00 From HDFC Bank A/C *2054 To Amazon On 20/03/26 Ref 12345",
                received_at="2026-03-20T10:00:00Z",
            )

        entries = self.store.list_log_entries()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["event_type"], "intake")
        self.assertEqual(entries[0]["sender"], "JM-HDFCBK-S")

    def test_enqueue_filtered_creates_filtered_log_entry(self) -> None:
        with patch("expense_tracker.review_workflow.OpenAIDraftClient.is_configured", return_value=False):
            self.store.enqueue_review(
                sender="AD-PROMO",
                content="Your OTP is 123456. Do not share this with anyone.",
                received_at="2026-03-20T10:00:00Z",
            )

        entries = self.store.list_log_entries()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["event_type"], "filtered")

    def test_approve_creates_approved_log_entry(self) -> None:
        tracker = ExpenseTracker(self.data_file)
        tracker.add_transaction(
            account_name="Cash",
            payee_name="Amazon",
            category_name="General",
            subcategory_name="Delivery",
            amount="-100.00",
            spent_on="2026-03-20",
        )
        with patch("expense_tracker.review_workflow.OpenAIDraftClient.is_configured", return_value=False):
            review = self.store.create_review(
                sender="JM-HDFCBK-S",
                content="Sent Rs.100.00 From HDFC Bank A/C *2054 To Amazon On 20/03/26 Ref 11111",
                received_at="2026-03-20T10:00:00Z",
            )
        review.review_status = "for_review"
        # Force status to for_review so approval works
        with sqlite_connection(self.data_file) as connection:
            connection.execute(
                "UPDATE sms_reviews SET review_status = 'for_review' WHERE id = ?",
                (review.id,),
            )

        self.store.approve_review(
            review.id,
            account_name="Cash",
            payee_name="Amazon",
            category_value="General / Delivery",
            amount="-100.00",
            spent_on="2026-03-20",
            notes="",
        )

        approved_entries = [e for e in self.store.list_log_entries() if e["event_type"] == "approved"]
        self.assertEqual(len(approved_entries), 1)
        self.assertEqual(approved_entries[0]["event_details"]["payee_name"], "Amazon")

    def test_delete_creates_deleted_log_entry(self) -> None:
        with patch("expense_tracker.review_workflow.OpenAIDraftClient.is_configured", return_value=False):
            review = self.store.enqueue_review(
                sender="JM-HDFCBK-S",
                content="Sent Rs.88.00 From HDFC Bank A/C *2054 To Shop On 20/03/26 Ref 9988",
                received_at="2026-03-20T10:00:00Z",
            )
        self.store.delete_review(review.id)

        deleted_entries = [e for e in self.store.list_log_entries() if e["event_type"] == "deleted"]
        self.assertEqual(len(deleted_entries), 1)

    def test_list_log_entries_filter_by_event_type(self) -> None:
        with patch("expense_tracker.review_workflow.OpenAIDraftClient.is_configured", return_value=False):
            self.store.enqueue_review(
                sender="JM-HDFCBK-S",
                content="Sent Rs.50.00 From HDFC Bank A/C *2054 To Shop On 20/03/26 Ref 111",
                received_at="2026-03-20T09:00:00Z",
            )
            self.store.enqueue_review(
                sender="AD-PROMO",
                content="Your OTP is 000000. Do not share.",
                received_at="2026-03-20T10:00:00Z",
            )

        intake_entries = self.store.list_log_entries(event_type="intake")
        filtered_entries = self.store.list_log_entries(event_type="filtered")
        self.assertEqual(len(intake_entries), 1)
        self.assertEqual(len(filtered_entries), 1)

    def test_log_entry_count(self) -> None:
        with patch("expense_tracker.review_workflow.OpenAIDraftClient.is_configured", return_value=False):
            self.store.enqueue_review(
                sender="JM-HDFCBK-S",
                content="Sent Rs.50.00 From HDFC Bank A/C *2054 To Shop On 20/03/26 Ref 222",
                received_at="2026-03-20T09:00:00Z",
            )
        self.assertEqual(self.store.log_entry_count(), 1)
        self.assertEqual(self.store.log_entry_count(event_type="intake"), 1)
        self.assertEqual(self.store.log_entry_count(event_type="approved"), 0)

    def test_process_queue_creates_processed_log_entry(self) -> None:
        with patch("expense_tracker.review_workflow.OpenAIDraftClient.is_configured", return_value=False):
            self.store.enqueue_review(
                sender="JM-HDFCBK-S",
                content="Sent Rs.75.00 From HDFC Bank A/C *2054 To Amazon On 20/03/26 Ref 333",
                received_at="2026-03-20T08:00:00Z",
            )
        with patch("expense_tracker.review_workflow.OpenAIDraftClient.is_configured", return_value=False):
            self.store.process_pending_queue()

        processed_entries = [e for e in self.store.list_log_entries() if e["event_type"] == "processed"]
        self.assertEqual(len(processed_entries), 1)


# ---------------------------------------------------------------------------
# Logs web endpoint tests
# ---------------------------------------------------------------------------


class LogsWebTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.data_file = Path(self.temp_dir.name) / "ledger.db"
        self.client = TestClient(create_app(self.data_file, process_reviews_inline=True))

    def test_logs_page_is_accessible(self) -> None:
        response = self.client.get("/logs")
        self.assertEqual(response.status_code, 200)
        self.assertIn("SMS Logs", response.text)

    def test_logs_page_shows_intake_entries(self) -> None:
        with patch("expense_tracker.review_workflow.OpenAIDraftClient.is_configured", return_value=False):
            self.client.post(
                "/api/sms/intake",
                json={
                    "sender": "JM-HDFCBK-S",
                    "content": "Sent Rs.88.00 From HDFC Bank A/C *2054 To Amazon On 20/03/26 Ref 12345",
                    "received_at": "2026-03-20T10:00:00Z",
                },
            )
        response = self.client.get("/logs")
        self.assertEqual(response.status_code, 200)
        self.assertIn("JM-HDFCBK-S", response.text)

    def test_logs_page_filter_by_event_type(self) -> None:
        response = self.client.get("/logs", params={"event_type": "filtered"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("Showing: filtered", response.text)

    def test_logs_sidebar_link_exists(self) -> None:
        response = self.client.get("/")
        self.assertIn('href="/logs"', response.text)


# ---------------------------------------------------------------------------
# Exact merchant match tests
# ---------------------------------------------------------------------------


class ExactMerchantMatchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.data_file = Path(self.temp_dir.name) / "ledger.db"
        self.tracker = ExpenseTracker(self.data_file)
        self.sms_csv = Path(self.temp_dir.name) / "conversations.csv"

    def _write_csv(self, rows: list[str]) -> None:
        header = [
            "Exported on 2026-03-20 10:33 with SMS Exporter android app https://smartpositive.com/sms-exporter",
            "",
            "Date,Time,Direction,Contact,Phone,Content,Type",
        ]
        self.sms_csv.write_text("\n".join(header + rows), encoding="utf-8")

    def test_exact_merchant_match_works(self) -> None:
        self.tracker.add_transaction(
            account_name="HDFC Savings 2054",
            payee_name="Online shopping",
            category_name="General",
            subcategory_name="Delivery",
            amount="-499.00",
            spent_on="2026-03-20",
        )
        self._write_csv([
            "2026-03-20,09:01:10,Received,JM-HDFCBK-S,JM-HDFCBK-S,"
            "Sent Rs.499.00 From HDFC Bank A/C *2054 To Amazon Seller Services On 20/03/26 Ref 111,SMS",
        ])
        store = SmsHistoryStore(self.data_file)
        store.import_csv(self.sms_csv, self.tracker.list_transactions())

        # Exact match - same string
        examples = store.merchant_examples(
            merchant_hint="Amazon Seller Services",
            transactions=self.tracker.list_transactions(),
        )
        self.assertEqual(len(examples), 1)

    def test_partial_token_match_no_longer_matches(self) -> None:
        """Previously 'Amazon' would match 'Amazon Seller Services' via token overlap."""
        self.tracker.add_transaction(
            account_name="HDFC Savings 2054",
            payee_name="Online shopping",
            category_name="General",
            subcategory_name="Delivery",
            amount="-499.00",
            spent_on="2026-03-20",
        )
        self._write_csv([
            "2026-03-20,09:01:10,Received,JM-HDFCBK-S,JM-HDFCBK-S,"
            "Sent Rs.499.00 From HDFC Bank A/C *2054 To Amazon Seller Services On 20/03/26 Ref 111,SMS",
        ])
        store = SmsHistoryStore(self.data_file)
        store.import_csv(self.sms_csv, self.tracker.list_transactions())

        # Partial match should NOT return results now (exact match only)
        examples = store.merchant_examples(
            merchant_hint="Amazon",
            transactions=self.tracker.list_transactions(),
        )
        self.assertEqual(len(examples), 0)

    def test_case_insensitive_exact_match(self) -> None:
        self.tracker.add_transaction(
            account_name="HDFC Savings 2054",
            payee_name="Online shopping",
            category_name="General",
            subcategory_name="Delivery",
            amount="-499.00",
            spent_on="2026-03-20",
        )
        self._write_csv([
            "2026-03-20,09:01:10,Received,JM-HDFCBK-S,JM-HDFCBK-S,"
            "Sent Rs.499.00 From HDFC Bank A/C *2054 To AMAZON SELLER SERVICES On 20/03/26 Ref 111,SMS",
        ])
        store = SmsHistoryStore(self.data_file)
        store.import_csv(self.sms_csv, self.tracker.list_transactions())

        # Case-insensitive match should still work
        examples = store.merchant_examples(
            merchant_hint="Amazon Seller Services",
            transactions=self.tracker.list_transactions(),
        )
        self.assertEqual(len(examples), 1)


# ---------------------------------------------------------------------------
# Review detail UX tests
# ---------------------------------------------------------------------------


class ReviewDetailUxTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.data_file = Path(self.temp_dir.name) / "ledger.db"
        self.client = TestClient(create_app(self.data_file, process_reviews_inline=True))

    def _create_review_with_history(self) -> str:
        """Create an SMS review with similar history and return the review_id."""
        tracker = ExpenseTracker(self.data_file)
        tracker.add_transaction(
            account_name="Cash",
            payee_name="Amazon",
            category_name="General",
            subcategory_name="Delivery",
            amount="-88.00",
            spent_on="2026-03-10",
            notes="- Ref: 12345",
        )
        transaction = tracker.list_transactions()[0]
        store = ReviewWorkflowStore(self.data_file)
        from expense_tracker.sms_history import SmsHistoryStore
        historical_sms = SmsMessage(
            row_number=1,
            received_at=datetime(2026, 3, 10, 10, 0, 0),
            direction="Received",
            contact="JM-HDFCBK-S",
            phone="JM-HDFCBK-S",
            content="Sent Rs.88.00 From HDFC Bank A/C *2054 To Amazon On 10/03/26 Ref 12345",
            message_type="SMS",
        )
        SmsHistoryStore(self.data_file).record_approved_review(
            sender=historical_sms.contact,
            phone=historical_sms.phone,
            content=historical_sms.content,
            received_at=historical_sms.received_at,
            markers_payload=extract_sms_markers(historical_sms).to_dict(),
            matched_transaction_id=transaction.id,
        )
        with patch("expense_tracker.review_workflow.OpenAIDraftClient.is_configured", return_value=False):
            response = self.client.post(
                "/api/sms/intake",
                json={
                    "sender": "JM-HDFCBK-S",
                    "content": "Sent Rs.88.00 From HDFC Bank A/C *2054 To Amazon On 19/03/26 Ref 67890",
                    "received_at": "2026-03-19T10:15:00Z",
                },
            )
        return response.json()["review"]["id"]

    def test_action_buttons_appear_above_form_fields(self) -> None:
        review_id = self._create_review_with_history()
        response = self.client.get(f"/reviews/{review_id}", params={"return_to": "/reviews"})
        self.assertEqual(response.status_code, 200)

        # Approve button must appear before the Account label in the HTML
        approve_pos = response.text.find("Approve transaction")
        account_label_pos = response.text.find("<label>Account</label>")
        self.assertGreater(account_label_pos, approve_pos, "Approve button should appear before form fields")

    def test_similar_history_is_in_details_element(self) -> None:
        review_id = self._create_review_with_history()
        response = self.client.get(f"/reviews/{review_id}", params={"return_to": "/reviews"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("<details", response.text)
        self.assertIn("Similar history", response.text)

    def test_similar_history_details_is_not_open_by_default(self) -> None:
        review_id = self._create_review_with_history()
        response = self.client.get(f"/reviews/{review_id}", params={"return_to": "/reviews"})
        # The <details> for similar history must not have the 'open' attribute
        import re
        details_matches = re.findall(r"<details[^>]*>", response.text)
        history_details = [d for d in details_matches if "review-panel" in d]
        for detail_tag in history_details:
            self.assertNotIn(" open", detail_tag, f"Similar history details should not be open: {detail_tag}")


if __name__ == "__main__":
    unittest.main()
