from __future__ import annotations

import argparse
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Sequence

from .models import TransactionRecord
from .tracker import ExpenseTracker


def default_data_file() -> Path:
    return Path(__file__).resolve().parent.parent / "data" / "ledger.json"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="expense-tracker",
        description="Track accounts, payees, transfers, and attachments from the command line.",
    )
    parser.add_argument(
        "--data-file",
        type=Path,
        default=default_data_file(),
        help="Path to the JSON file used for persistence.",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    add_parser = subparsers.add_parser("add", help="Add a transaction.")
    add_parser.add_argument("--account", required=True)
    add_parser.add_argument("--payee", required=True)
    add_parser.add_argument("--category", required=True)
    add_parser.add_argument("--subcategory")
    add_parser.add_argument("--amount", required=True)
    add_parser.add_argument("--type", choices=["expense", "income", "transfer"], default="expense")
    add_parser.add_argument("--date", default=date.today().isoformat())
    add_parser.add_argument("--notes", default="")
    add_parser.add_argument("--attachment", action="append", default=[])

    list_parser = subparsers.add_parser("list", help="List transactions.")
    list_parser.add_argument("--account")
    list_parser.add_argument("--category")
    list_parser.add_argument("--payee")
    list_parser.add_argument("--date")
    list_parser.add_argument("--start-date")
    list_parser.add_argument("--end-date")

    summary_parser = subparsers.add_parser("summary", help="Show balances and totals.")
    summary_parser.add_argument("--account")
    summary_parser.add_argument("--category")
    summary_parser.add_argument("--payee")
    summary_parser.add_argument("--date")
    summary_parser.add_argument("--start-date")
    summary_parser.add_argument("--end-date")

    delete_parser = subparsers.add_parser("delete", help="Delete a transaction by id.")
    delete_parser.add_argument("transaction_id")

    subparsers.add_parser("tui", help="Launch the terminal UI.")

    return parser


def launch_tui(data_file: Path) -> None:
    from .tui import ExpenseTrackerApp

    app = ExpenseTrackerApp(data_file)
    app.run()


def render_transactions(transactions: list[TransactionRecord], total: Decimal) -> str:
    if not transactions:
        return "No transactions found."

    lines = [
        "ID | Date | Account | Payee | Category | Subcategory | Amount | Notes | Files"
    ]
    for transaction in transactions:
        lines.append(
            f"{transaction.id} | {transaction.spent_on.isoformat()} | {transaction.account_name} | "
            f"{transaction.payee_name} | {transaction.category_name} | "
            f"{transaction.subcategory_name or '-'} | ${transaction.amount:.2f} | "
            f"{transaction.notes or '-'} | {len(transaction.attachments)}"
        )
    lines.append(f"Net total: ${total:.2f}")
    return "\n".join(lines)


def render_summary(
    balances: dict[str, Decimal],
    category_totals: dict[str, Decimal],
) -> str:
    lines = ["Account balances:"]
    if balances:
        for account, amount in balances.items():
            lines.append(f"- {account}: ${amount:.2f}")
    else:
        lines.append("- No accounts yet.")

    lines.append("")
    lines.append("Category totals:")
    if category_totals:
        for category, amount in category_totals.items():
            lines.append(f"- {category}: ${amount:.2f}")
    else:
        lines.append("- No matching transactions.")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    tracker = ExpenseTracker(args.data_file)

    try:
        if args.command == "add":
            created = tracker.add_transaction(
                account_name=args.account,
                payee_name=args.payee,
                category_name=args.category,
                subcategory_name=args.subcategory,
                amount=args.amount,
                spent_on=args.date,
                notes=args.notes,
                transaction_type=args.type,
                attachment_paths=args.attachment,
            )
            if len(created) == 2:
                print(
                    f"Created transfer {created[0].id} -> {created[1].id} for ${abs(created[0].amount):.2f}."
                )
            else:
                print(f"Added transaction {created[0].id} for ${abs(created[0].amount):.2f}.")
            return 0

        if args.command == "list":
            transactions = tracker.list_transactions(
                account=args.account,
                category=args.category,
                payee=args.payee,
                exact_date=args.date,
                start_date=args.start_date,
                end_date=args.end_date,
            )
            print(render_transactions(transactions, tracker.total_amount(transactions)))
            return 0

        if args.command == "summary":
            print(
                render_summary(
                    tracker.account_balances(),
                    tracker.category_totals(
                        account=args.account,
                        category=args.category,
                        payee=args.payee,
                        exact_date=args.date,
                        start_date=args.start_date,
                        end_date=args.end_date,
                    ),
                )
            )
            return 0

        if args.command == "delete":
            deleted = tracker.delete_transaction(args.transaction_id)
            if not deleted:
                parser.error(f"Transaction {args.transaction_id} was not found.")
            print(f"Deleted transaction {args.transaction_id}.")
            return 0

        if args.command == "tui":
            launch_tui(args.data_file)
            return 0
    except ValueError as exc:
        parser.error(str(exc))

    parser.error("Unknown command.")
    return 2
