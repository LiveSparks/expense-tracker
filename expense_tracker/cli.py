from __future__ import annotations

import argparse
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Sequence

from .models import Expense
from .tracker import ExpenseTracker


def default_data_file() -> Path:
    return Path(__file__).resolve().parent.parent / "data" / "expenses.json"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="expense-tracker",
        description="Track personal expenses from the command line.",
    )
    parser.add_argument(
        "--data-file",
        type=Path,
        default=default_data_file(),
        help="Path to the JSON file used for persistence.",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    add_parser = subparsers.add_parser("add", help="Add a new expense.")
    add_parser.add_argument("--description", required=True)
    add_parser.add_argument("--amount", required=True)
    add_parser.add_argument("--category", required=True)
    add_parser.add_argument(
        "--date",
        default=date.today().isoformat(),
        help="Expense date in YYYY-MM-DD format.",
    )

    list_parser = subparsers.add_parser("list", help="List recorded expenses.")
    list_parser.add_argument("--category")
    list_parser.add_argument("--month", help="Filter by YYYY-MM.")

    summary_parser = subparsers.add_parser("summary", help="Show category totals.")
    summary_parser.add_argument("--month", help="Filter by YYYY-MM.")

    delete_parser = subparsers.add_parser("delete", help="Delete an expense by id.")
    delete_parser.add_argument("expense_id")

    subparsers.add_parser("tui", help="Launch the terminal UI.")

    return parser


def launch_tui(data_file: Path) -> None:
    from .tui import ExpenseTrackerApp

    app = ExpenseTrackerApp(data_file)
    app.run()


def render_expenses(expenses: list[Expense], total: Decimal) -> str:
    if not expenses:
        return "No expenses found."

    lines = ["ID | Date | Category | Amount | Description"]
    for expense in expenses:
        lines.append(
            f"{expense.id} | {expense.spent_on.isoformat()} | {expense.category} | "
            f"${expense.amount:.2f} | {expense.description}"
        )
    lines.append(f"Total: ${total:.2f}")
    return "\n".join(lines)


def render_summary(summary: dict[str, Decimal]) -> str:
    if not summary:
        return "No expenses found."

    lines = ["Category totals:"]
    grand_total = Decimal("0.00")
    for category, amount in summary.items():
        grand_total += amount
        lines.append(f"- {category}: ${amount:.2f}")
    lines.append(f"Grand total: ${grand_total:.2f}")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    tracker = ExpenseTracker(args.data_file)

    try:
        if args.command == "add":
            expense = tracker.add_expense(
                description=args.description,
                amount=args.amount,
                category=args.category,
                spent_on=args.date,
            )
            print(f"Added expense {expense.id} for ${expense.amount:.2f}.")
            return 0

        if args.command == "list":
            expenses = tracker.list_expenses(category=args.category, month=args.month)
            print(render_expenses(expenses, tracker.total_spend(expenses)))
            return 0

        if args.command == "summary":
            print(render_summary(tracker.summary(month=args.month)))
            return 0

        if args.command == "delete":
            deleted = tracker.delete_expense(args.expense_id)
            if not deleted:
                parser.error(f"Expense {args.expense_id} was not found.")
            print(f"Deleted expense {args.expense_id}.")
            return 0

        if args.command == "tui":
            launch_tui(args.data_file)
            return 0
    except ValueError as exc:
        parser.error(str(exc))

    parser.error("Unknown command.")
    return 2
