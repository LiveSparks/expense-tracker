from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from expense_tracker.web import create_app


class ExpenseTrackerWebTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.data_file = Path(self.temp_dir.name) / "ledger.json"
        self.client = TestClient(create_app(self.data_file))

    def test_dashboard_shows_accounts_and_balances(self) -> None:
        self.client.post("/api/transactions", data={"account_name": "Cash", "payee_name": "Employer", "category_value": "Income / Salary", "amount": "1000.00", "spent_on": "2026-03-01"})
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Cash", response.text)
        self.assertIn("₹1,000.00", response.text)
        self.assertNotIn("Tap an account to view its transactions.", response.text)

    def test_transactions_page_can_be_filtered_by_account(self) -> None:
        self.client.post("/api/transactions", data={"account_name": "Cash", "payee_name": "Shop 1", "category_value": "General / Grocery", "amount": "-20.00", "spent_on": "2026-03-01"})
        self.client.post("/api/transactions", data={"account_name": "Credit", "payee_name": "Pharmacy", "category_value": "Medical / Meds", "amount": "-12.00", "spent_on": "2026-03-02"})
        response = self.client.get("/transactions", params={"account": "Credit"})
        self.assertEqual(response.status_code, 200)
        self.assertIn('class="transaction-primary">Pharmacy<', response.text)
        self.assertNotIn('class="transaction-primary">Shop 1<', response.text)
        self.assertIn('<a href="/transactions/new?account=Credit" data-add-link>Add</a>', response.text)
        self.assertIn("<details class=\"filter-drawer\"", response.text)
        self.assertEqual(response.text.count('type="date"'), 3)
        self.assertIn("More actions", response.text)
        self.assertIn('class="search-bar"', response.text)

    def test_transactions_page_supports_search(self) -> None:
        self.client.post("/api/transactions", data={"account_name": "Cash", "payee_name": "Amazon", "category_value": "General / Delivery", "amount": "-20.00", "spent_on": "2026-03-01", "notes": "Ref 1122"})
        self.client.post("/api/transactions", data={"account_name": "Cash", "payee_name": "Pharmacy", "category_value": "Medical / Meds", "amount": "-12.00", "spent_on": "2026-03-02", "notes": "Prescription"})

        response = self.client.get("/transactions", params={"search": "1122"})

        self.assertEqual(response.status_code, 200)
        self.assertIn('value="1122"', response.text)
        self.assertIn('class="transaction-primary">Amazon<', response.text)
        self.assertNotIn('class="transaction-primary">Pharmacy<', response.text)
        self.assertIn("<details class=\"filter-drawer\"", response.text)

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


if __name__ == "__main__":
    unittest.main()
