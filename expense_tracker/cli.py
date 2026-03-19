from __future__ import annotations

import argparse
from decimal import Decimal
from pathlib import Path
from typing import Sequence

from .formatting import format_inr
from .review_workflow import ReviewWorkflowStore
from .models import TransactionRecord
from .tracker import ExpenseTracker


def default_data_file() -> Path:
    return Path(__file__).resolve().parent.parent / "data" / "ledger.db"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="expense-tracker",
        description="Track accounts, payees, transfers, and attachments from the command line.",
    )
    parser.add_argument(
        "--data-file",
        type=Path,
        default=default_data_file(),
        help="Path to the SQLite file used for persistence.",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    add_parser = subparsers.add_parser("add", help="Add a transaction.")
    add_parser.add_argument("--account", required=True)
    add_parser.add_argument("--payee", required=True)
    add_parser.add_argument("--category", required=True)
    add_parser.add_argument("--subcategory")
    add_parser.add_argument("--amount", required=True)
    add_parser.add_argument("--date", required=True)
    add_parser.add_argument("--notes", default="")
    add_parser.add_argument("--attachment", action="append", default=[])

    list_parser = subparsers.add_parser("list", help="List transactions.")
    list_parser.add_argument("--account")
    list_parser.add_argument("--category")
    list_parser.add_argument("--payee")
    list_parser.add_argument("--search")
    list_parser.add_argument("--date")
    list_parser.add_argument("--start-date")
    list_parser.add_argument("--end-date")

    summary_parser = subparsers.add_parser("summary", help="Show balances and totals.")
    summary_parser.add_argument("--account")
    summary_parser.add_argument("--category")
    summary_parser.add_argument("--payee")
    summary_parser.add_argument("--search")
    summary_parser.add_argument("--date")
    summary_parser.add_argument("--start-date")
    summary_parser.add_argument("--end-date")

    delete_parser = subparsers.add_parser("delete", help="Delete a transaction by id.")
    delete_parser.add_argument("transaction_id")

    serve_parser = subparsers.add_parser("serve", help="Run the web UI and API server.")
    serve_parser.add_argument("--host", default="127.0.0.1")
    serve_parser.add_argument("--port", type=int, default=8000)

    analyze_parser = subparsers.add_parser("analyze-sms", help="Run the legacy SMS analysis pipeline.")
    analyze_parser.add_argument("--transactions-csv", type=Path, default=Path("/root/All-Accounts_2.csv"))
    analyze_parser.add_argument("--sms-csv", type=Path, default=Path("/root/all_sms.csv"))
    analyze_parser.add_argument("--output-dir", type=Path, default=Path(__file__).resolve().parent.parent / "data" / "sms_pipeline")

    seed_parser = subparsers.add_parser("seed-demo-data", help="Import generated mock ledger data into the app database.")
    seed_parser.add_argument(
        "--seed-file",
        type=Path,
        default=Path(__file__).resolve().parent.parent / "data" / "sms_pipeline" / "mock_ledger_seed.json",
    )

    import_parser = subparsers.add_parser("import-ledger-csv", help="Replace the app database with transactions from a legacy CSV export.")
    import_parser.add_argument("--csv-file", type=Path, default=Path("/root/All-Accounts_2.csv"))

    return parser

def launch_web(data_file: Path, host: str, port: int) -> None:
    import uvicorn

    from .web import create_app

    uvicorn.run(create_app(data_file), host=host, port=port)


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
            f"{transaction.subcategory_name or '-'} | {format_inr(transaction.amount)} | "
            f"{transaction.notes or '-'} | {len(transaction.attachments)}"
        )
    lines.append(f"Net total: {format_inr(total)}")
    return "\n".join(lines)


def render_summary(
    balances: dict[str, Decimal],
    category_totals: dict[str, Decimal],
) -> str:
    lines = ["Account balances:"]
    if balances:
        for account, amount in balances.items():
            lines.append(f"- {account}: {format_inr(amount)}")
    else:
        lines.append("- No accounts yet.")

    lines.append("")
    lines.append("Category totals:")
    if category_totals:
        for category, amount in category_totals.items():
            lines.append(f"- {category}: {format_inr(amount)}")
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
                attachment_paths=args.attachment,
            )
            if len(created) == 2:
                print(
                    f"Created transfer {created[0].id} -> {created[1].id} for {format_inr(abs(created[0].amount))}."
                )
            else:
                print(f"Added transaction {created[0].id} for {format_inr(abs(created[0].amount))}.")
            return 0

        if args.command == "list":
            transactions = tracker.list_transactions(
                account=args.account,
                category=args.category,
                payee=args.payee,
                search=args.search,
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
                        search=args.search,
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

        if args.command == "serve":
            launch_web(args.data_file, args.host, args.port)
            return 0

        if args.command == "analyze-sms":
            from .sms_pipeline import analyze_legacy_sms_data

            artifacts = analyze_legacy_sms_data(args.transactions_csv, args.sms_csv, args.output_dir)
            matched = len([match for match in artifacts.matches if match.transaction is not None])
            print(
                f"Analyzed {len(artifacts.transactions)} transactions and {len(artifacts.useful_sms)} useful SMS messages; matched {matched}. Artifacts written to {args.output_dir}."
            )
            return 0

        if args.command == "seed-demo-data":
            summary = tracker.import_ledger_seed(args.seed_file)
            print(
                "Imported demo seed "
                f"({summary['transactions_added']} transactions added, {summary['transactions_skipped']} skipped) from {args.seed_file}."
            )
            return 0

        if args.command == "import-ledger-csv":
            summary = tracker.import_legacy_csv(args.csv_file)
            ReviewWorkflowStore(args.data_file).clear_reviews()
            skipped_suffix = f", {summary['skipped_rows']} skipped zero-amount rows" if summary["skipped_rows"] else ""
            print(
                "Imported real ledger "
                f"({summary['transactions']} transactions, {summary['accounts']} accounts, {summary['payees']} payees, "
                f"{summary['categories']} categories, {summary['subcategories']} subcategories{skipped_suffix}) from {args.csv_file}."
            )
            return 0
    except ValueError as exc:
        parser.error(str(exc))

    parser.error("Unknown command.")
    return 2
