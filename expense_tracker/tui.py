from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path

from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.reactive import reactive
from textual.widgets import Button, DataTable, Footer, Header, Input, OptionList, Static
from textual.widgets.option_list import Option, Separator

from .models import MetadataSnapshot, TransactionRecord
from .tracker import ExpenseTracker


@dataclass(slots=True)
class SuggestionItem:
    label: str
    value: str | None
    disabled: bool = False


class SuggestField(Vertical):
    DEFAULT_CSS = """
    SuggestField {
        height: auto;
        margin-bottom: 1;
    }

    SuggestField > OptionList {
        display: none;
        height: 7;
        border: round $surface;
    }

    SuggestField.-open > OptionList {
        display: block;
    }
    """

    class ValueSelected(Message):
        def __init__(self, field_id: str, value: str) -> None:
            self.field_id = field_id
            self.value = value
            super().__init__()

    suggestions = reactive(list[SuggestionItem])

    def __init__(self, *, field_id: str, placeholder: str) -> None:
        super().__init__(id=f"{field_id}-field")
        self.field_id = field_id
        self.placeholder = placeholder
        self._suggestions: list[SuggestionItem] = []
        self._suppress_events = False

    def compose(self) -> ComposeResult:
        yield Input(placeholder=self.placeholder, id=f"{self.field_id}-input")
        yield OptionList(id=f"{self.field_id}-options")

    @property
    def input(self) -> Input:
        return self.query_one(Input)

    @property
    def option_list(self) -> OptionList:
        return self.query_one(OptionList)

    @property
    def value(self) -> str:
        return self.input.value

    @value.setter
    def value(self, new_value: str) -> None:
        self._suppress_events = True
        self.input.value = new_value
        self._suppress_events = False
        self.close()

    def focus_input(self) -> None:
        self.input.focus()

    def set_suggestions(self, suggestions: list[SuggestionItem]) -> None:
        self._suggestions = suggestions
        option_list = self.option_list
        option_list.clear_options()
        for index, item in enumerate(suggestions):
            option = Option(item.label, id=str(index), disabled=item.disabled)
            option_list.add_option(option)
        if suggestions:
            option_list.highlighted = 0
            self.add_class("-open")
        else:
            self.close()

    def close(self) -> None:
        self.remove_class("-open")
        self.option_list.clear_options()
        self._suggestions = []

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id != f"{self.field_id}-input" or self._suppress_events:
            return
        self.post_message(self.ValueSelected(self.field_id, event.value))

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != f"{self.field_id}-input":
            return
        if self._suggestions:
            self._select_index(self.option_list.highlighted or 0)
        else:
            self.post_message(self.ValueSelected(self.field_id, event.value))

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        if event.option_list.id != f"{self.field_id}-options":
            return
        if event.option.id is None:
            return
        self._select_index(int(event.option.id))

    def _select_index(self, index: int) -> None:
        if index >= len(self._suggestions):
            return
        suggestion = self._suggestions[index]
        if suggestion.disabled or suggestion.value is None:
            return
        self.value = suggestion.value
        self.post_message(self.ValueSelected(self.field_id, suggestion.value))


def render_balances_text(balances: dict[str, Decimal]) -> str:
    if not balances:
        return "Account balances: - No accounts yet."
    return "Account balances: " + " | ".join(
        f"{account}: ${amount:.2f}" for account, amount in balances.items()
    )


def render_category_totals_text(summary: dict[str, Decimal]) -> str:
    if not summary:
        return "Category totals: - No matching transactions."
    return "Category totals: " + " | ".join(
        f"{category}: ${amount:.2f}" for category, amount in summary.items()
    )


def split_category_value(value: str) -> tuple[str, str | None]:
    if " / " not in value:
        return value.strip(), None
    category_name, subcategory_name = value.split(" / ", maxsplit=1)
    return category_name.strip(), subcategory_name.strip() or None


class ExpenseTrackerApp(App[None]):
    TITLE = "Expense Tracker"
    SUB_TITLE = "Compact TUI"
    CSS = """
    Screen {
        layout: vertical;
    }

    #body {
        height: 1fr;
    }

    #left-panel {
        width: 46;
        min-width: 46;
        height: 1fr;
        border: round $surface;
        padding: 1 1;
    }

    #right-panel {
        width: 1fr;
        height: 1fr;
        border: round $surface;
        padding: 1 1;
    }

    .section-title {
        text-style: bold;
        margin-bottom: 1;
    }

    .form-row {
        height: auto;
    }

    .form-row > * {
        width: 1fr;
        margin-right: 1;
    }

    #actions, #filter-actions {
        height: auto;
        margin-top: 1;
    }

    Button {
        margin-right: 1;
    }

    #status {
        margin-top: 1;
        min-height: 1;
    }

    #balances, #summary {
        margin-top: 1;
    }

    #filter-panel {
        height: auto;
    }

    #filter-panel > * {
        width: 1fr;
        margin-right: 1;
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
                with Horizontal(classes="form-row"):
                    yield SuggestField(field_id="payee", placeholder="Payee")
                    yield SuggestField(field_id="account", placeholder="Account")
                with Horizontal(classes="form-row"):
                    yield SuggestField(field_id="category", placeholder="Category or subcategory")
                    yield Input(placeholder="Amount (+income, -expense, transfers use account payee)", id="amount")
                with Horizontal(classes="form-row"):
                    yield Input(value=date.today().isoformat(), placeholder="YYYY-MM-DD", id="spent-on")
                    yield Input(placeholder="Attachment paths, comma separated", id="attachments")
                yield Input(placeholder="Notes", id="notes")
                with Horizontal(id="actions"):
                    yield Button("Add", variant="primary", id="add-transaction")
                    yield Button("Delete", variant="error", id="delete-transaction")
                    yield Button("Refresh", id="refresh-view")
                yield Static("Ready.", id="status")
                yield Static("", id="balances")
                yield Static("", id="summary")
            with Vertical(id="right-panel"):
                yield Static("Filters", classes="section-title")
                with Horizontal(id="filter-panel"):
                    yield Input(placeholder="Account", id="filter-account")
                    yield Input(placeholder="Category", id="filter-category")
                    yield Input(placeholder="Payee", id="filter-payee")
                    yield Input(placeholder="Date", id="filter-date")
                    yield Input(placeholder="From", id="filter-start-date")
                    yield Input(placeholder="To", id="filter-end-date")
                with Horizontal(id="filter-actions"):
                    yield Button("Apply filters", id="apply-filters")
                    yield Button("Clear", id="clear-filters")
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
            "Amount",
            "Notes",
            "Files",
            "Transaction ID",
        )
        self.refresh_view()
        self.query_one("#payee-field", SuggestField).focus_input()

    def action_reload_data(self) -> None:
        self.refresh_view("Reloaded data.")

    def action_apply_filters(self) -> None:
        self.refresh_view("Applied filters.")

    def on_suggest_field_value_selected(self, event: SuggestField.ValueSelected) -> None:
        metadata = self.tracker.metadata_snapshot()
        if event.field_id == "payee":
            self.query_one("#payee-field", SuggestField).set_suggestions(
                self._build_name_suggestions(event.value, metadata.payees, "Payee")
            )
        elif event.field_id == "account":
            self.query_one("#account-field", SuggestField).set_suggestions(
                self._build_name_suggestions(event.value, metadata.accounts, "Account")
            )
        elif event.field_id == "category":
            self.query_one("#category-field", SuggestField).set_suggestions(
                self._build_category_suggestions(event.value, metadata)
            )

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
        return {
            "account": self.query_one("#filter-account", Input).value or None,
            "category": self.query_one("#filter-category", Input).value or None,
            "payee": self.query_one("#filter-payee", Input).value or None,
            "exact_date": self.query_one("#filter-date", Input).value or None,
            "start_date": self.query_one("#filter-start-date", Input).value or None,
            "end_date": self.query_one("#filter-end-date", Input).value or None,
        }

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
        category_value = self.query_one("#category-field", SuggestField).value
        category_name, subcategory_name = split_category_value(category_value)
        try:
            created = self.tracker.add_transaction(
                account_name=self.query_one("#account-field", SuggestField).value,
                payee_name=self.query_one("#payee-field", SuggestField).value,
                category_name=category_name,
                subcategory_name=subcategory_name,
                amount=self.query_one("#amount", Input).value,
                spent_on=self.query_one("#spent-on", Input).value,
                notes=self.query_one("#notes", Input).value,
                attachment_paths=attachment_paths,
            )
        except ValueError as exc:
            self.update_status(str(exc))
            return

        self.query_one("#payee-field", SuggestField).value = ""
        self.query_one("#amount", Input).value = ""
        self.query_one("#attachments", Input).value = ""
        self.query_one("#notes", Input).value = ""
        self.query_one("#spent-on", Input).value = date.today().isoformat()
        if len(created) == 2:
            self.refresh_view(f"Created transfer {created[0].id} -> {created[1].id}.")
        else:
            self.refresh_view(f"Added transaction {created[0].id}.")
        self.query_one("#payee-field", SuggestField).focus_input()

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

        self.query_one("#balances", Static).update(
            render_balances_text(self.tracker.account_balances())
        )
        self.query_one("#summary", Static).update(
            render_category_totals_text(category_totals)
        )
        metadata = self.tracker.metadata_snapshot()
        self.query_one("#payee-field", SuggestField).set_suggestions([])
        self.query_one("#account-field", SuggestField).set_suggestions([])
        self.query_one("#category-field", SuggestField).set_suggestions([])
        self.update_status(status_message)

    def _row_from_transaction(self, transaction: TransactionRecord) -> tuple[str, ...]:
        category_value = transaction.category_name
        if transaction.subcategory_name:
            category_value = f"{category_value} / {transaction.subcategory_name}"
        return (
            transaction.spent_on.isoformat(),
            transaction.account_name,
            transaction.payee_name,
            category_value,
            f"${transaction.amount:.2f}",
            transaction.notes or "-",
            str(len(transaction.attachments)),
            transaction.id,
        )

    def _build_name_suggestions(
        self,
        query: str,
        names: list[str],
        noun: str,
    ) -> list[SuggestionItem]:
        normalized_query = query.strip().casefold()
        matches = [
            name for name in names if not normalized_query or normalized_query in name.casefold()
        ]
        suggestions = [SuggestionItem(label=name, value=name) for name in matches[:8]]
        if query.strip() and query.strip() not in names:
            suggestions.append(
                SuggestionItem(label=f'Add new {noun.lower()}: {query.strip()}', value=query.strip())
            )
        return suggestions

    def _build_category_suggestions(
        self,
        query: str,
        metadata: MetadataSnapshot,
    ) -> list[SuggestionItem]:
        normalized_query = query.strip().casefold()
        suggestions: list[SuggestionItem] = []
        for category in metadata.categories:
            category_matches = not normalized_query or normalized_query in category.casefold()
            subcategories = metadata.subcategories_by_category.get(category, [])
            matching_subcategories = [
                subcategory
                for subcategory in subcategories
                if not normalized_query or normalized_query in subcategory.casefold() or category_matches
            ]
            if not category_matches and not matching_subcategories:
                continue
            suggestions.append(SuggestionItem(label=f"[{category}]", value=None, disabled=True))
            suggestions.append(SuggestionItem(label=category, value=category))
            for subcategory in matching_subcategories:
                suggestions.append(
                    SuggestionItem(
                        label=f"  {subcategory}",
                        value=f"{category} / {subcategory}",
                    )
                )
            suggestions.append(SuggestionItem(label="", value=None, disabled=True))
        if query.strip() and all(query.strip() != category for category in metadata.categories):
            suggestions.append(
                SuggestionItem(label=f"Add new category: {query.strip()}", value=query.strip())
            )
        return suggestions[:18]

    def update_status(self, message: str) -> None:
        self.query_one("#status", Static).update(message)
