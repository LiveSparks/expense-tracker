from __future__ import annotations

import shutil
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator
from urllib.parse import urlencode

from fastapi import Body, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .cli import default_data_file
from .tracker import ExpenseTracker

BASE_DIR = Path(__file__).resolve().parent
TEMPLATES = Jinja2Templates(directory=str(BASE_DIR / "templates"))


def category_options_from_tracker(tracker: ExpenseTracker) -> list[str]:
    metadata = tracker.metadata_snapshot()
    options: list[str] = []
    for category in metadata.categories:
        options.append(category)
        for subcategory in metadata.subcategories_by_category.get(category, []):
            options.append(f"{category} / {subcategory}")
    return options


def split_category_value(value: str) -> tuple[str, str | None]:
    if " / " not in value:
        return value.strip(), None
    category_name, subcategory_name = value.split(" / ", maxsplit=1)
    return category_name.strip(), subcategory_name.strip() or None


@contextmanager
def saved_uploads(files: list[UploadFile]) -> Iterator[list[str]]:
    temp_paths: list[str] = []
    try:
        for upload in files:
            if not upload.filename:
                continue
            suffix = Path(upload.filename).suffix
            with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as handle:
                shutil.copyfileobj(upload.file, handle)
                temp_paths.append(handle.name)
        yield temp_paths
    finally:
        for path in temp_paths:
            temp_file = Path(path)
            if temp_file.exists():
                temp_file.unlink()
        for upload in files:
            upload.file.close()


def serialize_transaction(record) -> dict[str, object]:
    return {
        "id": record.id,
        "entry_type": record.entry_type,
        "account_name": record.account_name,
        "payee_name": record.payee_name,
        "category_name": record.category_name,
        "subcategory_name": record.subcategory_name,
        "amount": f"{record.amount:.2f}",
        "notes": record.notes,
        "spent_on": record.spent_on.isoformat(),
        "linked_transaction_id": record.linked_transaction_id,
        "transfer_group_id": record.transfer_group_id,
        "attachments": [
            {"id": item.id, "name": item.original_name, "path": item.stored_path}
            for item in record.attachments
        ],
    }


def create_app(data_file: Path | None = None) -> FastAPI:
    app = FastAPI(title="Expense Tracker")
    app.state.data_file = data_file or default_data_file()
    app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

    def tracker() -> ExpenseTracker:
        return ExpenseTracker(app.state.data_file)

    def transaction_context(current_tracker: ExpenseTracker, **filters: str | None) -> dict[str, object]:
        metadata = current_tracker.metadata_snapshot()
        transactions = current_tracker.list_transactions(**filters)
        return {
            "filters": filters,
            "transactions": transactions,
            "total_amount": current_tracker.total_amount(transactions),
            "accounts": metadata.accounts,
            "payees": metadata.payees,
            "category_options": category_options_from_tracker(current_tracker),
        }

    @app.get("/")
    def dashboard(request: Request):
        current_tracker = tracker()
        return TEMPLATES.TemplateResponse(
            request,
            "dashboard.html",
            {"accounts": current_tracker.account_summaries(), "balances": current_tracker.account_balances()},
        )

    @app.get("/transactions")
    def transaction_list(
        request: Request,
        account: str | None = None,
        category: str | None = None,
        payee: str | None = None,
        date: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
    ):
        current_tracker = tracker()
        return TEMPLATES.TemplateResponse(
            request,
            "transactions.html",
            transaction_context(
                current_tracker,
                account=account,
                category=category,
                payee=payee,
                exact_date=date,
                start_date=start_date,
                end_date=end_date,
            ),
        )

    @app.get("/transactions/new")
    def new_transaction_form(request: Request, account: str | None = None):
        current_tracker = tracker()
        metadata = current_tracker.metadata_snapshot()
        return TEMPLATES.TemplateResponse(
            request,
            "transaction_form.html",
            {
                "mode": "create",
                "transaction": None,
                "accounts": metadata.accounts,
                "payees": metadata.payees,
                "category_options": category_options_from_tracker(current_tracker),
                "selected_account": account or "",
                "selected_payee": "",
                "selected_category": "",
                "error": None,
            },
        )

    @app.post("/transactions")
    async def create_transaction(
        request: Request,
        account_name: str = Form(...),
        payee_name: str = Form(...),
        category_value: str = Form(...),
        amount: str = Form(...),
        spent_on: str = Form(...),
        notes: str = Form(""),
        files: list[UploadFile] = File(default=[]),
    ):
        current_tracker = tracker()
        category_name, subcategory_name = split_category_value(category_value)
        try:
            with saved_uploads(files) as paths:
                current_tracker.add_transaction(
                    account_name=account_name,
                    payee_name=payee_name,
                    category_name=category_name,
                    subcategory_name=subcategory_name,
                    amount=amount,
                    spent_on=spent_on,
                    notes=notes,
                    attachment_paths=paths,
                )
        except ValueError as exc:
            metadata = current_tracker.metadata_snapshot()
            return TEMPLATES.TemplateResponse(
                request,
                "transaction_form.html",
                {
                    "mode": "create",
                    "transaction": None,
                    "accounts": metadata.accounts,
                    "payees": metadata.payees,
                    "category_options": category_options_from_tracker(current_tracker),
                    "selected_account": account_name,
                    "selected_payee": payee_name,
                    "selected_category": category_value,
                    "error": str(exc),
                },
                status_code=400,
            )
        return RedirectResponse(f"/transactions?{urlencode({'account': account_name})}", status_code=303)

    @app.get("/transactions/{transaction_id}/edit")
    def edit_transaction_form(request: Request, transaction_id: str):
        current_tracker = tracker()
        transaction = current_tracker.get_transaction(transaction_id)
        if transaction is None:
            raise HTTPException(status_code=404, detail="Transaction not found")
        metadata = current_tracker.metadata_snapshot()
        selected_category = transaction.category_name
        if transaction.subcategory_name:
            selected_category = f"{selected_category} / {transaction.subcategory_name}"
        return TEMPLATES.TemplateResponse(
            request,
            "transaction_form.html",
            {
                "mode": "edit",
                "transaction": transaction,
                "accounts": metadata.accounts,
                "payees": metadata.payees,
                "category_options": category_options_from_tracker(current_tracker),
                "selected_account": transaction.account_name,
                "selected_payee": transaction.payee_name,
                "selected_category": selected_category,
                "error": None,
            },
        )

    @app.post("/transactions/{transaction_id}/edit")
    async def edit_transaction(
        request: Request,
        transaction_id: str,
        account_name: str = Form(...),
        payee_name: str = Form(...),
        category_value: str = Form(...),
        amount: str = Form(...),
        spent_on: str = Form(...),
        notes: str = Form(""),
        files: list[UploadFile] = File(default=[]),
    ):
        current_tracker = tracker()
        category_name, subcategory_name = split_category_value(category_value)
        try:
            with saved_uploads(files) as paths:
                current_tracker.update_transaction(
                    transaction_id,
                    account_name=account_name,
                    payee_name=payee_name,
                    category_name=category_name,
                    subcategory_name=subcategory_name,
                    amount=amount,
                    spent_on=spent_on,
                    notes=notes,
                    attachment_paths=paths,
                )
        except ValueError as exc:
            transaction = current_tracker.get_transaction(transaction_id)
            metadata = current_tracker.metadata_snapshot()
            return TEMPLATES.TemplateResponse(
                request,
                "transaction_form.html",
                {
                    "mode": "edit",
                    "transaction": transaction,
                    "accounts": metadata.accounts,
                    "payees": metadata.payees,
                    "category_options": category_options_from_tracker(current_tracker),
                    "selected_account": account_name,
                    "selected_payee": payee_name,
                    "selected_category": category_value,
                    "error": str(exc),
                },
                status_code=400,
            )
        return RedirectResponse(f"/transactions?{urlencode({'account': account_name})}", status_code=303)

    @app.post("/transactions/{transaction_id}/delete")
    def delete_transaction(transaction_id: str):
        tracker().delete_transaction(transaction_id)
        return RedirectResponse("/transactions", status_code=303)

    @app.get("/attachments/{transaction_id}/{attachment_name}")
    def attachment_file(transaction_id: str, attachment_name: str):
        current_tracker = tracker()
        transaction = current_tracker.get_transaction(transaction_id)
        if transaction is None:
            raise HTTPException(status_code=404, detail="Transaction not found")
        attachment = next((item for item in transaction.attachments if item.original_name == attachment_name), None)
        if attachment is None:
            raise HTTPException(status_code=404, detail="Attachment not found")
        return FileResponse(attachment.stored_path, filename=attachment.original_name)

    @app.get("/manage")
    def manage_index(request: Request):
        return TEMPLATES.TemplateResponse(request, "manage_index.html", {})

    @app.get("/manage/accounts")
    def manage_accounts(request: Request, error: str | None = None):
        return TEMPLATES.TemplateResponse(request, "manage_accounts.html", {"accounts": tracker().account_summaries(), "error": error})

    @app.post("/manage/accounts")
    async def manage_accounts_post(action: str = Form(...), name: str = Form(""), current_name: str = Form(""), new_name: str = Form("")):
        current_tracker = tracker()
        try:
            if action == "create":
                current_tracker.create_account(name)
            elif action == "rename":
                current_tracker.rename_account(current_name, new_name)
            elif action == "delete":
                current_tracker.delete_account(current_name)
        except ValueError as exc:
            return RedirectResponse(f"/manage/accounts?error={exc}", status_code=303)
        return RedirectResponse("/manage/accounts", status_code=303)

    @app.get("/manage/payees")
    def manage_payees(request: Request, error: str | None = None):
        return TEMPLATES.TemplateResponse(request, "manage_payees.html", {"payees": tracker().payee_summaries(), "error": error})

    @app.post("/manage/payees")
    async def manage_payees_post(action: str = Form(...), name: str = Form(""), current_name: str = Form(""), new_name: str = Form("")):
        current_tracker = tracker()
        try:
            if action == "create":
                current_tracker.create_payee(name)
            elif action == "rename":
                current_tracker.rename_payee(current_name, new_name)
            elif action == "delete":
                current_tracker.delete_payee(current_name)
        except ValueError as exc:
            return RedirectResponse(f"/manage/payees?error={exc}", status_code=303)
        return RedirectResponse("/manage/payees", status_code=303)

    @app.get("/manage/categories")
    def manage_categories(request: Request, error: str | None = None):
        return TEMPLATES.TemplateResponse(request, "manage_categories.html", {"categories": tracker().category_summaries(), "error": error})

    @app.post("/manage/categories")
    async def manage_categories_post(
        action: str = Form(...),
        name: str = Form(""),
        current_name: str = Form(""),
        new_name: str = Form(""),
        category_name: str = Form(""),
        subcategory_name: str = Form(""),
    ):
        current_tracker = tracker()
        try:
            if action == "create-category":
                current_tracker.create_category(name)
            elif action == "rename-category":
                current_tracker.rename_category(current_name, new_name)
            elif action == "delete-category":
                current_tracker.delete_category(current_name)
            elif action == "create-subcategory":
                current_tracker.create_subcategory(category_name, subcategory_name)
            elif action == "rename-subcategory":
                current_tracker.rename_subcategory(category_name, current_name, new_name)
            elif action == "delete-subcategory":
                current_tracker.delete_subcategory(category_name, current_name)
        except ValueError as exc:
            return RedirectResponse(f"/manage/categories?error={exc}", status_code=303)
        return RedirectResponse("/manage/categories", status_code=303)

    @app.get("/api/accounts")
    def api_accounts():
        return {"accounts": tracker().account_summaries()}

    @app.post("/api/accounts")
    def api_create_account(payload: dict = Body(...)):
        tracker().create_account(str(payload["name"]))
        return {"status": "ok"}

    @app.patch("/api/accounts/{account_name}")
    def api_rename_account(account_name: str, payload: dict = Body(...)):
        tracker().rename_account(account_name, str(payload["name"]))
        return {"status": "ok"}

    @app.delete("/api/accounts/{account_name}")
    def api_delete_account(account_name: str):
        tracker().delete_account(account_name)
        return {"status": "ok"}

    @app.get("/api/payees")
    def api_payees():
        return {"payees": tracker().payee_summaries()}

    @app.get("/api/categories")
    def api_categories():
        return {"categories": tracker().category_summaries()}

    @app.get("/api/transactions")
    def api_transactions(
        account: str | None = None,
        category: str | None = None,
        payee: str | None = None,
        date: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
    ):
        records = tracker().list_transactions(
            account=account,
            category=category,
            payee=payee,
            exact_date=date,
            start_date=start_date,
            end_date=end_date,
        )
        return {"transactions": [serialize_transaction(item) for item in records]}

    @app.post("/api/transactions")
    async def api_create_transaction(
        account_name: str = Form(...),
        payee_name: str = Form(...),
        category_value: str = Form(...),
        amount: str = Form(...),
        spent_on: str = Form(...),
        notes: str = Form(""),
        files: list[UploadFile] = File(default=[]),
    ):
        current_tracker = tracker()
        category_name, subcategory_name = split_category_value(category_value)
        with saved_uploads(files) as paths:
            created = current_tracker.add_transaction(
                account_name=account_name,
                payee_name=payee_name,
                category_name=category_name,
                subcategory_name=subcategory_name,
                amount=amount,
                spent_on=spent_on,
                notes=notes,
                attachment_paths=paths,
            )
        return {"transactions": [serialize_transaction(item) for item in created]}

    @app.put("/api/transactions/{transaction_id}")
    async def api_update_transaction(
        transaction_id: str,
        account_name: str = Form(...),
        payee_name: str = Form(...),
        category_value: str = Form(...),
        amount: str = Form(...),
        spent_on: str = Form(...),
        notes: str = Form(""),
        files: list[UploadFile] = File(default=[]),
    ):
        current_tracker = tracker()
        category_name, subcategory_name = split_category_value(category_value)
        with saved_uploads(files) as paths:
            updated = current_tracker.update_transaction(
                transaction_id,
                account_name=account_name,
                payee_name=payee_name,
                category_name=category_name,
                subcategory_name=subcategory_name,
                amount=amount,
                spent_on=spent_on,
                notes=notes,
                attachment_paths=paths,
            )
        return {"transactions": [serialize_transaction(item) for item in updated]}

    @app.delete("/api/transactions/{transaction_id}")
    def api_delete_transaction(transaction_id: str):
        return {"deleted": tracker().delete_transaction(transaction_id)}

    return app
