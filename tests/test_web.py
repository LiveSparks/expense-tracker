from __future__ import annotations

import tempfile
import time
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from expense_tracker.config import AppConfig
from expense_tracker.review_workflow import OpenAIDraftClient
from expense_tracker.sms_history import SmsHistoryStore
from expense_tracker.sms_pipeline import ReviewDraftTextFormat, SmsMessage, extract_sms_markers
from expense_tracker.tracker import ExpenseTracker
from expense_tracker.web import create_app


class ExpenseTrackerWebTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.data_file = Path(self.temp_dir.name) / "ledger.json"
        self.client = TestClient(create_app(self.data_file, process_reviews_inline=True))

    def test_dashboard_shows_accounts_and_balances(self) -> None:
        self.client.post("/api/transactions", data={"account_name": "Cash", "payee_name": "Employer", "category_value": "Income / Salary", "amount": "1000.00", "spent_on": "2026-03-01"})
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Cash", response.text)
        self.assertIn("₹1,000.00", response.text)
        self.assertNotIn("Tap an account to view its transactions.", response.text)

    def test_web_ui_redirects_to_login_when_auth_enabled(self) -> None:
        protected_client = TestClient(
            create_app(
                self.data_file,
                process_reviews_inline=True,
                config=AppConfig(auth_token="secret-token"),
            )
        )

        response = protected_client.get("/", follow_redirects=False)

        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/auth/login?next=%2F")

    def test_login_sets_cookie_and_allows_web_ui(self) -> None:
        protected_client = TestClient(
            create_app(
                self.data_file,
                process_reviews_inline=True,
                config=AppConfig(auth_token="secret-token"),
            )
        )

        login = protected_client.post(
            "/auth/login",
            data={"token": "secret-token", "next": "/"},
            follow_redirects=False,
        )
        dashboard = protected_client.get("/")

        self.assertEqual(login.status_code, 303)
        self.assertEqual(login.headers["location"], "/")
        self.assertIn("Max-Age=7776000", login.headers.get("set-cookie", ""))
        self.assertEqual(dashboard.status_code, 200)
        self.assertIn("Logout", dashboard.text)

    def test_topbar_keeps_add_with_sidebar_menu(self) -> None:
        response = self.client.get("/")

        self.assertEqual(response.status_code, 200)
        self.assertIn('data-add-link', response.text)
        self.assertIn('data-sidebar-open', response.text)
        self.assertIn('data-sidebar-close-on-nav', response.text)

    def test_api_requires_bearer_token_when_auth_enabled(self) -> None:
        protected_client = TestClient(
            create_app(
                self.data_file,
                process_reviews_inline=True,
                config=AppConfig(auth_token="secret-token"),
            )
        )

        unauthorized = protected_client.get("/api/transactions")
        authorized = protected_client.get(
            "/api/transactions",
            headers={"Authorization": "Bearer secret-token"},
        )

        self.assertEqual(unauthorized.status_code, 401)
        self.assertEqual(unauthorized.json()["detail"], "Unauthorized")
        self.assertEqual(authorized.status_code, 200)

    def test_healthz_stays_open_when_auth_enabled(self) -> None:
        protected_client = TestClient(
            create_app(
                self.data_file,
                process_reviews_inline=True,
                config=AppConfig(auth_token="secret-token"),
            )
        )

        response = protected_client.get("/healthz")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})

    def test_transactions_page_can_be_filtered_by_account(self) -> None:
        self.client.post("/api/transactions", data={"account_name": "Cash", "payee_name": "Shop 1", "category_value": "General / Grocery", "amount": "-20.00", "spent_on": "2026-03-01"})
        self.client.post("/api/transactions", data={"account_name": "Credit", "payee_name": "Pharmacy", "category_value": "Medical / Meds", "amount": "-12.00", "spent_on": "2026-03-02"})
        response = self.client.get("/transactions", params={"account": "Credit"})
        self.assertEqual(response.status_code, 200)
        self.assertIn('class="transaction-primary">Pharmacy<', response.text)
        self.assertNotIn('class="transaction-primary">Shop 1<', response.text)
        self.assertIn('<a href="/transactions/new?account=Credit" class="button primary compact-button" data-add-link>Add</a>', response.text)
        self.assertIn("<details class=\"filter-drawer\"", response.text)
        self.assertEqual(response.text.count('type="date"'), 3)
        self.assertIn("More actions", response.text)
        self.assertIn('class="search-bar"', response.text)
        self.assertIn("02 March, 2026", response.text)

    def test_transactions_page_supports_search(self) -> None:
        self.client.post("/api/transactions", data={"account_name": "Cash", "payee_name": "Amazon", "category_value": "General / Delivery", "amount": "-20.00", "spent_on": "2026-03-01", "notes": "Ref 1122"})
        self.client.post("/api/transactions", data={"account_name": "Cash", "payee_name": "Pharmacy", "category_value": "Medical / Meds", "amount": "-12.00", "spent_on": "2026-03-02", "notes": "Prescription"})

        response = self.client.get("/transactions", params={"search": "1122"})

        self.assertEqual(response.status_code, 200)
        self.assertIn('value="1122"', response.text)
        self.assertIn('class="transaction-primary">Amazon<', response.text)
        self.assertNotIn('class="transaction-primary">Pharmacy<', response.text)
        self.assertIn("<details class=\"filter-drawer\"", response.text)

    def test_transactions_page_preserves_return_to_and_scroll_state(self) -> None:
        tracker = ExpenseTracker(self.data_file)
        tracker.add_transaction(
            account_name="Cash",
            payee_name="Amazon",
            category_name="General",
            subcategory_name="Delivery",
            amount="-20.00",
            spent_on="2026-03-01",
        )
        created = tracker.list_transactions()[0]

        response = self.client.get("/transactions", params={"account": "Cash", "search": "Amazon"})

        self.assertEqual(response.status_code, 200)
        self.assertIn('data-scroll-restore-key="/transactions?account=Cash&amp;search=Amazon"', response.text)
        self.assertIn(
            f'data-edit-url="/transactions/{created.id}/edit?return_to=/transactions%3Faccount%3DCash%26search%3DAmazon"',
            response.text,
        )
        self.assertIn("data-preserve-scroll", response.text)

    def test_transaction_edit_redirect_preserves_return_to(self) -> None:
        tracker = ExpenseTracker(self.data_file)
        tracker.add_transaction(
            account_name="Cash",
            payee_name="Amazon",
            category_name="General",
            subcategory_name="Delivery",
            amount="-20.00",
            spent_on="2026-03-01",
        )
        created = tracker.list_transactions()[0]

        response = self.client.post(
            f"/transactions/{created.id}/edit",
            data={
                "account_name": "Cash",
                "payee_name": "Amazon",
                "category_value": "General / Delivery",
                "amount": "-22.00",
                "spent_on": "2026-03-01",
                "notes": "",
                "return_to": "/transactions?account=Cash&search=Amazon",
            },
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/transactions?account=Cash&search=Amazon")

    def test_transaction_delete_redirect_preserves_return_to(self) -> None:
        tracker = ExpenseTracker(self.data_file)
        tracker.add_transaction(
            account_name="Cash",
            payee_name="Amazon",
            category_name="General",
            subcategory_name="Delivery",
            amount="-20.00",
            spent_on="2026-03-01",
        )
        created = tracker.list_transactions()[0]

        response = self.client.post(
            f"/transactions/{created.id}/delete",
            data={"return_to": "/transactions?account=Cash&search=Amazon"},
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/transactions?account=Cash&search=Amazon")

    def test_transaction_form_supports_file_upload(self) -> None:
        receipt = Path(self.temp_dir.name) / "receipt.txt"
        receipt.write_text("receipt", encoding="utf-8")
        with receipt.open("rb") as handle:
            response = self.client.post(
                "/transactions",
                data={"account_name": "Cash", "payee_name": "Amazon", "category_value": "General / Delivery", "amount": "-8.99", "spent_on": "2026-03-10", "notes": "Order"},
                files={"files": ("receipt.txt", handle, "text/plain")},
                follow_redirects=True,
            )
        self.assertEqual(response.status_code, 200)
        self.assertIn("Amazon", response.text)
        self.assertTrue((self.data_file.parent / "attachments").exists())

    def test_new_transaction_form_autofocuses_amount_and_defaults_negative_sign(self) -> None:
        response = self.client.get("/transactions/new")

        self.assertEqual(response.status_code, 200)
        self.assertIn('name="amount" value="-"', response.text)
        self.assertIn('name="amount" value="-" inputmode="decimal" required autofocus', response.text)

    def test_transaction_form_marks_non_notes_fields_selectable(self) -> None:
        tracker = ExpenseTracker(self.data_file)
        tracker.add_transaction(
            account_name="Cash",
            payee_name="Amazon",
            category_name="General",
            subcategory_name="Delivery",
            amount="-20.00",
            spent_on="2026-03-01",
            notes="Keep cursor placement here.",
        )
        created = tracker.list_transactions()[0]

        response = self.client.get(f"/transactions/{created.id}/edit")

        self.assertEqual(response.status_code, 200)
        self.assertIn('name="account_name" value="Cash" autocomplete="off" required data-select-on-focus', response.text)
        self.assertIn('name="payee_name" value="Amazon" autocomplete="off" required data-select-on-focus', response.text)
        self.assertIn('name="category_value" value="General / Delivery" autocomplete="off" placeholder="Choose a subcategory" required data-select-on-focus', response.text)
        self.assertIn('name="amount" value="-20.00" inputmode="decimal" required', response.text)
        self.assertNotIn('name="amount" value="-20.00" inputmode="decimal" required data-select-on-focus', response.text)
        self.assertIn('<textarea name="notes" rows="3">Keep cursor placement here.</textarea>', response.text)

    def test_edit_view_shows_linked_sms_for_transfer_pair(self) -> None:
        self.client.post("/manage/accounts", data={"action": "create", "name": "Cash"})
        tracker = ExpenseTracker(self.data_file)
        created = tracker.add_transaction(
            account_name="HDFC Savings 2054",
            payee_name="Cash",
            category_name="General",
            subcategory_name="Transfer",
            amount="-500.00",
            spent_on="2026-03-10",
            notes="- Ref: 12345",
        )
        source, destination = created
        store = SmsHistoryStore(self.data_file)
        source_sms = SmsMessage(
            row_number=1,
            received_at=datetime(2026, 3, 10, 10, 0, 0),
            direction="Received",
            contact="JM-HDFCBK-S",
            phone="JM-HDFCBK-S",
            content="Sent Rs.500.00 From HDFC Bank A/C *2054 To Cash On 10/03/26 Ref 12345",
            message_type="SMS",
        )
        destination_sms = SmsMessage(
            row_number=2,
            received_at=datetime(2026, 3, 10, 10, 1, 0),
            direction="Received",
            contact="AX-CASHBK",
            phone="AX-CASHBK",
            content="Rs.500.00 credited to account Cash from HDFC Savings 2054 on 10/03/26 Ref 12345",
            message_type="SMS",
        )
        store.record_approved_review(
            sender=source_sms.contact,
            phone=source_sms.phone,
            content=source_sms.content,
            received_at=source_sms.received_at,
            markers_payload=extract_sms_markers(source_sms).to_dict(),
            matched_transaction_id=source.id,
        )
        store.record_approved_review(
            sender=destination_sms.contact,
            phone=destination_sms.phone,
            content=destination_sms.content,
            received_at=destination_sms.received_at,
            markers_payload=extract_sms_markers(destination_sms).to_dict(),
            matched_transaction_id=destination.id,
        )

        response = self.client.get(f"/transactions/{source.id}/edit")

        self.assertEqual(response.status_code, 200)
        self.assertIn("Linked SMS", response.text)
        self.assertIn("JM-HDFCBK-S", response.text)
        self.assertIn("AX-CASHBK", response.text)
        self.assertIn("Paired transfer side", response.text)

    def test_management_pages_can_create_metadata(self) -> None:
        self.client.post("/manage/accounts", data={"action": "create", "name": "HDFC"})
        self.client.post("/manage/payees", data={"action": "create", "name": "Amazon"})
        self.client.post("/manage/categories", data={"action": "create-subcategory", "category_name": "Travel", "subcategory_name": "Flights"})
        response = self.client.get("/manage/categories")
        self.assertIn("Travel", response.text)
        self.assertIn("Flights", response.text)
        self.assertIn("manage-inline-form", self.client.get("/manage/accounts").text)
        self.assertIn("manage-inline-form", self.client.get("/manage/payees").text)
        self.assertNotIn(">Save<", response.text)
        self.assertNotIn(">Cancel<", response.text)

    def test_manage_pages_include_delete_dialogs(self) -> None:
        self.client.post("/api/transactions", data={"account_name": "Cash", "payee_name": "Amazon", "category_value": "General / Delivery", "amount": "-8.00", "spent_on": "2026-03-10"})

        accounts_page = self.client.get("/manage/accounts")
        payees_page = self.client.get("/manage/payees")
        categories_page = self.client.get("/manage/categories")

        self.assertIn("data-manage-delete-dialog", accounts_page.text)
        self.assertIn('data-delete-count="1"', accounts_page.text)
        self.assertIn("data-manage-delete-dialog", payees_page.text)
        self.assertIn("data-manage-delete-dialog", categories_page.text)

    def test_transaction_form_uses_custom_picker_markup(self) -> None:
        self.client.post("/manage/accounts", data={"action": "create", "name": "HDFC"})
        response = self.client.get("/transactions/new")
        self.assertEqual(response.status_code, 200)
        self.assertIn("js-picker", response.text)
        self.assertIn('"group": "Transfer to"', response.text)
        self.assertIn('value="%s"' % date.today().isoformat(), response.text)
        self.assertIn('"selectable": false', response.text)
        self.assertIn("data-category-dialog", response.text)
        self.assertNotIn("<datalist", response.text)

    def test_dashboard_shows_all_accounts_card(self) -> None:
        self.client.post(
            "/api/transactions",
            data={
                "account_name": "Cash",
                "payee_name": "Employer",
                "category_value": "Income / Salary",
                "amount": "500.00",
                "spent_on": "2026-03-10",
            },
        )
        response = self.client.get("/")

        self.assertEqual(response.status_code, 200)
        self.assertIn("All Accounts", response.text)
        self.assertIn('href="/transactions"', response.text)

    def test_manage_data_page_and_export_endpoint(self) -> None:
        self.client.post(
            "/api/transactions",
            data={
                "account_name": "Cash",
                "payee_name": "Employer",
                "category_value": "Income / Salary",
                "amount": "500.00",
                "spent_on": "2026-03-10",
            },
        )

        manage_page = self.client.get("/manage/data")
        export_response = self.client.get("/manage/data/export")

        self.assertEqual(manage_page.status_code, 200)
        self.assertIn("Import database", manage_page.text)
        self.assertEqual(export_response.status_code, 200)
        self.assertEqual(export_response.headers.get("content-type"), "application/octet-stream")
        self.assertIn("attachment", export_response.headers.get("content-disposition", ""))

    def test_manage_data_import_rejects_non_sqlite_file(self) -> None:
        response = self.client.post(
            "/manage/data/import",
            files={"database_file": ("bad.txt", b"not a sqlite file", "text/plain")},
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 303)
        self.assertIn("/manage/data?error=Uploaded+file+is+not+a+valid+SQLite+database.", response.headers["location"])

    def test_manage_data_import_replaces_database(self) -> None:
        self.client.post(
            "/api/transactions",
            data={
                "account_name": "Old",
                "payee_name": "Legacy",
                "category_value": "General / Grocery",
                "amount": "-10.00",
                "spent_on": "2026-03-01",
            },
        )

        source_path = Path(self.temp_dir.name) / "incoming.db"
        source_tracker = ExpenseTracker(source_path)
        source_tracker.add_transaction(
            account_name="New",
            payee_name="Imported",
            category_name="General",
            subcategory_name="Delivery",
            amount="-20.00",
            spent_on="2026-03-15",
        )
        with source_path.open("rb") as file_handle:
            response = self.client.post(
                "/manage/data/import",
                files={"database_file": ("incoming.db", file_handle, "application/octet-stream")},
                follow_redirects=False,
            )

        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/manage/data?message=Database+imported+successfully.")
        transactions = self.client.get("/api/transactions").json()["transactions"]
        self.assertEqual(len(transactions), 1)
        self.assertEqual(transactions[0]["payee_name"], "Imported")

    def test_category_group_value_is_rejected(self) -> None:
        response = self.client.post(
            "/transactions",
            data={"account_name": "Cash", "payee_name": "Shop 1", "category_value": "General", "amount": "-20.00", "spent_on": "2026-03-01"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("Choose a subcategory", response.text)

    def test_bulk_duplicate_action_creates_another_transaction(self) -> None:
        create_response = self.client.post(
            "/api/transactions",
            data={"account_name": "Cash", "payee_name": "Shop 1", "category_value": "General / Grocery", "amount": "-20.00", "spent_on": "2026-03-01"},
        )
        transaction_id = create_response.json()["transactions"][0]["id"]
        duplicate_response = self.client.post(
            "/transactions/bulk",
            data={"action": "duplicate", "transaction_ids": [transaction_id], "return_to": "/transactions"},
            follow_redirects=True,
        )
        self.assertEqual(duplicate_response.status_code, 200)
        api_response = self.client.get("/api/transactions")
        self.assertEqual(len(api_response.json()["transactions"]), 2)

    def test_bulk_change_payee_updates_transaction(self) -> None:
        create_response = self.client.post(
            "/api/transactions",
            data={"account_name": "Cash", "payee_name": "Shop 1", "category_value": "General / Grocery", "amount": "-20.00", "spent_on": "2026-03-01"},
        )
        transaction_id = create_response.json()["transactions"][0]["id"]
        response = self.client.post(
            "/transactions/bulk",
            data={"action": "change_payee", "transaction_ids": [transaction_id], "target_payee": "Amazon", "return_to": "/transactions"},
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        api_response = self.client.get("/api/transactions")
        self.assertEqual(api_response.json()["transactions"][0]["payee_name"], "Amazon")

    def test_api_returns_transaction_payloads(self) -> None:
        create_response = self.client.post("/api/transactions", data={"account_name": "Cash", "payee_name": "Employer", "category_value": "Income / Salary", "amount": "100.00", "spent_on": "2026-03-01"})
        transaction_id = create_response.json()["transactions"][0]["id"]
        list_response = self.client.get("/api/transactions")
        delete_response = self.client.delete(f"/api/transactions/{transaction_id}")
        self.assertEqual(list_response.status_code, 200)
        self.assertEqual(delete_response.status_code, 200)
        self.assertEqual(list_response.json()["transactions"][0]["account_name"], "Cash")
        self.assertTrue(delete_response.json()["deleted"])

    def test_sms_intake_creates_review_draft_and_dashboard_count(self) -> None:
        self.client.post("/api/transactions", data={"account_name": "HDFC Savings 2054", "payee_name": "Blinkit", "category_value": "General / Grocery", "amount": "-499.00", "spent_on": "2026-03-19"})

        with patch("expense_tracker.review_workflow.OpenAIDraftClient.is_configured", return_value=False):
            intake_response = self.client.post(
                "/api/sms/intake",
                json={
                    "sender": "JM-HDFCBK-S",
                    "phone": "JM-HDFCBK-S",
                    "content": "Sent Rs.499.00 From HDFC Bank A/C *2054 To Blinkit On 19/03/26 Ref 552880661565 Note household order",
                    "received_at": "2026-03-19T09:01:10Z",
                    "latitude": 28.61,
                    "longitude": 77.20,
                },
            )

        self.assertEqual(intake_response.status_code, 200)
        review = intake_response.json()["review"]
        self.assertEqual(review["draft_account_name"], "HDFC Savings 2054")
        self.assertEqual(review["draft_payee_name"], "Blinkit")
        self.assertEqual(review["draft_category_value"], "General / Grocery")
        self.assertIn("- Ref: 552880661565", review["draft_notes"])
        dashboard = self.client.get("/")
        reviews_page = self.client.get("/reviews")
        self.assertIn("For review", dashboard.text)
        self.assertIn("1", dashboard.text)
        self.assertIn("Blinkit", reviews_page.text)

    def test_sms_intake_filters_otp_and_personal_messages_before_review(self) -> None:
        otp_response = self.client.post(
            "/api/sms/intake",
            json={
                "sender": "CP-RAJTVD-S",
                "content": "OTP to verify your mobile is 796589. This OTP will be expired in 5 Minutes. Do not share this to anyone.",
                "received_at": "2026-03-19T10:15:00Z",
            },
        )
        personal_response = self.client.post(
            "/api/sms/intake",
            json={
                "sender": "Vijay 👸🏽",
                "content": "Ek aur bhejo",
                "received_at": "2026-03-19T10:16:00Z",
            },
        )

        self.assertEqual(otp_response.status_code, 200)
        self.assertEqual(personal_response.status_code, 200)
        self.assertEqual(otp_response.json()["review"]["review_status"], "filtered")
        self.assertEqual(personal_response.json()["review"]["review_status"], "filtered")
        queue = self.client.get("/api/reviews").json()["queue"]
        self.assertEqual(queue["filtered"], 2)
        self.assertEqual(queue["ready"], 0)

    def test_review_approval_creates_transaction(self) -> None:
        self.client.post("/api/transactions", data={"account_name": "Cash", "payee_name": "Amazon", "category_value": "General / Delivery", "amount": "-250.00", "spent_on": "2026-03-19"})
        with patch("expense_tracker.review_workflow.OpenAIDraftClient.is_configured", return_value=False):
            intake_response = self.client.post(
                "/api/sms/intake",
                json={
                    "sender": "JM-HDFCBK-S",
                    "content": "Sent Rs.250.00 From HDFC Bank A/C *2054 To Amazon On 19/03/26 Ref 7788990011",
                    "received_at": "2026-03-19T10:15:00Z",
                },
            )
        review_id = intake_response.json()["review"]["id"]

        approve_response = self.client.post(
            f"/reviews/{review_id}/approve",
            data={
                "account_name": "Cash",
                "payee_name": "Amazon",
                "category_value": "General / Delivery",
                "amount": "-250.00",
                "spent_on": "2026-03-19",
                "notes": "- Ref: 7788990011",
            },
            follow_redirects=True,
        )

        self.assertEqual(approve_response.status_code, 200)
        reviews_api = self.client.get("/api/reviews")
        transactions_api = self.client.get("/api/transactions", params={"payee": "Amazon"})
        self.assertEqual(reviews_api.json()["reviews"], [])
        self.assertEqual(len(transactions_api.json()["transactions"]), 2)

    def test_review_approval_redirect_preserves_return_to(self) -> None:
        with patch("expense_tracker.review_workflow.OpenAIDraftClient.is_configured", return_value=False):
            intake_response = self.client.post(
                "/api/sms/intake",
                json={
                    "sender": "JM-HDFCBK-S",
                    "content": "Sent Rs.250.00 From HDFC Bank A/C *2054 To Amazon On 19/03/26 Ref 7788990011",
                    "received_at": "2026-03-19T10:15:00Z",
                },
            )
        review_id = intake_response.json()["review"]["id"]

        response = self.client.post(
            f"/reviews/{review_id}/approve",
            data={
                "account_name": "Cash",
                "payee_name": "Amazon",
                "category_value": "General / Delivery",
                "amount": "-250.00",
                "spent_on": "2026-03-19",
                "notes": "- Ref: 7788990011",
                "return_to": "/reviews",
            },
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/reviews")

    def test_review_detail_can_delete_draft(self) -> None:
        with patch("expense_tracker.review_workflow.OpenAIDraftClient.is_configured", return_value=False):
            intake_response = self.client.post(
                "/api/sms/intake",
                json={
                    "sender": "JM-HDFCBK-S",
                    "content": "Sent Rs.88.00 From HDFC Bank A/C *2054 To Amazon On 19/03/26 Ref 12345",
                    "received_at": "2026-03-19T10:15:00Z",
                },
            )

        review_id = intake_response.json()["review"]["id"]
        detail_response = self.client.get(f"/reviews/{review_id}")
        delete_response = self.client.post(f"/reviews/{review_id}/delete", follow_redirects=True)

        self.assertEqual(detail_response.status_code, 200)
        self.assertIn("19/03/2026 10:15", detail_response.text)
        self.assertIn("Delete draft", detail_response.text)
        self.assertIn("Extracted markers", detail_response.text)
        self.assertEqual(delete_response.status_code, 200)
        self.assertEqual(self.client.get("/api/reviews").json()["reviews"], [])

    def test_reviews_page_preserves_return_to_and_scroll_state(self) -> None:
        with patch("expense_tracker.review_workflow.OpenAIDraftClient.is_configured", return_value=False):
            intake_response = self.client.post(
                "/api/sms/intake",
                json={
                    "sender": "JM-HDFCBK-S",
                    "content": "Sent Rs.88.00 From HDFC Bank A/C *2054 To Amazon On 19/03/26 Ref 12345",
                    "received_at": "2026-03-19T10:15:00Z",
                },
            )

        review_id = intake_response.json()["review"]["id"]
        response = self.client.get("/reviews")

        self.assertEqual(response.status_code, 200)
        self.assertIn('data-scroll-restore-key="/reviews"', response.text)
        self.assertIn(f'href="/reviews/{review_id}?return_to=/reviews"', response.text)
        self.assertIn('name="return_to" value="/reviews"', response.text)
        self.assertIn("data-preserve-scroll", response.text)

    def test_review_detail_links_similar_history_and_llm_request(self) -> None:
        tracker = ExpenseTracker(self.data_file)
        tracker.add_transaction(
            account_name="Cash",
            payee_name="Online shopping",
            category_name="General",
            subcategory_name="Delivery",
            amount="-88.00",
            spent_on="2026-03-10",
            notes="- Ref: 12345",
        )
        transaction = tracker.list_transactions()[0]
        store = SmsHistoryStore(self.data_file)
        historical_sms = SmsMessage(
            row_number=1,
            received_at=datetime(2026, 3, 10, 10, 0, 0),
            direction="Received",
            contact="JM-HDFCBK-S",
            phone="JM-HDFCBK-S",
            content="Sent Rs.88.00 From HDFC Bank A/C *2054 To Amazon On 10/03/26 Ref 12345",
            message_type="SMS",
        )
        store.record_approved_review(
            sender=historical_sms.contact,
            phone=historical_sms.phone,
            content=historical_sms.content,
            received_at=historical_sms.received_at,
            markers_payload=extract_sms_markers(historical_sms).to_dict(),
            matched_transaction_id=transaction.id,
        )

        with patch("expense_tracker.review_workflow.OpenAIDraftClient.is_configured", return_value=False):
            intake_response = self.client.post(
                "/api/sms/intake",
                json={
                    "sender": "JM-HDFCBK-S",
                    "content": "Sent Rs.88.00 From HDFC Bank A/C *2054 To Amazon On 19/03/26 Ref 67890",
                    "received_at": "2026-03-19T10:15:00Z",
                },
            )

        review_id = intake_response.json()["review"]["id"]
        detail_response = self.client.get(f"/reviews/{review_id}", params={"return_to": "/reviews"})
        prompt_response = self.client.get(f"/reviews/{review_id}/llm-request", params={"return_to": f"/reviews/{review_id}"})
        prompt_api = self.client.get(f"/api/reviews/{review_id}/llm-request").json()

        self.assertEqual(detail_response.status_code, 200)
        self.assertIn(
            f'/transactions/{transaction.id}/edit?return_to=/reviews/{review_id}%3Freturn_to%3D%252Freviews',
            detail_response.text,
        )
        self.assertIn(f'/reviews/{review_id}/llm-request?return_to=/reviews/{review_id}%3Freturn_to%3D%252Freviews', detail_response.text)
        self.assertEqual(prompt_response.status_code, 200)
        self.assertIn("System prompt", prompt_response.text)
        self.assertIn("User payload", prompt_response.text)
        self.assertIn("Card purchase", prompt_response.text)
        self.assertEqual(prompt_api["text_format_model"], "ReviewDraftTextFormat")
        self.assertEqual(prompt_api["user_payload"]["new_sms"]["content"], "Sent Rs.88.00 From HDFC Bank A/C *2054 To Amazon On 19/03/26 Ref 67890")
        self.assertIn("Latest transactions", detail_response.text)
        self.assertIn("Same extracted payee", detail_response.text)
        self.assertIn("Same amount", detail_response.text)
        self.assertIn("Same SMS sender", detail_response.text)
        self.assertIn("latest_transactions", prompt_api["user_payload"]["similar_examples"])
        self.assertIn("same_payee_transactions", prompt_api["user_payload"]["similar_examples"])
        self.assertIn("same_amount_transactions", prompt_api["user_payload"]["similar_examples"])
        self.assertIn("same_sender_transactions", prompt_api["user_payload"]["similar_examples"])
        self.assertEqual(len(prompt_api["user_payload"]["similar_examples"]["same_payee_transactions"]), 1)
        self.assertEqual(
            prompt_api["user_payload"]["similar_examples"]["same_payee_transactions"][0]["historical_markers"]["merchant_hint"],
            "Amazon",
        )
        self.assertEqual(len(prompt_api["user_payload"]["similar_examples"]["same_amount_transactions"]), 1)
        self.assertEqual(len(prompt_api["user_payload"]["similar_examples"]["same_sender_transactions"]), 1)

    def test_sms_intake_uses_openai_draft_when_configured(self) -> None:
        with (
            patch("expense_tracker.review_workflow.OpenAIDraftClient.is_configured", return_value=True),
            patch(
                "expense_tracker.review_workflow.OpenAIDraftClient.generate_draft",
                return_value={
                    "account_name": "Cash",
                    "payee_name": "Amazon",
                    "category_value": "General / Delivery",
                    "amount": "-88.00",
                    "spent_on": "2026-03-19",
                    "notes": "Ref: 12345\nNote: pantry restock",
                    "reasoning": ["Used similar Amazon history."],
                },
            ),
        ):
            intake_response = self.client.post(
                "/api/sms/intake",
                json={
                    "sender": "JM-HDFCBK-S",
                    "content": "Sent Rs.88.00 From HDFC Bank A/C *2054 To Amazon On 19/03/26 Ref 12345",
                    "received_at": "2026-03-19T10:15:00Z",
                },
            )

        self.assertEqual(intake_response.status_code, 200)
        review = intake_response.json()["review"]
        self.assertEqual(review["source"], "sms_intake:openai")
        self.assertEqual(review["draft_account_name"], "Cash")
        self.assertEqual(review["draft_payee_name"], "Amazon")
        self.assertEqual(review["draft_category_value"], "General / Delivery")
        self.assertEqual(review["draft_notes"], "- Ref: 12345\n- Note: pantry restock")
        self.assertIn("OpenAI structured output", review["reasoning"][-1])

    def test_openai_client_uses_typed_parse_flow(self) -> None:
        key_path = Path(self.temp_dir.name) / "openai.key"
        key_path.write_text("test-key", encoding="utf-8")
        tracker = ExpenseTracker(self.data_file)
        metadata = tracker.metadata_snapshot()
        message = SmsMessage(
            row_number=1,
            received_at=datetime(2026, 3, 19, 10, 15, 0),
            direction="Received",
            contact="JM-HDFCBK-S",
            phone="JM-HDFCBK-S",
            content="Sent Rs.88.00 From HDFC Bank A/C *2054 To Amazon On 19/03/26 Ref 12345",
            message_type="SMS",
        )
        markers = extract_sms_markers(message).to_dict()
        client = OpenAIDraftClient(key_path=key_path)

        with patch("expense_tracker.review_workflow.OpenAI") as openai_class:
            openai_class.return_value.responses.parse.return_value.output_parsed = ReviewDraftTextFormat(
                message_kind="transaction",
                account_name="Cash",
                payee_name="Amazon",
                category_value="General / Delivery",
                amount="-88.00",
                spent_on="2026-03-19",
                notes="- Ref: 12345",
                non_transactional_reason="",
                review_status="for_review",
                confidence=0.92,
                reasoning=["Used similar history."],
            )
            draft = client.generate_draft(metadata=metadata, message=message, markers=markers, history=[])

        parse_kwargs = openai_class.return_value.responses.parse.call_args.kwargs
        self.assertEqual(parse_kwargs["model"], "gpt-5-mini")
        self.assertIs(parse_kwargs["text_format"], ReviewDraftTextFormat)
        self.assertEqual(parse_kwargs["input"][0]["role"], "system")
        self.assertEqual(parse_kwargs["input"][1]["role"], "user")
        self.assertEqual(draft["payee_name"], "Amazon")

    def test_reviews_page_handles_invalid_openai_amounts(self) -> None:
        with (
            patch("expense_tracker.review_workflow.OpenAIDraftClient.is_configured", return_value=True),
            patch(
                "expense_tracker.review_workflow.OpenAIDraftClient.generate_draft",
                return_value={
                    "account_name": "Cash",
                    "payee_name": "Airtel",
                    "category_value": "General / Delivery",
                    "amount": "UNKNOWN",
                    "spent_on": "2026-03-19",
                    "notes": "",
                    "reasoning": ["Non-financial message."],
                },
            ),
        ):
            intake_response = self.client.post(
                "/api/sms/intake",
                json={
                    "sender": "JM-HDFCBK-S",
                    "content": "Sent Rs.249.00 From HDFC Bank A/C *2054 To Airtel On 19/03/26 Ref 77889",
                    "received_at": "2026-03-19T10:15:00Z",
                },
            )

        self.assertEqual(intake_response.status_code, 200)
        response = self.client.get("/reviews")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Airtel", response.text)
        self.assertNotIn("UNKNOWN", response.text)

    def test_reviews_page_shows_queue_counts_and_quick_actions(self) -> None:
        with (
            patch("expense_tracker.review_workflow.OpenAIDraftClient.is_configured", return_value=True),
            patch(
                "expense_tracker.review_workflow.OpenAIDraftClient.generate_draft",
                return_value={
                    "account_name": "Cash",
                    "payee_name": "Amazon",
                    "category_value": "General / Delivery",
                    "amount": "-88.00",
                    "spent_on": "2026-03-19",
                    "notes": "",
                    "reasoning": ["Looks like a card purchase."],
                },
            ),
        ):
            self.client.post(
                "/api/sms/intake",
                json={
                    "sender": "JM-HDFCBK-S",
                    "content": "Sent Rs.88.00 From HDFC Bank A/C *2054 To Amazon On 19/03/26 Ref 12345",
                    "received_at": "2026-03-19T10:15:00Z",
                },
            )

        response = self.client.get("/reviews")

        self.assertEqual(response.status_code, 200)
        self.assertIn("Queued:", response.text)
        self.assertIn("Processing:", response.text)
        self.assertIn("Ready:", response.text)
        self.assertIn("/quick-approve", response.text)
        self.assertIn("Delete review draft", response.text)

    def test_async_sms_intake_returns_queued_review_immediately(self) -> None:
        async_client = TestClient(create_app(self.data_file, process_reviews_inline=False))
        def slow_queue_processor(self, *, max_items=None):
            time.sleep(0.5)
            return 0

        with patch("expense_tracker.review_workflow.ReviewWorkflowStore.process_pending_queue", slow_queue_processor):
            started_at = time.monotonic()
            response = async_client.post(
                "/api/sms/intake",
                json={
                    "sender": "JM-HDFCBK-S",
                    "content": "Sent Rs.88.00 From HDFC Bank A/C *2054 To Amazon On 19/03/26 Ref 12345",
                    "received_at": "2026-03-19T10:15:00Z",
                },
            )
            elapsed = time.monotonic() - started_at

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["review"]["review_status"], "queued")
        self.assertEqual(response.json()["queue"]["queued"], 1)
        self.assertLess(elapsed, 0.5)

    def test_sms_intake_groups_transfer_history_before_openai(self) -> None:
        self.client.post("/manage/accounts", data={"action": "create", "name": "Cash"})
        tracker = ExpenseTracker(self.data_file)
        tracker.add_transaction(
            account_name="HDFC Savings 2054",
            payee_name="Cash",
            category_name="General",
            subcategory_name="Transfer",
            amount="-500.00",
            spent_on="2026-03-10",
            notes="- Ref: 12345",
        )
        transactions = tracker.list_transactions()
        source = next(item for item in transactions if item.entry_type == "transfer_out")
        destination = next(item for item in transactions if item.entry_type == "transfer_in")
        store = SmsHistoryStore(self.data_file)
        source_sms = SmsMessage(
            row_number=1,
            received_at=datetime(2026, 3, 10, 10, 0, 0),
            direction="Received",
            contact="JM-HDFCBK-S",
            phone="JM-HDFCBK-S",
            content="Sent Rs.500.00 From HDFC Bank A/C *2054 To Cash On 10/03/26 Ref 12345",
            message_type="SMS",
        )
        destination_sms = SmsMessage(
            row_number=2,
            received_at=datetime(2026, 3, 10, 10, 1, 0),
            direction="Received",
            contact="AX-CASHBK",
            phone="AX-CASHBK",
            content="Rs.500.00 credited to account Cash from HDFC Savings 2054 on 10/03/26 Ref 12345",
            message_type="SMS",
        )
        store.record_approved_review(
            sender=source_sms.contact,
            phone=source_sms.phone,
            content=source_sms.content,
            received_at=source_sms.received_at,
            markers_payload=extract_sms_markers(source_sms).to_dict(),
            matched_transaction_id=source.id,
        )
        store.record_approved_review(
            sender=destination_sms.contact,
            phone=destination_sms.phone,
            content=destination_sms.content,
            received_at=destination_sms.received_at,
            markers_payload=extract_sms_markers(destination_sms).to_dict(),
            matched_transaction_id=destination.id,
        )

        with (
            patch("expense_tracker.review_workflow.OpenAIDraftClient.is_configured", return_value=True),
            patch(
                "expense_tracker.review_workflow.OpenAIDraftClient.generate_draft",
                return_value={
                    "account_name": "HDFC Savings 2054",
                    "payee_name": "Cash",
                    "category_value": "Transfers / Transfer",
                    "amount": "-500.00",
                    "spent_on": "2026-03-12",
                    "notes": "- Ref: 67890",
                    "reasoning": ["Detected an internal transfer."],
                },
            ) as generate_draft,
        ):
            response = self.client.post(
                "/api/sms/intake",
                json={
                    "sender": "AX-CASHBK",
                    "content": "Rs.500.00 credited to account Cash from HDFC Savings 2054 on 12/03/26 Ref 67890",
                    "received_at": "2026-03-12T10:15:00Z",
                },
            )

        self.assertEqual(response.status_code, 200)
        history = generate_draft.call_args.kwargs["history"]
        self.assertIn("same_sender_transactions", history)
        self.assertEqual(len(history["same_sender_transactions"]), 1)
        self.assertEqual(history["same_sender_transactions"][0]["entry_type"], "transfer_out")
        self.assertEqual(history["same_sender_transactions"][0]["account_name"], "HDFC Savings 2054")
        self.assertEqual(history["same_sender_transactions"][0]["payee_name"], "Cash")
        self.assertEqual(history["same_sender_transactions"][0]["historical_sender"], "AX-CASHBK")
        self.assertEqual(len(history["same_amount_transactions"]), 1)


if __name__ == "__main__":
    unittest.main()
