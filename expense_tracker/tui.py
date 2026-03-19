from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, DataTable, Footer, Header, Input, Static

from .tracker import ExpenseTracker


def render_summary_text(summary: dict[str, Decimal]) -> str:
    if not summary:
        return "No expenses recorded yet."

    lines = ["Category totals:"]
    grand_total = Decimal("0.00")
    for category, amount in summary.items():
        grand_total += amount
        lines.append(f"- {category}: ${amount:.2f}")
    lines.append(f"Grand total: ${grand_total:.2f}")
    return "\n".join(lines)


class ExpenseTrackerApp(App[None]):
    TITLE = "Expense Tracker"
    SUB_TITLE = "Terminal UI"
    CSS = """
    Screen {
        layout: vertical;
    }

    #body {
        height: 1fr;
    }

    #form-panel, #table-panel {
        width: 1fr;
        height: 1fr;
        border: round $surface;
        padding: 1 2;
    }

    #form-panel {
        max-width: 42;
    }

    .panel-title {
        text-style: bold;
        margin-bottom: 1;
    }

    Input {
        margin-bottom: 1;
    }

    #actions {
        height: auto;
        margin-top: 1;
    }

    Button {
        margin-right: 1;
    }

    #status {
        margin-top: 1;
        min-height: 3;
        color: $text;
    }

    #summary {
        margin-top: 1;
    }

    DataTable {
        height: 1fr;
    }
    """
    BINDINGS = [
        ("q", "quit", "Quit"),
        ("r", "reload_data", "Refresh"),
    ]

    def __init__(self, data_file: Path) -> None:
        super().__init__()
        self.tracker = ExpenseTracker(data_file)
        self._row_ids: list[str] = []

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="body"):
            with Vertical(id="form-panel"):
                yield Static("Manage expenses", classes="panel-title")
                yield Input(placeholder="Description", id="description")
                yield Input(placeholder="Amount", id="amount")
                yield Input(placeholder="Category", id="category")
                yield Input(value=date.today().isoformat(), placeholder="YYYY-MM-DD", id="spent-on")
                with Horizontal(id="actions"):
                    yield Button("Add expense", variant="primary", id="add-expense")
                    yield Button("Delete selected", variant="error", id="delete-expense")
                    yield Button("Refresh", id="refresh-expenses")
                yield Static("Ready.", id="status")
                yield Static("", id="summary")
            with Vertical(id="table-panel"):
                yield Static("Expenses", classes="panel-title")
                yield DataTable(id="expense-table")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#expense-table", DataTable)
        table.cursor_type = "row"
        table.zebra_stripes = True
        table.add_columns("Date", "Category", "Amount", "Description", "Expense ID")
        self.refresh_view()
        self.query_one("#description", Input).focus()

    def action_reload_data(self) -> None:
        self.refresh_view("Expenses reloaded.")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "add-expense":
            self.add_expense_from_form()
            return

        if event.button.id == "delete-expense":
            self.delete_selected_expense()
            return

        if event.button.id == "refresh-expenses":
            self.refresh_view("Expenses reloaded.")

    def add_expense_from_form(self) -> None:
        try:
            expense = self.tracker.add_expense(
                description=self.query_one("#description", Input).value,
                amount=self.query_one("#amount", Input).value,
                category=self.query_one("#category", Input).value,
                spent_on=self.query_one("#spent-on", Input).value,
            )
        except ValueError as exc:
            self.update_status(str(exc))
            return

        self.query_one("#description", Input).value = ""
        self.query_one("#amount", Input).value = ""
        self.query_one("#category", Input).value = ""
        self.query_one("#spent-on", Input).value = date.today().isoformat()
        self.refresh_view(f"Added expense {expense.id}.")
        self.query_one("#description", Input).focus()

    def delete_selected_expense(self) -> None:
        if not self._row_ids:
            self.update_status("No expense is selected.")
            return

        table = self.query_one("#expense-table", DataTable)
        cursor_row = table.cursor_row
        if cursor_row >= len(self._row_ids):
            self.update_status("No expense is selected.")
            return

        expense_id = self._row_ids[cursor_row]
        deleted = self.tracker.delete_expense(expense_id)
        if not deleted:
            self.update_status(f"Expense {expense_id} was not found.")
            return

        self.refresh_view(f"Deleted expense {expense_id}.")

    def refresh_view(self, status_message: str = "Ready.") -> None:
        expenses = self.tracker.list_expenses()
        table = self.query_one("#expense-table", DataTable)
        table.clear(columns=False)
        self._row_ids = []

        for expense in expenses:
            self._row_ids.append(expense.id)
            table.add_row(
                expense.spent_on.isoformat(),
                expense.category,
                f"${expense.amount:.2f}",
                expense.description,
                expense.id,
            )

        summary = self.tracker.summary()
        self.query_one("#summary", Static).update(render_summary_text(summary))
        self.update_status(status_message)

    def update_status(self, message: str) -> None:
        self.query_one("#status", Static).update(message)
