from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, DataTable, Footer, Header, Input, Static

from .models import MetadataSnapshot, TransactionRecord
from .tracker import ExpenseTracker


def render_balances_text(balances: dict[str, Decimal]) -> str:
    if not balances:
        return "Account balances:\n- No accounts yet."
    lines = ["Account balances:"]
    for account, amount in balances.items():
        lines.append(f"- {account}: ${amount:.2f}")
    return "\n".join(lines)


def render_category_totals_text(summary: dict[str, Decimal]) -> str:
    if not summary:
        return "Category totals:\n- No matching transactions."
    lines = ["Category totals:"]
    grand_total = Decimal("0.00")
    for category, amount in summary.items():
        grand_total += amount
        lines.append(f"- {category}: ${amount:.2f}")
    lines.append(f"Net total: ${grand_total:.2f}")
    return "\n".join(lines)


def render_catalog_text(metadata: MetadataSnapshot) -> str:
    account_lines = [f"- {name}" for name in metadata.accounts] or ["- None"]
    payee_lines = [f"- {name}" for name in metadata.payees] or ["- None"]
    category_lines: list[str] = []
    for category in metadata.categories:
        subcategories = metadata.subcategories_by_category.get(category, [])
        details = ", ".join(subcategories) if subcategories else "-"
        category_lines.append(f"- {category}: {details}")
    return "\n".join(
        [
            "Known accounts:",
            *account_lines,
            "",
            "Known payees:",
            *payee_lines,
            "",
            "Categories and subcategories:",
            *category_lines,
        ]
    )


class ExpenseTrackerApp(App[None]):
    TITLE = "Expense Tracker"
    SUB_TITLE = "Accounts, transfers, filters, and files"
    CSS = """
    Screen {
        layout: vertical;
    }

    #body {
        height: 1fr;
    }

    #left-panel {
        width: 44;
        min-width: 44;
        height: 1fr;
        border: round $surface;
        padding: 1 2;
    }

    #right-panel {
        width: 1fr;
        height: 1fr;
        border: round $surface;
        padding: 1 2;
    }

    .section-title {
        text-style: bold;
        margin-top: 1;
        margin-bottom: 1;
    }

    Input {
        margin-bottom: 1;
    }

    #actions, #filter-actions {
        height: auto;
        margin-top: 1;
    }

    Button {
        margin-right: 1;
        margin-bottom: 1;
    }

    #status {
        min-height: 3;
        margin-top: 1;
    }

    #balances, #summary, #catalog {
        margin-top: 1;
    }

    #filter-panel {
        height: auto;
    }

    DataTable {
        height: 1fr;
        margin-top: 1;
    }
    """
    BINDINGS = [
        ("q", "quit", "Quit"),
        ("r", "reload_data", "Refresh"),
        ("f", "apply_filters", "Apply filters"),
    ]

    def __init__(self, data_file: Path) -> None:
        super().__init__()
        self.tracker = ExpenseTracker(data_file)
        self._row_ids: list[str] = []

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="body"):
            with Vertical(id="left-panel"):
                yield Static("New transaction", classes="section-title")
                yield Static(
                    "Transfers are created automatically when Payee matches another account name. "
                    "You can also set Type to transfer to create the destination account on the fly.",
                )
                yield Input(placeholder="Payee", id="payee")
                yield Input(placeholder="Account", id="account")
                yield Input(placeholder="Category", id="category")
                yield Input(placeholder="Subcategory", id="subcategory")
                yield Input(placeholder="Amount", id="amount")
                yield Input(placeholder="Type: expense, income, or transfer", value="expense", id="transaction-type")
                yield Input(value=date.today().isoformat(), placeholder="YYYY-MM-DD", id="spent-on")
                yield Input(placeholder="Attachment file paths (comma separated)", id="attachments")
                yield Input(placeholder="Notes", id="notes")
                with Horizontal(id="actions"):
                    yield Button("Add transaction", variant="primary", id="add-transaction")
                    yield Button("Delete selected", variant="error", id="delete-transaction")
                    yield Button("Refresh", id="refresh-view")
                yield Static("Ready.", id="status")
                yield Static("", id="balances")
                yield Static("", id="summary")
                yield Static("", id="catalog")
            with Vertical(id="right-panel"):
                yield Static("Filters", classes="section-title")
                with Horizontal(id="filter-panel"):
                    with Vertical():
                        yield Input(placeholder="Filter account", id="filter-account")
                        yield Input(placeholder="Filter category", id="filter-category")
                    with Vertical():
                        yield Input(placeholder="Filter payee", id="filter-payee")
                        yield Input(placeholder="Exact date YYYY-MM-DD", id="filter-date")
                    with Vertical():
                        yield Input(placeholder="From date YYYY-MM-DD", id="filter-start-date")
                        yield Input(placeholder="To date YYYY-MM-DD", id="filter-end-date")
                with Horizontal(id="filter-actions"):
                    yield Button("Apply filters", id="apply-filters")
                    yield Button("Clear filters", id="clear-filters")
                yield DataTable(id="transaction-table")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#transaction-table", DataTable)
        table.cursor_type = "row"
        table.zebra_stripes = True
        table.add_columns(
            "Date",
            "Account",
            "Payee",
            "Category",
            "Subcategory",
            "Amount",
            "Type",
            "Notes",
            "Files",
            "Transaction ID",
        )
        self.refresh_view()
        self.query_one("#payee", Input).focus()

    def action_reload_data(self) -> None:
        self.refresh_view("Reloaded data.")

    def action_apply_filters(self) -> None:
        self.refresh_view("Applied filters.")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id
        if button_id == "add-transaction":
            self.add_transaction_from_form()
            return
        if button_id == "delete-transaction":
            self.delete_selected_transaction()
            return
        if button_id == "refresh-view":
            self.refresh_view("Reloaded data.")
            return
        if button_id == "apply-filters":
            self.refresh_view("Applied filters.")
            return
        if button_id == "clear-filters":
            self.clear_filters()

    def current_filters(self) -> dict[str, str | None]:
        values = {
            "account": self.query_one("#filter-account", Input).value or None,
            "category": self.query_one("#filter-category", Input).value or None,
            "payee": self.query_one("#filter-payee", Input).value or None,
            "exact_date": self.query_one("#filter-date", Input).value or None,
            "start_date": self.query_one("#filter-start-date", Input).value or None,
            "end_date": self.query_one("#filter-end-date", Input).value or None,
        }
        return values

    def clear_filters(self) -> None:
        for widget_id in (
            "#filter-account",
            "#filter-category",
            "#filter-payee",
            "#filter-date",
            "#filter-start-date",
            "#filter-end-date",
        ):
            self.query_one(widget_id, Input).value = ""
        self.refresh_view("Cleared filters.")

    def add_transaction_from_form(self) -> None:
        attachment_paths = [
            item.strip()
            for item in self.query_one("#attachments", Input).value.split(",")
            if item.strip()
        ]
        try:
            created = self.tracker.add_transaction(
                account_name=self.query_one("#account", Input).value,
                payee_name=self.query_one("#payee", Input).value,
                category_name=self.query_one("#category", Input).value,
                subcategory_name=self.query_one("#subcategory", Input).value or None,
                amount=self.query_one("#amount", Input).value,
                spent_on=self.query_one("#spent-on", Input).value,
                notes=self.query_one("#notes", Input).value,
                transaction_type=self.query_one("#transaction-type", Input).value,
                attachment_paths=attachment_paths,
            )
        except ValueError as exc:
            self.update_status(str(exc))
            return

        self.query_one("#payee", Input).value = ""
        self.query_one("#amount", Input).value = ""
        self.query_one("#attachments", Input).value = ""
        self.query_one("#notes", Input).value = ""
        self.query_one("#spent-on", Input).value = date.today().isoformat()
        if len(created) == 2:
            self.refresh_view(
                f"Created transfer {created[0].id} -> {created[1].id}."
            )
        else:
            self.refresh_view(f"Added transaction {created[0].id}.")
        self.query_one("#payee", Input).focus()

    def delete_selected_transaction(self) -> None:
        if not self._row_ids:
            self.update_status("No transaction is selected.")
            return
        table = self.query_one("#transaction-table", DataTable)
        cursor_row = table.cursor_row
        if cursor_row >= len(self._row_ids):
            self.update_status("No transaction is selected.")
            return
        transaction_id = self._row_ids[cursor_row]
        if not self.tracker.delete_transaction(transaction_id):
            self.update_status(f"Transaction {transaction_id} was not found.")
            return
        self.refresh_view(f"Deleted transaction {transaction_id}.")

    def refresh_view(self, status_message: str = "Ready.") -> None:
        filters = self.current_filters()
        try:
            transactions = self.tracker.list_transactions(**filters)
            category_totals = self.tracker.category_totals(**filters)
        except ValueError as exc:
            self.update_status(str(exc))
            return

        table = self.query_one("#transaction-table", DataTable)
        table.clear(columns=False)
        self._row_ids = []
        for transaction in transactions:
            self._row_ids.append(transaction.id)
            table.add_row(*self._row_from_transaction(transaction))

        metadata = self.tracker.metadata_snapshot()
        self.query_one("#balances", Static).update(
            render_balances_text(self.tracker.account_balances())
        )
        self.query_one("#summary", Static).update(
            render_category_totals_text(category_totals)
        )
        self.query_one("#catalog", Static).update(render_catalog_text(metadata))
        self.update_status(status_message)

    def _row_from_transaction(self, transaction: TransactionRecord) -> tuple[str, ...]:
        return (
            transaction.spent_on.isoformat(),
            transaction.account_name,
            transaction.payee_name,
            transaction.category_name,
            transaction.subcategory_name or "-",
            f"${transaction.amount:.2f}",
            transaction.entry_type,
            transaction.notes or "-",
            str(len(transaction.attachments)),
            transaction.id,
        )

    def update_status(self, message: str) -> None:
        self.query_one("#status", Static).update(message)
